# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Skolverkets öppna API:er (REST, JSON).

Skolverket exponerar flera fristående REST-API:er under api.skolverket.se.
Den här klienten täcker de två som är öppna utan auth och bär den data vi
vill kombinera med geo-stacken via kommunkod:

    Skolenhetsregistret (v1)
        Det officiella registret över alla skolenheter i Sverige med
        status (Aktiv, Vilande, Planerad), kommunkod och organisationsnr.
        GET /skolenhetsregistret/v1/skolenhet            -> alla enheter
        GET /skolenhetsregistret/v1/skolenhet/{kod}      -> en enhet (detalj)

    Planned educations / Utbildningsinfo (v3)
        Rikare beskrivning per skolenhet: skolformer, årskurser,
        huvudmannatyp, samt länkar till statistik (betyg, behörighet,
        personaltäthet). HAL+JSON med `_embedded` och `_links`.
        GET /planned-educations/v3/school-units          -> paginerat
        GET /planned-educations/v3/school-units/{kod}     -> en enhet
        Statistik nås via `_links.statistics.href` i enhetssvaret — vi
        följer den länken i stället för att gissa sökvägen, så att API:et
        självt pekar ut var statistiken ligger.

Skolenhetsregistret returnerar hela registret (~7 000 enheter) i ett enda
svar utan serverstöd för filtrering. Vi filtrerar och paginerar därför
lokalt. Planned educations paginerar på serversidan via `page`/`size`.

Ingångspunkter:
    lista_skolenheter(fritext=None, status=None, kommunkod=None, sida=1, per_sida=...)
    hamta_skolenhet(skolenhetskod)
    sok_planerade_utbildningar(kommunkod=None, sida=1, per_sida=20)
    hamta_planerad_skolenhet(skolenhetskod)
    hamta_skolenhet_statistik(skolenhetskod)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.paginering import paginera
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://api.skolverket.se"
MYNDIGHET = "skolverket"
USER_AGENT = "svensk-data-och-analys/SkolverketClient (+https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Utbildningsinfo kräver en versionsförhandlad Accept-header.
_HAL_ACCEPT = "application/vnd.skolverket.plannededucations.api.v3.hal+json"


# ============================================================================
# HTTP-anrop
# ============================================================================


async def _hamta_json(
    sokvag: str,
    parametrar: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> Any:
    """GET mot en relativ sökväg under BAS_URL."""
    return await _hamta_full_url(
        f"{BAS_URL}/{sokvag.lstrip('/')}", parametrar, accept=accept
    )


async def _hamta_full_url(
    url: str,
    parametrar: dict[str, Any] | None = None,
    accept: str = "application/json",
) -> Any:
    """GET mot en fullständig URL — används för att följa HAL-länkar."""
    await hamta_grind(MYNDIGHET).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Skolverket-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


# ============================================================================
# Skolenhetsregistret v1
# ============================================================================


async def _hamta_alla_skolenheter() -> list[dict[str, Any]]:
    """Hämtar hela skolenhetsregistret (rått, ofiltrerat).

    Registret levereras i ett svar utan serverfiltrering. Anropet är
    enkelt men svaret är stort — håll det här som enda nätverkssteget och
    filtrera/paginera lokalt ovanpå.
    """
    res = await _hamta_json("skolenhetsregistret/v1/skolenhet")
    return res.get("Skolenheter", [])


async def lista_skolenheter(
    fritext: str | None = None,
    status: str | None = None,
    kommunkod: str | None = None,
    sida: int = 1,
    per_sida: int = 200,
) -> dict[str, Any]:
    """Listar skolenheter ur registret med lokal filtrering och paginering.

    `fritext` matchar (skiftlägesokänsligt) mot skolenhetsnamnet.
    `status` filtrerar på `Aktiv`, `Vilande` eller `Planerad`.
    `kommunkod` är SCB:s 4-siffriga kommunkod (`"0180"` Stockholm).

    Returnerar ett pagineringspaket
    `{total, sida, per_sida, antal_sidor, datapunkter}` där varje
    datapunkt har `{Skolenhetskod, Kommunkod, PeOrgNr, Skolenhetsnamn,
    Status}`.
    """
    enheter = await _hamta_alla_skolenheter()

    if status is not None:
        s = status.casefold()
        enheter = [e for e in enheter if str(e.get("Status", "")).casefold() == s]
    if kommunkod is not None:
        enheter = [e for e in enheter if e.get("Kommunkod") == kommunkod]
    if fritext is not None:
        f = fritext.casefold()
        enheter = [
            e for e in enheter
            if f in str(e.get("Skolenhetsnamn", "")).casefold()
        ]

    return paginera(enheter, sida=sida, per_sida=per_sida)


async def hamta_skolenhet(skolenhetskod: str) -> dict[str, Any] | None:
    """Returnerar fullständig information om en skolenhet ur registret.

    Inkluderar rektorsnamn, kontaktuppgifter, besöks- och leveransadress
    samt geokoordinater (SWEREF 99 och WGS84) under
    `SkolenhetInfo.Besoksadress.GeoData`.
    """
    res = await _hamta_json(f"skolenhetsregistret/v1/skolenhet/{skolenhetskod}")
    return res.get("SkolenhetInfo")


# ============================================================================
# Planned educations / Utbildningsinfo v3
# ============================================================================


def _platta_planerade(res: dict[str, Any]) -> list[dict[str, Any]]:
    """Plockar ut listan med skolenheter ur planned-educations HAL-svaret."""
    return (
        res.get("body", {})
        .get("_embedded", {})
        .get("listedSchoolUnits", [])
    )


async def sok_planerade_utbildningar(
    kommunkod: str | None = None,
    sida: int = 1,
    per_sida: int = 20,
) -> dict[str, Any]:
    """Söker skolenheter i Utbildningsinfo (planned educations).

    Paginerar på serversidan via API:ets egna `page`/`size`. `kommunkod`
    filtrerar lokalt på `geographicalAreaCode` eftersom API:et inte
    exponerar ett kommunfilter direkt.

    Returnerar ett pagineringspaket där varje datapunkt har bl.a.
    `code`, `name`, `geographicalAreaCode`, `typeOfSchooling`,
    `principalOrganizerType` och `_links` (inkl. `statistics`).
    Serverns egna sidtotaler bifogas som `server_totalt` och
    `server_antal_sidor` när de finns.
    """
    # API:ets sidnummer är 0-baserat; vårt kontrakt är 1-baserat.
    res = await _hamta_json(
        "planned-educations/v3/school-units",
        {"page": max(sida - 1, 0), "size": per_sida},
        accept=_HAL_ACCEPT,
    )
    enheter = _platta_planerade(res)
    if kommunkod is not None:
        enheter = [
            e for e in enheter if e.get("geographicalAreaCode") == kommunkod
        ]
    sidinfo = res.get("body", {}).get("page", {})
    paket = paginera(enheter, sida=1, per_sida=per_sida)
    paket["sida"] = sida
    if sidinfo:
        paket["server_totalt"] = sidinfo.get("totalElements")
        paket["server_antal_sidor"] = sidinfo.get("totalPages")
    return paket


async def hamta_planerad_skolenhet(skolenhetskod: str) -> dict[str, Any]:
    """Hämtar en skolenhets fullständiga Utbildningsinfo-post.

    Innehåller `_links.statistics` som pekar mot statistik-resursen —
    använd `hamta_skolenhet_statistik` för att följa den länken.
    """
    res = await _hamta_json(
        f"planned-educations/v3/school-units/{skolenhetskod}",
        accept=_HAL_ACCEPT,
    )
    return res.get("body", res)


async def hamta_skolenhet_statistik(
    skolenhetskod: str, lasaar: str | None = None
) -> dict[str, Any]:
    """Hämtar konsoliderad statistik per skolform för en skolenhet.

    Statistik-länken på en enhet är en HAL-wrapper med sub-länkar per
    skolform — `gr-statistics` (grundskolan), `fsk-statistics`
    (förskoleklassen), `gy-statistics` (gymnasiet) osv. Vi följer alla
    förekommande sub-länkar och konsoliderar svaret.

    API:et levererar varje mätvärde som en lista av
    `{value, valueType, timePeriod}` — en post per läsår. Skolverket
    håller ett rullande 5-årsfönster: typiskt 2021/22→2025/26 för
    personal/elev-mätvärden, 2020/21→2024/25 för betyg/behörighet.
    Äldre läsår (t.ex. 2013/14) finns INTE i detta API — för det
    krävs Skolverkets arkivpublikationer (Excel/CSV) som måste
    ingestas separat.

    `lasaar` filtrerar tidsperioder vid leverans (t.ex. `"2023/24"`).
    Saknar du `lasaar` får du alla tillgängliga år.

    Returnerar:

        {
          "skolenhetskod": "72627865",
          "skolformer": ["gr", "fsk"],
          "tillgangliga_lasaar": ["2020/21", ..., "2025/26"],
          "lasaarfilter": "2023/24" eller None,
          "statistik": {
            "gr": {<matvarden>},
            "fsk": {<matvarden>}
          }
        }
    """
    enhet = await hamta_planerad_skolenhet(skolenhetskod)
    statistik = enhet.get("_links", {}).get("statistics", {})
    href = statistik.get("href")
    if not href:
        raise ValueError(
            f"skolenhet {skolenhetskod!r} saknar statistics-länk i "
            "Utbildningsinfo — statistik kan vara opublicerad för enheten"
        )

    wrapper = await _hamta_full_url(href, accept=_HAL_ACCEPT)
    sublankar = wrapper.get("body", wrapper).get("_links", {})

    # Plocka skolforms-länkarna (alla utom `self`)
    skolforms_lankar = {
        k.rsplit("-", 1)[0]: v.get("href")
        for k, v in sublankar.items()
        if k.endswith("-statistics") and isinstance(v, dict) and v.get("href")
    }
    if not skolforms_lankar:
        # Vissa enheter har statistik direkt på wrapper-nivå (gymnasiet
        # historiskt) — returnera vad vi har.
        return {
            "skolenhetskod": skolenhetskod,
            "skolformer": [],
            "tillgangliga_lasaar": [],
            "lasaarfilter": lasaar,
            "statistik": wrapper.get("body", wrapper),
        }

    statistik_per_skolform: dict[str, dict[str, Any]] = {}
    alla_lasaar: set[str] = set()
    for skolform, sub_href in skolforms_lankar.items():
        rad = await _hamta_full_url(sub_href, accept=_HAL_ACCEPT)
        body = rad.get("body", rad)
        if lasaar is not None:
            body = _filtrera_pa_lasaar(body, lasaar)
        statistik_per_skolform[skolform] = body
        alla_lasaar.update(_extrahera_lasaar(body))

    return {
        "skolenhetskod": skolenhetskod,
        "skolformer": sorted(skolforms_lankar.keys()),
        "tillgangliga_lasaar": sorted(alla_lasaar),
        "lasaarfilter": lasaar,
        "statistik": statistik_per_skolform,
    }


def _extrahera_lasaar(skolforms_body: dict[str, Any]) -> set[str]:
    """Plockar ut alla unika `timePeriod`-värden ur ett skolformssvar."""
    lasaar: set[str] = set()
    for v in skolforms_body.values():
        if isinstance(v, list):
            for post in v:
                if isinstance(post, dict) and "timePeriod" in post:
                    lasaar.add(post["timePeriod"])
    return lasaar


def _filtrera_pa_lasaar(
    skolforms_body: dict[str, Any], lasaar: str
) -> dict[str, Any]:
    """Behåller bara poster vars `timePeriod` matchar `lasaar`.

    Skalärvärden (`schoolUnit`, `hasLibrary`, `_links`) lämnas orörda.
    Listor av tidsserier filtreras; om alla poster försvinner kvarstår
    en tom lista så strukturen behålls.
    """
    filtrerad: dict[str, Any] = {}
    for nyckel, varde in skolforms_body.items():
        if isinstance(varde, list) and varde and isinstance(varde[0], dict) and "timePeriod" in varde[0]:
            filtrerad[nyckel] = [
                p for p in varde if p.get("timePeriod") == lasaar
            ]
        else:
            filtrerad[nyckel] = varde
    return filtrerad
