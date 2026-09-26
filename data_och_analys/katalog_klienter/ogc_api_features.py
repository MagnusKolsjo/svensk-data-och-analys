# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Generisk klient för OGC API Features (OAFeat) — modern WFS-efterföljare.

OGC API Features (även kallad "OAFeat" eller bara "Features") är OGC:s
RESTful efterföljare till WFS. Endpoint-strukturen är standardiserad:

    GET /                          -> landningssida
    GET /collections               -> lista över datalager
    GET /collections/{id}          -> metadata för ett lager
    GET /collections/{id}/items    -> features (GeoJSON, paginerat)
    GET /conformance               -> vilka OGC-conformance-klasser stöds

Klienten är generisk över instanser. En "instans" är en specifik landnings-
sida — t.ex. Lantmäteriets administrativ-indelning eller hydrografi.

Auth-stöd
---------
Tre auth-typer hanteras transparent enligt instansens konfiguration:

    "none"                         offentligt öppet, inga creds
    "apikey"                       statisk nyckel i `ApiKey`-header
    "oauth2_client_credentials"    consumer-key + consumer-secret -> Bearer

OAuth2 client_credentials-flödet (WSO2-standard, Lantmäteriet m.fl.):

    POST {token_url}
        Authorization: Basic base64(consumer_key:consumer_secret)
        grant_type=client_credentials
    -> {"access_token": ..., "expires_in": 3600, "token_type": "Bearer"}

Token cachas in-memory per instans med en 60s säkerhetsmarginal mot TTL.
Vid 401 från en API-anrop slängs cachen och en ny token hämtas — det
gör att klienten klarar tokens som löper ut under en synk.

Ingångspunkter:
    lista_instanser() -> list[dict]
    lista_collections(instans) -> dict
    hamta_collection(instans, collection_id) -> dict
    hamta_items(instans, collection_id, ...) -> dict
    stroma_alla_items(instans, collection_id, ...) -> async generator
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Instansregister
# ============================================================================


@dataclass(frozen=True)
class OAFeatInstans:
    namn: str
    bas_url: str
    auth_typ: str = "none"                     # none|apikey|oauth2_client_credentials
    consumer_key_env: str | None = None
    consumer_secret_env: str | None = None
    apikey_env: str | None = None
    token_url: str | None = None
    # OAuth-scopes som ska begäras vid client_credentials-anropet.
    # WSO2 ger bara "default" om inget skickas — då nekas resurser
    # som kräver finkornigare scope (vanligt 403 på items-endpoints).
    oauth_scopes: tuple[str, ...] = ()
    # Env-variabel som överstyr scope-listan (space-separerad).
    oauth_scopes_env: str | None = None
    beskrivning: str = ""


# Lantmäteriets API-portal listar 15 publicerade API:er bakom WSO2. För
# administrativa indelningar (kommun, län, rike) använder vi OGC API
# Features med OAuth2 client_credentials. Fler instanser läggs in när
# andra svenska myndigheter publicerar OAFeat-endpoints.
_BASLINJE: dict[str, OAFeatInstans] = {
    "lantmateriet_admin": OAFeatInstans(
        namn="lantmateriet_admin",
        bas_url="https://api.lantmateriet.se/ogc-features/v1/administrativ-indelning",
        # OGC-Features-API:t stöds enligt OpenAPI-specen med OAuth +
        # scope `ogc-features:ngp.read`, men i praktiken visar LM:s
        # WSO2 att Application User-grant (password grant med LM-konto)
        # krävs, inte client_credentials. Behåller flödet här för det
        # fall att LM ändrar konfigurationen, men förvänta dig 403
        # tills den ändringen sker. För administrativ indelning används
        # istället Atom-feed-flödet (se `katalog_klienter.atom_inspire`
        # när det är byggt).
        auth_typ="oauth2_client_credentials",
        consumer_key_env="LANTMATERIET_CONSUMER_KEY",
        consumer_secret_env="LANTMATERIET_CONSUMER_SECRET",
        # WSO2 accepterar även en `ApiKey`-header enligt sitt 401-svar.
        # Växla dit med DOA_OAFEAT_LANTMATERIET_ADMIN_AUTH_TYP=apikey om
        # client_credentials ger 403 för prenumerationen.
        apikey_env="LANTMATERIET_API_NYCKEL",
        token_url="https://apimanager.lantmateriet.se/oauth2/token",
        oauth_scopes=("ogc-features:ngp.read",),
        oauth_scopes_env="LANTMATERIET_OAUTH_SCOPES",
        beskrivning="Administrativ indelning (kommun, län, rike) per år",
    ),
}


def _med_overstyrning(bas: OAFeatInstans) -> OAFeatInstans:
    """Tillämpar env-överstyrning av bas-URL och autentiseringssätt.

    Auth-typen är överstyrbar därför att samma endpoint kan kräva olika
    saker beroende på hur prenumerationen är uppsatt. Lantmäteriets WSO2
    svarar `WWW-Authenticate: Basic ..., Internal API Key ..., Bearer ...`
    och accepterar enligt sitt eget felmeddelande antingen en OAuth-token
    eller en `ApiKey`-header. Vilken som fungerar avgörs av applikationen
    i devportalen, inte av koden — därför en env-rad i stället för en
    commit när det visar sig.
    """
    overstyrd = os.getenv(f"DOA_OAFEAT_{bas.namn.upper()}_BAS_URL", "").strip()
    auth = os.getenv(f"DOA_OAFEAT_{bas.namn.upper()}_AUTH_TYP", "").strip().lower()
    if not overstyrd and not auth:
        return bas
    return OAFeatInstans(
        namn=bas.namn,
        bas_url=overstyrd.rstrip("/") if overstyrd else bas.bas_url,
        auth_typ=auth or bas.auth_typ,
        consumer_key_env=bas.consumer_key_env,
        consumer_secret_env=bas.consumer_secret_env,
        apikey_env=bas.apikey_env,
        token_url=bas.token_url,
        beskrivning=bas.beskrivning,
    )


def _instans_ur_adress(url: str) -> OAFeatInstans:
    """Landningssidan ur en adress: allt före `/collections`. Öppna tjänster
    kräver ingen autentisering; de som gör det står i registret."""
    rot = re.split(r"/collections(?:/|$|\?)", url.split("#")[0], maxsplit=1)[0].split("?")[0]
    return OAFeatInstans(namn=urlparse(url).hostname or url, bas_url=rot.rstrip("/"))


def hamta_instans(namn: str) -> OAFeatInstans:
    """Namngiven instans ur registret, eller en instans ur en adress."""
    if namn.startswith(("http://", "https://")):
        return _instans_ur_adress(namn)
    if namn not in _BASLINJE:
        kanda = ", ".join(sorted(_BASLINJE))
        raise ValueError(
            f"okänd oafeat-instans: {namn!r}. Tillgängliga: {kanda}"
        )
    return _med_overstyrning(_BASLINJE[namn])


def lista_instanser() -> list[dict[str, str]]:
    """Returnerar instansregistret med namn, URL och beskrivning."""
    return [
        {
            "namn": i.namn,
            "bas_url": i.bas_url,
            "auth_typ": i.auth_typ,
            "beskrivning": i.beskrivning,
        }
        for namn in sorted(_BASLINJE)
        for i in (_med_overstyrning(_BASLINJE[namn]),)
    ]


# ============================================================================
# OAuth2 token-cache
# ============================================================================
#
# Per-instans-cache: instansnamn -> (token, expires_at_monotonic). TTL läses
# ur servern svar; vi minskar med 60s säkerhetsmarginal så att en token inte
# används precis innan den löper ut.

_token_cache: dict[str, tuple[str, float]] = {}
_token_las = asyncio.Lock()


async def _hamta_oauth2_token(instans: OAFeatInstans) -> str:
    """Hämtar (eller återanvänder) Bearer-token för en OAuth2-instans."""
    if instans.token_url is None:
        raise RuntimeError(
            f"instans {instans.namn} saknar token_url men har auth_typ "
            f"oauth2_client_credentials"
        )

    # Snabb-väg utan lås
    cachat = _token_cache.get(instans.namn)
    if cachat is not None:
        token, expires_at = cachat
        if time.monotonic() < expires_at - 60:
            return token

    async with _token_las:
        # Double-check efter lås
        cachat = _token_cache.get(instans.namn)
        if cachat is not None:
            token, expires_at = cachat
            if time.monotonic() < expires_at - 60:
                return token

        ck = os.getenv(instans.consumer_key_env or "", "").strip()
        cs = os.getenv(instans.consumer_secret_env or "", "").strip()
        if not ck or not cs:
            raise RuntimeError(
                f"saknar OAuth-creds för {instans.namn} — sätt "
                f"{instans.consumer_key_env} och {instans.consumer_secret_env} "
                f"i .env (hämtas från Lantmäteriets API-portal: "
                f"apimanager.lantmateriet.se/devportal)"
            )

        basic = base64.b64encode(f"{ck}:{cs}".encode()).decode()

        # Bygg scope-listan: env-överstyrning först, annars baslinjens scopes.
        scopes: list[str] = list(instans.oauth_scopes)
        if instans.oauth_scopes_env:
            overstyrt = os.getenv(instans.oauth_scopes_env, "").strip()
            if overstyrt:
                scopes = overstyrt.split()

        data: dict[str, str] = {"grant_type": "client_credentials"}
        if scopes:
            data["scope"] = " ".join(scopes)

        async with httpx.AsyncClient(timeout=30.0) as klient:
            svar = await klient.post(
                instans.token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=data,
            )
        if svar.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"OAuth2 token-anrop misslyckades ({svar.status_code}) mot "
                f"{instans.token_url}: {svar.text[:200]}",
                request=svar.request,
                response=svar,
            )
        data = svar.json()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 3600))
        _token_cache[instans.namn] = (token, time.monotonic() + expires_in)
        logger.info(
            "Ny OAuth2-token för %s (giltig %d sek)", instans.namn, expires_in
        )
        return token


def _slang_token_cache(instans_namn: str) -> None:
    """Slänger cachad token (anropas vid 401 från API)."""
    _token_cache.pop(instans_namn, None)


# ============================================================================
# HTTP-anrop
# ============================================================================

USER_AGENT = (
    "svensk-data-och-analys/OgcApiFeaturesClient (+https://github.com/)"
)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_LANG_TIMEOUT = httpx.Timeout(600.0, connect=30.0)


def _har_creds(instans: OAFeatInstans) -> bool:
    """Returnerar True om miljön har de creds som instansen kräver."""
    if instans.auth_typ in ("oauth2_client_credentials", "basic_auth"):
        ck = os.getenv(instans.consumer_key_env or "", "").strip()
        cs = os.getenv(instans.consumer_secret_env or "", "").strip()
        return bool(ck and cs)
    if instans.auth_typ == "apikey":
        return bool(os.getenv(instans.apikey_env or "", "").strip())
    return True


async def _bygg_headers(
    instans: OAFeatInstans, accept: str = "application/geo+json"
) -> dict[str, str]:
    """Bygger HTTP-headers. Auth är opportunistisk — har vi creds skickas
    de, annars går anropet anonymt. Det gör att öppna endpoints (landings-
    sida, collections-listning) fortsätter fungera utan att .env är ifyllt.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if not _har_creds(instans):
        return headers
    if instans.auth_typ == "oauth2_client_credentials":
        token = await _hamta_oauth2_token(instans)
        headers["Authorization"] = f"Bearer {token}"
    elif instans.auth_typ == "basic_auth":
        ck = os.getenv(instans.consumer_key_env or "", "").strip()
        cs = os.getenv(instans.consumer_secret_env or "", "").strip()
        if ck and cs:
            basic = base64.b64encode(f"{ck}:{cs}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
    elif instans.auth_typ == "apikey":
        nyckel = os.getenv(instans.apikey_env or "", "").strip()
        if nyckel:
            headers["ApiKey"] = nyckel
    return headers


async def _hamta_json(
    instans: OAFeatInstans,
    sokvag: str,
    parametrar: dict[str, Any] | None = None,
    accept: str = "application/geo+json",
    timeout: httpx.Timeout = _TIMEOUT,
) -> Any:
    url = f"{instans.bas_url}/{sokvag.lstrip('/')}"
    forsta_forsok = True
    while True:
        headers = await _bygg_headers(instans, accept)
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as klient:
            svar = await klient.get(url, params=parametrar)
        if (
            svar.status_code in (401, 403)
            and forsta_forsok
            and instans.auth_typ != "none"
        ):
            # 401: token kan ha gått ut. 403: scopes kan ha ändrats hos
            # API-ägaren. Båda löses av att slänga cache och hämta ny token
            # en gång. Andra 403 i rad släpps igenom som äkta access denied.
            _slang_token_cache(instans.namn)
            forsta_forsok = False
            continue
        if svar.status_code in (401, 403):
            if not _har_creds(instans):
                hint = (
                    f"Endpointen kräver autentisering — sätt "
                    f"{instans.consumer_key_env}/{instans.consumer_secret_env} "
                    f"i .env (hämtas från apimanager.lantmateriet.se/devportal)."
                    if instans.auth_typ == "oauth2_client_credentials"
                    else f"Sätt {instans.apikey_env} i .env."
                )
            else:
                hint = (
                    "Verifiera att dina creds är giltiga och att din "
                    "application prenumererar på API:t i portalen."
                )
            raise httpx.HTTPStatusError(
                f"OGC Features-anrop nekat ({svar.status_code}) mot {url}. "
                f"{hint}",
                request=svar.request,
                response=svar,
            )
        if svar.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"OGC Features-anrop misslyckades ({svar.status_code}) mot "
                f"{url}: {svar.text[:200]}",
                request=svar.request,
                response=svar,
            )
        return svar.json()


# ============================================================================
# Publikt API
# ============================================================================


async def lista_collections(instans: str = "lantmateriet_admin") -> dict[str, Any]:
    """Listar alla collections i en OGC API Features-instans.

    Landningssidan och collections-listan är öppna utan auth även när
    items-endpointen kräver token. Bra för att utforska vad som finns
    utan att behöva creds.
    """
    inst = hamta_instans(instans)
    # Collections-listan kräver inte auth i de flesta WSO2-installationer
    # men kostar inget att skicka token om vi har den.
    return await _hamta_json(inst, "collections", accept="application/json")


async def hamta_collection(
    instans: str, collection_id: str
) -> dict[str, Any]:
    """Returnerar metadata för en specifik collection."""
    inst = hamta_instans(instans)
    return await _hamta_json(
        inst, f"collections/{collection_id}", accept="application/json"
    )


async def hamta_items(
    instans: str,
    collection_id: str,
    bbox: tuple[float, float, float, float] | None = None,
    limit: int = 100,
    offset: int | None = None,
    filter: str | None = None,
) -> dict[str, Any]:
    """Hämtar en sida med features ur en collection.

    `bbox` filtrerar geografiskt (minx, miny, maxx, maxy i collection-CRS).
    `filter` är en CQL2- eller ECQL-sträng (instansspecifik). `limit` är
    sidstorlek; standard 100 men många servrar tillåter mer.

    Returvärdet är ett GeoJSON FeatureCollection med extra fält
    `numberMatched`, `numberReturned` och `links` (inkl. paginering).
    """
    inst = hamta_instans(instans)
    parametrar: dict[str, Any] = {"limit": limit}
    if bbox is not None:
        parametrar["bbox"] = ",".join(str(x) for x in bbox)
    if offset is not None:
        parametrar["offset"] = offset
    if filter is not None:
        parametrar["filter"] = filter
    return await _hamta_json(
        inst,
        f"collections/{collection_id}/items",
        parametrar=parametrar,
        timeout=_LANG_TIMEOUT,
    )


async def stroma_alla_items(
    instans: str,
    collection_id: str,
    bbox: tuple[float, float, float, float] | None = None,
    sida_storlek: int = 1000,
) -> AsyncIterator[dict[str, Any]]:
    """Strömmar alla features ur en collection via pagination.

    Följer `links.rel=next` tills det inte längre finns en nästa-länk.
    Yieldar enskilda features (inte hela FeatureCollections). Token
    refreshas automatiskt om den löper ut under en lång paginering.
    """
    inst = hamta_instans(instans)
    parametrar: dict[str, Any] = {"limit": sida_storlek}
    if bbox is not None:
        parametrar["bbox"] = ",".join(str(x) for x in bbox)
    url = f"{inst.bas_url}/collections/{collection_id}/items"

    async with httpx.AsyncClient(timeout=_LANG_TIMEOUT) as klient:
        while url:
            forsta_forsok = True
            while True:
                headers = await _bygg_headers(inst)
                svar = await klient.get(
                    url,
                    params=parametrar if "?" not in url else None,
                    headers=headers,
                )
                if (
                    svar.status_code in (401, 403)
                    and forsta_forsok
                    and inst.auth_typ != "none"
                ):
                    _slang_token_cache(inst.namn)
                    forsta_forsok = False
                    continue
                break

            if svar.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"OGC Features-pagination misslyckades ({svar.status_code}) "
                    f"mot {url}: {svar.text[:200]}",
                    request=svar.request,
                    response=svar,
                )
            data = svar.json()
            for feat in data.get("features", []):
                yield feat

            next_link = next(
                (l for l in data.get("links", []) if l.get("rel") == "next"),
                None,
            )
            url = next_link.get("href") if next_link else None
            parametrar = None
