# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Riksbankens öppna SWEA-API (räntor och valutakurser).

Sveriges Riksbank publicerar penningpolitiska och finansiella tidsserier via
SWEA-API:et (api.riksbank.se/swea/v1) — styrränta, marknadsräntor, växel-
kurser m.m. API:et är öppet REST utan auth och returnerar JSON.

Endpoints:
    GET /Series                              -> alla serier (metadata)
    GET /Series/{id}                         -> en serie (metadata)
    GET /Observations/{id}/{från}/{till}     -> [{date, value}]
    GET /CrossRates/{id1}/{id2}/{från}/{till} -> [{date, value}] (korskurs)

Serie-id:n är t.ex. `SECBREPOEFF` (styrränta) och `SEKEURPMI` (EUR-kurs).
Datum anges som YYYY-MM-DD. Serie-metadatan bär `observationMinDate` och
`observationMaxDate`, så ett intervall kan utelämnas och då fylls det i från
seriens egen historik.

Ingångspunkter:
    lista_serier(grupp_id=None, inkludera_stangda=True)
    hamta_serie(serie_id)
    hamta_observationer(serie_id, fran=None, till=None)
    hamta_korsvalutakurs(serie_id_1, serie_id_2, fran, till)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.paginering import paginera
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://api.riksbank.se/swea/v1"
MYNDIGHET = "riksbank"
USER_AGENT = "svensk-data-och-analys/RiksbankenClient (SWEA; +https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# ============================================================================
# HTTP-anrop
# ============================================================================


async def _hamta_json(sokvag: str) -> Any:
    """GET mot en relativ sökväg under BAS_URL."""
    await hamta_grind(MYNDIGHET).vanta()
    url = f"{BAS_URL}/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as klient:
        svar = await klient.get(url)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Riksbank-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


# ============================================================================
# Serier
# ============================================================================


async def lista_serier(
    grupp_id: int | None = None,
    inkludera_stangda: bool = True,
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Listar SWEA-serier med metadata.

    `grupp_id` filtrerar på `groupId` (t.ex. valutagruppen). Sätt
    `inkludera_stangda=False` för att utesluta nedlagda serier
    (`seriesClosed=true`). Varje serie har `seriesId`,
    `shortDescription`, `longDescription`, `groupId`,
    `observationMinDate`/`observationMaxDate`.

    Svaret är paginerat — SWEA har några hundra serier.
    """
    serier = await _hamta_json("Series")
    if grupp_id is not None:
        serier = [s for s in serier if s.get("groupId") == grupp_id]
    if not inkludera_stangda:
        serier = [s for s in serier if not s.get("seriesClosed")]
    return paginera(serier, sida=sida, per_sida=per_sida)


async def hamta_serie(serie_id: str) -> dict[str, Any]:
    """Returnerar metadata för en serie (datumintervall, beskrivning)."""
    return await _hamta_json(f"Series/{serie_id}")


# ============================================================================
# Observationer
# ============================================================================


async def _intervall(
    serie_id: str, fran: str | None, till: str | None
) -> tuple[str, str]:
    """Fyller i saknat datumintervall från seriens egen historik."""
    if fran and till:
        return fran, till
    meta = await hamta_serie(serie_id)
    return (
        fran or meta.get("observationMinDate", "1900-01-01"),
        till or meta.get("observationMaxDate", "2100-01-01"),
    )


async def hamta_observationer(
    serie_id: str,
    fran: str | None = None,
    till: str | None = None,
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Hämtar observationer för en serie inom ett datumintervall.

    `fran`/`till` är YYYY-MM-DD. Utelämnas de fylls de i från seriens
    `observationMinDate`/`observationMaxDate` — observera att hela
    historiken då kan bli många rader (paginerat på vår sida).

    Varje datapunkt är `{date, value}`. Långa dagliga serier (växelkurser
    sedan 1993) sträcker sig över flera sidor; hämta nästa med `sida=2`.
    """
    f, t = await _intervall(serie_id, fran, till)
    obs = await _hamta_json(f"Observations/{serie_id}/{f}/{t}")
    return paginera(obs, sida=sida, per_sida=per_sida)


async def hamta_korsvalutakurs(
    serie_id_1: str,
    serie_id_2: str,
    fran: str,
    till: str,
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Hämtar korsvalutakurs mellan två valutaserier.

    Beräknar kursen mellan två valutor ur deras respektive SEK-serier
    (t.ex. `SEKEURPMI` och `SEKUSDPMI` ger EUR/USD). `fran`/`till` är
    YYYY-MM-DD och obligatoriska. Varje datapunkt är `{date, value}`.
    """
    obs = await _hamta_json(
        f"CrossRates/{serie_id_1}/{serie_id_2}/{fran}/{till}"
    )
    return paginera(obs, sida=sida, per_sida=per_sida)
