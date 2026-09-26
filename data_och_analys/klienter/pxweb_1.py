# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""PxWeb v1-klient — gemensam för alla myndigheter som ännu talar v1.

Myndighet väljs via en `myndighet`-parameter; bas-URL slås upp i det
gemensamma registret (`klienter.myndigheter`). Endpoint-strukturen är
likadan hos alla v1-installationer; det som skiljer är bas-URL och
rate limit.

Klienten avvisar myndigheter som registret säger talar v2 — den dagen
en källa rullar över ändras registret (eller en env-överstyrning sätts)
och samma myndighet betjänas istället av `pxweb_2`-klienten.

API-trädet hos en PxWeb v1-installation:
    GET  /{lang}/                            -> databaser
    GET  /{lang}/{db}/{path...}              -> noder (nivå eller tabell)
    GET  /{lang}/{db}/{path...}/{tabell}     -> metadata för tabellen
    POST /{lang}/{db}/{path...}/{tabell}     -> datafråga (PxWeb query DSL)

Ingångspunkter:
    lista_myndigheter() -> list[str]
    lista_databaser(myndighet) -> list[Databas]
    lista_noder(myndighet, sokvag) -> list[Nod]
    hamta_metadata(myndighet, tabell_sokvag) -> Tabellmetadata
    hamta_data(myndighet, tabell_sokvag, query) -> dict
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from data_och_analys.infra.rate_limit import hamta_grind
from data_och_analys.klienter import myndigheter

logger = logging.getLogger(__name__)

VERSION = 1


def _hamta_konfig(namn: str) -> myndigheter.Myndighet:
    """Hämtar myndighetskonfig och vägrar om versionen inte är 1."""
    m = myndigheter.hamta(namn)
    if m.version != VERSION:
        raise ValueError(
            f"myndigheten {namn!r} talar PxWeb {m.version}, inte {VERSION}. "
            f"Använd pxweb_{m.version}-klienten."
        )
    return m


def lista_myndigheter() -> list[str]:
    """Returnerar ID:n för alla myndigheter som för närvarande talar v1."""
    return myndigheter.myndigheter_med_version(VERSION)


# ============================================================================
# Wire-modeller (PxWeb v1)
# ============================================================================


class Databas(BaseModel):
    """En databas i en PxWeb-installation. Ofta bara en per myndighet."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dbid: str
    text: str


class Nod(BaseModel):
    """En nod i tabellträdet — antingen en undermapp eller en tabell.

    Typvärdet är PxWeb v1:s konvention:
        "l" = level (mapp)
        "t" = tabell
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    typ: str = Field(alias="type")
    text: str
    updated: str | None = None


class Variabel(BaseModel):
    """En variabel i tabellens metadata.

    `values` och `value_texts` är parallella listor: index N i den ena
    motsvarar index N i den andra. Det är PxWeb v1:s konvention.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    code: str
    text: str
    values: list[str] = Field(default_factory=list)
    value_texts: list[str] = Field(default_factory=list, alias="valueTexts")
    time: bool = False
    elimination: bool = False


class Tabellmetadata(BaseModel):
    """Metadata för en tabell — variabler, titel, källinfo."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    title: str
    variables: list[Variabel] = Field(default_factory=list)


# ============================================================================
# HTTP-anrop
# ============================================================================

# Identifierar oss tydligt mot källornas WAF — bättre att synas än att
# fakas som en webbläsare.
# Skogsstyrelsens installation avvisar anrop utan webbläsarlik User-Agent
# med 403 — även mot deras vanliga webbplats. En ren verktygssträng räcker
# alltså inte. Identiteten står kvar sist så att den som läser loggarna ser
# vem som ringer.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
    "svensk-data-och-analys/PxWeb1Client"
)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def _hamta_json(myndighet_namn: str, url: str) -> Any:
    """GET med rate-limit, JSON-tolkning och svenska felmeddelanden."""
    await hamta_grind(myndighet_namn).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"PxWeb v1-anrop misslyckades ({svar.status_code}) mot {url}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


async def _utfor_post(
    myndighet_namn: str, url: str, kropp: dict[str, Any]
) -> httpx.Response:
    """POST med rate-limit och statuskontroll — returnerar råsvaret.

    Datafrågor kan begära csv/px/html via `response.format`, så
    anroparen avgör själv om kroppen ska tolkas som JSON eller bäras
    som text.
    """
    await hamta_grind(myndighet_namn).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.post(url, json=kropp)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"PxWeb v1-datafråga misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar


async def _posta_json(
    myndighet_namn: str, url: str, kropp: dict[str, Any]
) -> Any:
    """POST med rate-limit, JSON-kropp och svenska felmeddelanden."""
    svar = await _utfor_post(myndighet_namn, url, kropp)
    return svar.json()


def _bygg_sokvag(bas_url: str, segment: list[str]) -> str:
    """Slår ihop bas-URL och stigsegment med URL-encodning per segment."""
    if not segment:
        return bas_url + "/"
    return bas_url + "/" + "/".join(quote(s, safe="") for s in segment)


# ============================================================================
# Publikt API
# ============================================================================


# PxWeb v1:s nodträd adresseras med en lista av segment. Där stigen behöver
# vara en enda sträng — i sökindexets `kalla_id`, i API-routerns `sokvag` —
# måste separatorn vara ett tecken som inte kan stå i ett nodnamn. Komma
# duger inte: Tillväxtanalys har noden "Aktiva, nyetablerade och nedlagda
# företag" och Jordbruksverket flera liknande, och en split på komma delar
# då mitt i ett segment och ger 400 från källan.
STIG_SEPARATOR = "|"


def dela_stig(stig: str) -> list[str]:
    """Delar en strängad stig till segment.

    Tar `|` när det finns och faller tillbaka på komma annars, så att
    länkar och bokmärken från tiden före separatorbytet fortsätter
    fungera för de stigar där komma var entydigt.
    """
    tecken = STIG_SEPARATOR if STIG_SEPARATOR in stig else ","
    return [s.strip() for s in stig.split(tecken) if s.strip()]


def foga_stig(segment: list[str]) -> str:
    """Fogar ihop segment till en strängad stig."""
    return STIG_SEPARATOR.join(segment)


async def lista_databaser(myndighet: str) -> list[Databas]:
    """Listar databaser hos en myndighet.

    Många installationer har bara en databas; dess `dbid` behövs ändå
    som första segment i `sokvag` för efterföljande anrop.
    """
    m = _hamta_konfig(myndighet)
    data = await _hamta_json(myndighet, _bygg_sokvag(m.bas_url, []))
    return [Databas.model_validate(x) for x in data]


async def lista_noder(myndighet: str, sokvag: list[str]) -> list[Nod]:
    """Returnerar innehållet i en mapp i tabellträdet.

    `sokvag` är listan med stigsegment från och med databas-ID — t.ex.
    `["JO", "JO0103"]`. Tom lista listar databaserna (men använd då
    `lista_databaser` för korrekt typning).
    """
    m = _hamta_konfig(myndighet)
    data = await _hamta_json(myndighet, _bygg_sokvag(m.bas_url, sokvag))
    return [Nod.model_validate(x) for x in data]


async def hamta_metadata(
    myndighet: str, tabell_sokvag: list[str]
) -> Tabellmetadata:
    """Hämtar variabel- och titelmetadata för en tabell.

    Alltid anropad innan en datafråga byggs — variablernas koder och
    värdelistor får aldrig antas från träningsdata.
    """
    m = _hamta_konfig(myndighet)
    data = await _hamta_json(myndighet, _bygg_sokvag(m.bas_url, tabell_sokvag))
    return Tabellmetadata.model_validate(data)


def uppskatta_celler(
    metadata: Tabellmetadata, query: list[dict[str, Any]]
) -> int:
    """Multiplicerar urvalsstorlekar för att uppskatta antalet dataceller.

    Symmetriskt med `pxweb_2.uppskatta_celler`. Variabler som inte är
    med i `query` räknas som "alla värden" om de är `elimination=False`
    (vi auto-fyller dem med wildcard innan POST). `elimination=True`-
    variabler som saknas räknas som 1 cell eftersom källan summerar
    över dem.

    `values: ["*"]` eller `filter: "all"` tolkas också som "alla
    värden" och räknas mot dimensionens fulla storlek.
    """
    valda_per_kod = {sel.get("code"): sel.get("selection") or {} for sel in query}
    celler = 1
    for v in metadata.variables:
        sel = valda_per_kod.get(v.code)
        if sel is None:
            if v.elimination:
                antal = 1                            # källan summerar
            else:
                antal = max(len(v.values), 1)        # vi auto-fyller med wildcard
        else:
            varden = sel.get("values") or []
            filtertyp = sel.get("filter")
            ar_wildcard = varden == ["*"] or filtertyp == "all"
            antal = max(len(v.values), 1) if ar_wildcard else max(len(varden), 1)
        celler *= antal
    return celler


def _komplettera_query(
    query: list[dict[str, Any]], metadata: Tabellmetadata
) -> list[dict[str, Any]]:
    """Fyller på saknade obligatoriska variabler med wildcard-selektorer.

    Symmetriskt med v2:s automatiska selektor-expansion. En variabel
    som är `elimination=False` MÅSTE finnas i query — annars returnerar
    källan 400 "Missing selection for mandatory variable". Saknade
    `elimination=True`-variabler lämnas ute så källan summerar
    automatiskt (det är poängen med eliminationsflaggan).
    """
    finns = {sel.get("code") for sel in query}
    utokad = list(query)
    for v in metadata.variables:
        if v.code in finns:
            continue
        if v.elimination:
            continue  # låt källan summera
        utokad.append({
            "code": v.code,
            "selection": {"filter": "all", "values": ["*"]},
        })
    return utokad


async def hamta_data(
    myndighet: str,
    tabell_sokvag: list[str],
    query: list[dict[str, Any]],
    format: str = "json-stat2",
    metadata: Tabellmetadata | None = None,
) -> dict[str, Any]:
    """Skickar en datafråga mot en tabell.

    `query` är PxWeb v1:s query-DSL — en lista av selektorer på formen:
        {"code": "Region", "selection": {"filter": "item", "values": ["00"]}}

    När `metadata` skickas in (rekommenderat) gör klienten två saker:

    1. Saknade `elimination=False`-variabler i `query` fylls automatiskt
       med wildcard-selektorer. Det förhindrar 400-fel från källan
       ("Missing selection for mandatory variable") som annars triggar
       när användaren glömt en obligatorisk variabel.

    2. Cellantalet räknas upp i förväg via `uppskatta_celler`. Frågor
       som överskrider myndighetens celltak avvisas med ett tydligt
       svenskt fel innan API-anropet.

    Saknade `elimination=True`-variabler lämnas ute — källan summerar
    automatiskt över dem.

    Svaret är json-stat2 som standard; PxWeb v1 stöder även "json",
    "csv", "px", "xlsx" m.fl. via `format`. JSON-format returneras som
    tolkat objekt; textformat (csv, px, html) kan inte tolkas som JSON
    och bärs i stället som text i ett omslag
    `{"format", "content_type", "innehall"}`. Vilket svaret är avgörs av
    svarets Content-Type, inte av formatnamnet. Binära format (xlsx,
    parquet) kan inte transporteras över MCP-kanalen och avvisas med ett
    tydligt fel.
    """
    m = _hamta_konfig(myndighet)

    if metadata is not None:
        # Förhandskontrollera celltaket innan vi sänder POST. Vi räknar
        # mot expanderad query — efter wildcard-påfyllning — så
        # uppskattningen speglar det faktiska anropet.
        utokad = _komplettera_query(query, metadata)
        antal = uppskatta_celler(metadata, utokad)
        if antal > m.max_celler:
            raise ValueError(
                f"frågan ger ca {antal} celler — {myndighet}:s tak är "
                f"{m.max_celler}. Smalna urvalet eller chunka över en variabel."
            )
        query = utokad

    kropp = {"query": query, "response": {"format": format}}
    svar = await _utfor_post(
        myndighet, _bygg_sokvag(m.bas_url, tabell_sokvag), kropp
    )
    innehallstyp = svar.headers.get("content-type", "").lower()

    if "json" in innehallstyp:
        return svar.json()

    # Binära format (xlsx, parquet, octet-stream) går inte att skicka
    # över MCP-kanalen — avvisa dem innan texttolkning. Notera att
    # "spreadsheetml" innehåller delsträngen "xml", så vi matchar på
    # binära markörer i stället för en bred xml-nyckel.
    binara_markorer = (
        "spreadsheet",
        "officedocument",
        "octet-stream",
        "parquet",
        "excel",
        "zip",
        "pdf",
    )
    if any(markor in innehallstyp for markor in binara_markorer):
        raise ValueError(
            f"formatet {format!r} gav ett binärt svar ({innehallstyp!r}) som "
            f"inte kan bäras över MCP-kanalen. Välj json-stat2 för "
            f"strukturerad data eller csv för text."
        )

    # Textformat (csv, px, html) bärs som text — MCP-kanalen är JSON,
    # så texten läggs i ett omslag i stället för att tolkas som JSON.
    return {
        "format": format,
        "content_type": innehallstyp,
        "innehall": svar.text,
    }
