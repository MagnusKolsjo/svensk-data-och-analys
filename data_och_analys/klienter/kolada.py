# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Koladas REST-API v3 (kommunal och regional statistik).

Kolada är RKA:s (Rådet för främjande av kommunala analyser, ägt av SKR
och staten) databas för kommun- och regionstatistik. ~6 000 KPI:er
aggregeras från SCB, Försäkringskassan, Socialstyrelsen, Skolverket m.fl.
och rapporteras per kommun, region och organisationsenhet (skola,
äldreboende osv).

API:t är öppet utan auth. Endpoints (alla returnerar JSON med
`{values, next_url, previous_url, count}`-omslag):

    GET /v3/kpi[?title=...]                              -> lista KPI:er
    GET /v3/kpi/{id}                                     -> en KPI
    GET /v3/kpi_groups[?title=...]                       -> KPI-grupper
    GET /v3/municipality                                 -> kommuner och regioner
    GET /v3/ou                                           -> organisationsenheter
    GET /v3/data/kpi/{ids}/municipality/{ids}/year/{}    -> data

Datapunkter har formen `{kpi, municipality, period, values: [{gender, value, status}]}`
där gender är `T` (total), `M` (män) eller `K` (kvinnor) och status anger
preliminärt eller saknat värde.

Ingångspunkter:
    sok_kpier(title=None, per_page=20)
    hamta_kpi(kpi_id)
    lista_kpi_grupper(title=None)
    hamta_data(kpi_id, municipality_id=None, year=None)
    lista_kommuner()
    lista_organisationsenheter(kpi_id=None, municipality_id=None)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://api.kolada.se/v3"
MYNDIGHET = "kolada"
USER_AGENT = "svensk-data-och-analys/KoladaClient (+https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# ============================================================================
# HTTP-anrop
# ============================================================================


async def _hamta_json(
    sokvag: str, parametrar: dict[str, Any] | None = None
) -> Any:
    """GET mot en relativ sökväg under BAS_URL."""
    url = f"{BAS_URL}/{sokvag.lstrip('/')}"
    return await _hamta_full_url(url, parametrar)


async def _hamta_full_url(
    url: str, parametrar: dict[str, Any] | None = None
) -> Any:
    """GET mot en fullständig URL — används för Koladas `next_url`-kedja."""
    await hamta_grind(MYNDIGHET).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Kolada-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    data = svar.json()
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"Kolada-API-fel: {data['error']}")
    return data


def _csv(varde: str | int | list[str | int]) -> str:
    """Konverterar enskilt värde eller lista till kommaseparerad sträng."""
    if isinstance(varde, (list, tuple)):
        return ",".join(str(v) for v in varde)
    return str(varde)


# ============================================================================
# Publikt API — KPI:er
# ============================================================================


async def sok_kpier(
    title: str | None = None,
    per_page: int = 20,
    page: int = 1,
) -> dict[str, Any]:
    """Söker KPI:er (eller listar alla om `title` är None).

    Returnerar Koladas rå-svar med `values`, `next_url`, `count`.
    Använd `next_url` för paginering om träffmängden är stor.
    """
    parametrar: dict[str, Any] = {"per_page": per_page, "page": page}
    if title:
        parametrar["title"] = title
    return await _hamta_json("kpi", parametrar)


async def hamta_kpi(kpi_id: str) -> dict[str, Any] | None:
    """Returnerar full metadata för en specifik KPI."""
    res = await _hamta_json(f"kpi/{kpi_id}")
    varden = res.get("values", [])
    return varden[0] if varden else None


async def lista_kpi_grupper(
    title: str | None = None, per_page: int = 50, page: int = 1
) -> dict[str, Any]:
    """Listar KPI-grupper (tematiska samlingar av KPI:er)."""
    parametrar: dict[str, Any] = {"per_page": per_page, "page": page}
    if title:
        parametrar["title"] = title
    return await _hamta_json("kpi_groups", parametrar)


# ============================================================================
# Publikt API — data
# ============================================================================


def platta_datapunkter(
    raw: dict[str, Any], kon: str | None = None
) -> list[dict[str, Any]]:
    """Plattar Koladas nestade datasvar till en rad per (kpi, kommun, år, kön).

    Kolada returnerar `{values: [{kpi, municipality, period,
    values: [{gender, value, status, count?}]}]}` — vi packar upp till

        [{"kpi", "kommun", "ar", "kon", "varde", "status", "count"}]

    så Power Query och MCP-svar slipper packa upp wrappern. `kon`
    filtrerar på `T` (total), `M` eller `K` om satt; `None` släpper
    igenom alla.
    """
    if kon is not None:
        kon = kon.upper()
    rader: list[dict[str, Any]] = []
    for d in raw.get("values", []):
        for v in d.get("values", []):
            g = v.get("gender")
            if kon is not None and g != kon:
                continue
            rader.append({
                "kpi": d.get("kpi"),
                "kommun": d.get("municipality"),
                "ar": d.get("period"),
                "kon": g,
                "varde": v.get("value"),
                "status": v.get("status", ""),
                "count": v.get("count"),
            })
    return rader


# Återexportera den delade paginerings-hjälparen så befintliga
# importer (`from data_och_analys.klienter.kolada import paginera`)
# fortsätter fungera. Det är samma funktion — kolada-modulen äger
# inte längre den lokalt.
from data_och_analys.infra.paginering import paginera  # noqa: E402,F401


# Säkerhetstak på `hamta_data_alla` — antal sidor och rader.
# En kommunal Kolada-fråga med ~312 enheter × 25 år × 3 kön ger ~23 000
# datapunkter. Kolada paginerar ~5 000 per sida, så ~5 sidor räcker.
# Taken är en backstop mot oavsiktlig oändlig kedja, inte ett
# normalfall.
_MAX_SIDOR_DEFAULT = 50
_MAX_RADER_DEFAULT = 250_000


async def _hamta_data_endpoint(sokvag: str) -> dict[str, Any]:
    """Internt: hämtar en enda sida från en Kolada data-endpoint."""
    return await _hamta_json(sokvag)


async def _folj_next_url(
    forsta_sida: dict[str, Any],
    max_sidor: int = _MAX_SIDOR_DEFAULT,
    max_rader: int = _MAX_RADER_DEFAULT,
) -> dict[str, Any]:
    """Följer Koladas `next_url`-kedja och slår ihop alla `values`.

    Returnerar ett enkelt svar med en samlad `values`-lista plus
    `_sidor_hamtade` och `_avbrott` (om något säkerhetstak slog).
    """
    samlade: list[Any] = list(forsta_sida.get("values", []))
    next_url = forsta_sida.get("next_url")
    sidor = 1
    avbrott: str | None = None
    while next_url:
        if sidor >= max_sidor:
            avbrott = f"max_sidor={max_sidor} nått — fler sidor finns"
            break
        if len(samlade) >= max_rader:
            avbrott = f"max_rader={max_rader} nått — fler rader finns"
            break
        sida = await _hamta_full_url(next_url)
        samlade.extend(sida.get("values", []))
        next_url = sida.get("next_url")
        sidor += 1
    return {
        "values": samlade,
        "_sidor_hamtade": sidor,
        "_avbrott": avbrott,
    }


# Kolada v3 tar max 25 enheter per data-anrop.
_KOMMUN_CHUNK = 25
_enhetsid_cache: list[str] | None = None


async def _alla_enhetsider() -> list[str]:
    """Returnerar (och cachar) alla kommun- och region-id:n från Kolada.

    Kolada v3 kräver minst en kommun i data-sökvägen. För ett bart KPI-anrop
    fyller vi i samtliga enheter. Listan ändras sällan, så den cachas per
    process.
    """
    global _enhetsid_cache
    if _enhetsid_cache is None:
        res = await lista_kommuner()
        _enhetsid_cache = [m["id"] for m in res.get("values", []) if m.get("id")]
    return _enhetsid_cache


async def hamta_data(
    kpi_id: str | list[str],
    municipality_id: str | list[str] | None = None,
    year: int | list[int] | None = None,
) -> dict[str, Any]:
    """Hämtar datapunkter för KPI:er, kommuner och år (en sida).

    `kpi_id` är obligatoriskt — en eller flera. `municipality_id` är
    SCB:s 4-siffriga kommunkoder eller region-id:n (`1380` Halmstad,
    `01` Region Stockholm osv). `year` är ett eller flera årtal.

    Saknas `municipality_id` returneras alla kommuner och regioner.
    Saknas `year` returneras alla tillgängliga år.

    Returnerar Koladas råa svar inklusive `next_url` när det finns
    fler sidor. För frågor där hela datasetet behövs, använd
    `hamta_data_alla` som följer kedjan.

    Datapunkter har formen `{kpi, municipality, period, values: [...]}`
    där varje value har `{gender, value, status}`. Gender är `T`
    (total), `M` (män) eller `K` (kvinnor).
    """
    # Kolada v3 kräver kommun i sökvägen (`data/kpi/{id}` ensamt ger 404) och
    # tar max 25 kommuner per anrop. Det här är enkelanropet — anroparen
    # ansvarar för ≤25 enheter. Använd `hamta_data_alla` för "alla kommuner";
    # den defaultar och chunkar.
    if municipality_id is None:
        raise ValueError(
            "Kolada v3 kräver kommun(er) — ange municipality_id (≤25) eller "
            "använd hamta_data_alla som hämtar samtliga i bitar."
        )
    sokvag = f"data/kpi/{_csv(kpi_id)}/municipality/{_csv(municipality_id)}"
    if year is not None:
        sokvag += f"/year/{_csv(year)}"
    return await _hamta_data_endpoint(sokvag)


async def hamta_data_alla(
    kpi_id: str | list[str],
    municipality_id: str | list[str] | None = None,
    year: int | list[int] | None = None,
    max_sidor: int = _MAX_SIDOR_DEFAULT,
    max_rader: int = _MAX_RADER_DEFAULT,
) -> dict[str, Any]:
    """Som `hamta_data` men följer Koladas `next_url`-kedja till slutet.

    Kolada paginerar datasvar — typiskt ~5 000 datapunkter per sida.
    Stora longitudinella frågor (alla kommuner × många år × könsdelat)
    sträcker sig över flera sidor och `hamta_data` ger bara den första
    om man inte själv följer `next_url`. Den här funktionen följer
    kedjan tills den är slut eller ett säkerhetstak slår.

    Saknas `municipality_id` hämtas samtliga kommuner och regioner. Eftersom
    Kolada v3 tar max 25 enheter per anrop delas listan i bitar om 25 som
    hämtas i tur och ordning och slås ihop. `max_sidor`/`max_rader` är
    backstops mot oavsiktlig oändlig kedja; `_avbrott` sätts om ett tak slår.
    """
    if municipality_id is None:
        enheter = await _alla_enhetsider()
    elif isinstance(municipality_id, str):
        enheter = [municipality_id]
    else:
        enheter = list(municipality_id)

    samlade: list[Any] = []
    sidor = 0
    avbrott: str | None = None
    for i in range(0, len(enheter), _KOMMUN_CHUNK):
        grupp = enheter[i:i + _KOMMUN_CHUNK]
        forsta = await hamta_data(kpi_id=kpi_id, municipality_id=grupp, year=year)
        kedja = await _folj_next_url(
            forsta, max_sidor=max_sidor, max_rader=max_rader - len(samlade)
        )
        samlade.extend(kedja.get("values", []))
        sidor += kedja.get("_sidor_hamtade", 1)
        if kedja.get("_avbrott") or len(samlade) >= max_rader:
            avbrott = kedja.get("_avbrott") or f"max_rader={max_rader} nått"
            break
    return {"values": samlade, "_sidor_hamtade": sidor, "_avbrott": avbrott}


async def hamta_data_for_kommun(
    municipality_id: str,
    year: int | list[int] | None = None,
) -> dict[str, Any]:
    """Variant som returnerar alla KPI:er för en kommun (en sida).

    Användbar för att få en bredsida av kommunens nyckeltal. Eftersom
    Kolada har ~6 000 KPI:er kan svaret bli stort utan `year`-filter
    — använd `hamta_data_for_kommun_alla` för att följa hela kedjan.
    """
    sokvag = f"data/municipality/{municipality_id}"
    if year is not None:
        sokvag += f"/year/{_csv(year)}"
    return await _hamta_data_endpoint(sokvag)


async def hamta_data_for_kommun_alla(
    municipality_id: str,
    year: int | list[int] | None = None,
    max_sidor: int = _MAX_SIDOR_DEFAULT,
    max_rader: int = _MAX_RADER_DEFAULT,
) -> dict[str, Any]:
    """Som `hamta_data_for_kommun` men följer hela `next_url`-kedjan."""
    forsta = await hamta_data_for_kommun(
        municipality_id=municipality_id, year=year
    )
    return await _folj_next_url(forsta, max_sidor=max_sidor, max_rader=max_rader)


# ============================================================================
# Publikt API — kommuner / OU
# ============================================================================


async def lista_kommuner(per_page: int = 500) -> dict[str, Any]:
    """Returnerar Koladas kommun- och regionlista med id, titel och typ.

    Typ är `K` (kommun) eller `L` (landsting/region). Vi har samma koder
    lokalt i `kommun`- och `region`-tabellerna; denna funktion är mest
    för verifiering eller utforskning av Koladas namngivning.
    """
    return await _hamta_json("municipality", {"per_page": per_page})


async def lista_organisationsenheter(
    kpi_id: str | None = None,
    municipality_id: str | None = None,
    per_page: int = 100,
    page: int = 1,
) -> dict[str, Any]:
    """Listar organisationsenheter (skolor, äldreboende osv).

    OU:er är finare granularitet än kommun — KPI:er med
    `has_ou_data: true` rapporteras per enhet. Filtrera via `kpi_id`
    eller `municipality_id` för att smalna ner.
    """
    parametrar: dict[str, Any] = {"per_page": per_page, "page": page}
    if kpi_id:
        parametrar["kpi"] = kpi_id
    if municipality_id:
        parametrar["municipality"] = municipality_id
    return await _hamta_json("ou", parametrar)
