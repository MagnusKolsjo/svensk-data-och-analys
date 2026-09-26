# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Brottsförebyggande rådets (Brå) statistik över anmälda brott.

Brå publicerar ingen maskinläsbar API-tjänst för kriminalstatistiken. Den
interaktiva databasen "Anmälda brott" ligger i SolWebb — en sessionsbaserad
Struts-applikation på statistik.bra.se där urvalet byggs i tre flikar
(brottstyp, region, period) och resultatet renderas som en HTML-tabell.
Den här klienten replikerar det flödet över HTTP.

Flödet, härlett genom direkt observation av den levande tjänsten:

    GET  /action/index                          -> etablera session (JSESSIONID)
    GET  /action/start?menykatalogid=1          -> ladda menyträdet
    GET  /action/anmalda/urval/urval?menyid=NNN -> urvalssidan; bär koderna
                                                   som inline JS-arrayer
    POST /action/anmalda/urval/vantapopup       -> lämna urvalet (id-strängar)
    GET  /action/anmalda/urval/sok              -> kör sökningen
    GET  /action/anmalda/urval/soktabell        -> datatabellen (HTML)

Koderna (brottstyp, region, period) ligger som `arrayNivaett`,
`arrayRegionNivaEtt` och `arrayPeriod` i urvalssidans HTML och parsas ut
därifrån — aldrig hårdkodade, eftersom Brå reviderar brottskoderna.

`menyid` styr vilken urvalsdimension som gäller. `101` ger anmälda brott
per kommun/region och år, vilket är det som kopplas till geo-stacken via
kommunkod. Andra menyid:n (månad, fördelning på kön/ålder m.m.) kan anges.

SolWebb tar ett urval per förfrågan på ett robust sätt; den här klienten
itererar därför över kombinationer av (brottstyp × region × period) och
slår ihop resultaten. Det undviker beroendet av hur flervalskoderna
serialiseras i id-strängen — ett format som inte är dokumenterat och kan
ändras. Priset är en HTTP-runda per kombination, vilket rate-limitas och
begränsas av ett tak (`max_kombinationer`).

TLS: statistik.bra.se skickar fel intermediate-cert i sin kedja. curl
räddar det via AIA-hämtning, men Python gör inte det. Rätt intermediate
(DigiCert EV RSA CA G2) bundlas i `certs/` och läses in i SSL-kontexten så
att certifieringen förblir påslagen.

Ingångspunkter:
    lista_brottstyper(menyid=101)
    lista_regioner(menyid=101)
    lista_perioder(menyid=101)
    hamta_statistik(brottstyp_koder, region_koder, period_koder, per_100k=False, menyid=101)
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import ssl
from pathlib import Path
from typing import Any

import certifi
import httpx

from data_och_analys.infra.paginering import paginera
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://statistik.bra.se/solwebb/action"
MYNDIGHET = "bra"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Anmälda brott per kommun/region och år — den dimension som kopplar mot
# geo-stacken via kommunkod.
MENYID_KOMMUN_AR = 101

# Tak mot oavsiktligt enorma uttag (en HTTP-runda per kombination).
_MAX_KOMBINATIONER = 200

# Rätt intermediate-cert som servern utelämnar ur sin kedja.
_INTERMEDIATE_CERT = (
    Path(__file__).resolve().parent / "certs" / "bra_digicert_ev_rsa_ca_g2.pem"
)


# ============================================================================
# TLS och HTTP-session
# ============================================================================


_ssl_ctx: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """SSL-kontext = certifi + det intermediate servern glömmer skicka.

    Cachas så att certifikaten inte läses från disk vid varje anrop.
    Verifiering förblir påslagen — vi kompletterar bara kedjan.
    """
    global _ssl_ctx
    if _ssl_ctx is None:
        ctx = ssl.create_default_context(cafile=certifi.where())
        if _INTERMEDIATE_CERT.is_file():
            ctx.load_verify_locations(str(_INTERMEDIATE_CERT))
        else:
            logger.warning(
                "Brå intermediate-cert saknas på %s — TLS kan misslyckas",
                _INTERMEDIATE_CERT,
            )
        _ssl_ctx = ctx
    return _ssl_ctx


def _ny_klient() -> httpx.AsyncClient:
    """Skapar en httpx-klient med Brå-session (cookies, UA, TLS-kontext)."""
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=_TIMEOUT,
        follow_redirects=True,
        verify=_ssl_context(),
    )


async def _init_urval(klient: httpx.AsyncClient, menyid: int) -> str:
    """Etablerar sessionen och returnerar urvalssidans HTML.

    Stegen måste köras i ordning: SolWebb bygger sessionstillstånd i index
    och menyträdet innan urvalssidan kan laddas — annars svarar den med ett
    internt fel.
    """
    await hamta_grind(MYNDIGHET).vanta()
    await klient.get(f"{BAS_URL}/index")
    await klient.get(f"{BAS_URL}/start?menykatalogid=1")
    svar = await klient.get(f"{BAS_URL}/anmalda/urval/urval?menyid={menyid}")
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Brå-urval misslyckades ({svar.status_code}) för menyid={menyid}",
            request=svar.request,
            response=svar,
        )
    return svar.content.decode("iso-8859-1")


# ============================================================================
# Kodlistor — parsas ur urvalssidans JS-arrayer
# ============================================================================


def _parsa_kodarray(html: str, arraynamn: str) -> list[str]:
    """Plockar ut strängvärdena ur en JS-array `arraynamn[i]="..."`."""
    monster = re.escape(arraynamn) + r"\[\d+\]\s*=\s*\"([^\"]*)\""
    return re.findall(monster, html)


async def lista_brottstyper(menyid: int = MENYID_KOMMUN_AR) -> dict[str, Any]:
    """Listar valbara brottstyper med kod, namn och hierarkinivå.

    Brottstyperna är hierarkiska — namnet har inledande blanksteg som anger
    nivå (t.ex. "Totalt antal brott" > "Brott mot brottsbalken" >
    "3-7 kap. Brott mot person"). `niva` återger indraget. `kod` matas till
    `hamta_statistik`.
    """
    html = await _init_urval(_ny := _ny_klient(), menyid)
    try:
        poster = _parsa_kodarray(html, "arrayNivaett")
    finally:
        await _ny.aclose()

    rader: list[dict[str, Any]] = []
    for p in poster:
        kod, _, namn = p.partition("*")
        rader.append({
            "kod": kod,
            "namn": namn.strip(),
            "niva": (len(namn) - len(namn.lstrip())),
        })
    return paginera(rader, per_sida=len(rader) or 1)


async def lista_regioner(menyid: int = MENYID_KOMMUN_AR) -> dict[str, Any]:
    """Listar valbara regioner (kommuner, län, riket) med kod och namn.

    För `menyid=101` är detta kommuner. `kod` är Brå:s interna regionkod
    (inte SCB:s kommunkod) — använd `namn` för att korsa mot geo-stacken.
    """
    html = await _init_urval(_ny := _ny_klient(), menyid)
    try:
        poster = _parsa_kodarray(html, "arrayRegionNivaEtt")
    finally:
        await _ny.aclose()

    rader = []
    for p in poster:
        kod, _, namn = p.partition("*")
        rader.append({"kod": kod, "namn": namn.strip()})
    return paginera(rader, per_sida=len(rader) or 1)


async def lista_perioder(menyid: int = MENYID_KOMMUN_AR) -> list[dict[str, Any]]:
    """Listar valbara perioder med kod och år.

    Periodposterna har formen `kod*år*förälder*etikett*...`. Returnerar
    `{kod, ar, etikett}` sorterat som källan ger dem (senaste året först).
    """
    html = await _init_urval(_ny := _ny_klient(), menyid)
    try:
        poster = _parsa_kodarray(html, "arrayPeriod")
    finally:
        await _ny.aclose()

    rader = []
    for p in poster:
        delar = p.split("*")
        if len(delar) >= 2:
            rader.append({
                "kod": delar[0],
                "ar": delar[1],
                "etikett": delar[3] if len(delar) > 3 else delar[1],
            })
    return rader


# ============================================================================
# Data — körning av sökningen
# ============================================================================


def _parsa_varde(soktabell_html: str) -> str | None:
    """Plockar ut datavärdet ur soktabell-HTML:en.

    Vid enkelval renderas exakt ett datavärde — den sista cellen som ser ut
    som ett tal (eller Brås saknad-markör `..`/`-`). Tusentalsavgränsaren är
    hårt mellanslag (`\xa0`); den behålls i råsträngen.
    """
    celler = [
        _html.unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip()
        for c in re.findall(r"<td[^>]*>(.*?)</td>", soktabell_html, re.S | re.I)
    ]
    for cell in reversed(celler):
        if not cell or "Print" in cell:
            continue
        if cell in ("..", "-") or re.fullmatch(r"[\d\s.,]+", cell):
            return cell
    return None


def _till_tal(varde: str | None) -> float | None:
    """Tolkar ett rått värde till tal (None för saknat eller icke-numeriskt)."""
    if not varde or varde in ("..", "-"):
        return None
    rensat = varde.replace(" ", "").replace(",", ".")
    try:
        return float(rensat)
    except ValueError:
        return None


async def _hamta_en(
    klient: httpx.AsyncClient,
    menyid: int,
    brottstyp: str,
    region: str,
    period: str,
    per_100k: bool,
) -> str | None:
    """Kör en enskild sökning (en brottstyp × region × period) och returnerar värdet."""
    await hamta_grind(MYNDIGHET).vanta()
    data = {
        "showTable": "",
        "brottstyp_id_string": brottstyp,
        "region_id_string": region,
        "period_id_string": period,
        "fordelning_id_string": "",
        "antal": "0" if per_100k else "1",
        "antal_100k": "1" if per_100k else "0",
    }
    referer = {"Referer": f"{BAS_URL}/anmalda/urval/urval?menyid={menyid}"}
    await klient.post(f"{BAS_URL}/anmalda/urval/vantapopup", data=data, headers=referer)
    await klient.get(f"{BAS_URL}/anmalda/urval/sok", headers=referer)
    tabell = await klient.get(f"{BAS_URL}/anmalda/urval/soktabell", headers=referer)
    return _parsa_varde(tabell.content.decode("iso-8859-1"))


async def hamta_statistik(
    brottstyp_koder: str | list[str],
    region_koder: str | list[str],
    period_koder: str | list[str],
    per_100k: bool = False,
    menyid: int = MENYID_KOMMUN_AR,
    sida: int = 1,
    per_sida: int = 500,
    max_kombinationer: int = _MAX_KOMBINATIONER,
) -> dict[str, Any]:
    """Hämtar anmälda brott för kombinationer av brottstyp, region och period.

    Varje argument är en kod eller en lista av koder (från
    `lista_brottstyper`, `lista_regioner`, `lista_perioder`). Klienten kör
    en sökning per kombination och slår ihop resultaten till en rad per
    `(brottstyp, region, period)` med `{varde_text, varde}`.

    `per_100k=True` ger antal per 100 000 invånare i stället för absolut
    antal. `menyid` styr urvalsdimensionen (101 = kommun/region × år).

    Antalet kombinationer = produkten av listornas längder; det begränsas av
    `max_kombinationer` (en HTTP-runda var). Överstigs taket körs ingenting
    och ett fel höjs så att uttaget delas upp i stället för att tyst kapas.

    Svaret är paginerat: `{total, sida, per_sida, antal_sidor, datapunkter}`.
    """
    bt = [brottstyp_koder] if isinstance(brottstyp_koder, str) else list(brottstyp_koder)
    rg = [region_koder] if isinstance(region_koder, str) else list(region_koder)
    pe = [period_koder] if isinstance(period_koder, str) else list(period_koder)

    antal = len(bt) * len(rg) * len(pe)
    if antal > max_kombinationer:
        raise ValueError(
            f"{antal} kombinationer överstiger taket {max_kombinationer}. "
            "Dela upp uttaget (färre brottstyper, regioner eller år per anrop)."
        )

    rader: list[dict[str, Any]] = []
    klient = _ny_klient()
    try:
        await _init_urval(klient, menyid)
        for b in bt:
            for r in rg:
                for p in pe:
                    try:
                        varde = await _hamta_en(klient, menyid, b, r, p, per_100k)
                    except Exception as fel:  # noqa: BLE001
                        logger.warning(
                            "Brå-uttag misslyckades för (%s, %s, %s): %s",
                            b, r, p, fel,
                        )
                        varde = None
                    rader.append({
                        "brottstyp_kod": b,
                        "region_kod": r,
                        "period_kod": p,
                        "enhet": "per_100k" if per_100k else "antal",
                        "varde_text": varde,
                        "varde": _till_tal(varde),
                    })
    finally:
        await klient.aclose()

    return paginera(rader, sida=sida, per_sida=per_sida)
