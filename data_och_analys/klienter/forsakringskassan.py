# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Försäkringskassans statistik-API (öppna data, JSON).

Försäkringskassan publicerar statistik om sjukpenning, sjuk- och
aktivitetsersättning, föräldrapenning, bostadsbidrag m.m. som öppna data
under `www.forsakringskassan.se/api/sprstatistikrapportera/public/v1/`.
Varje dataset finns som JSON (och XLSX) och har formen:

    [{"dimensions": {"ar": "2025", "lan_kod": "ALL", ...},
      "observations": {"antal_avslag": {"rojd": false, "value": 7804.0},
                       "andel": {"rojd": false, "value": 9.4}}}]

`dimensions` är nedbrytningen (år, kön, län, åldersintervall …) och
`observations` är måtten, vart och ett med `value` och `rojd` (true om
värdet är sekretessröjt/maskerat). Klienten plattar detta till en rad per
post med dimensions- och måttfälten sida vid sida.

API-roten exponerar ingen katalog — dataseten upptäcks via dataportal.se
(DCAT). `lista_dataset` frågar dataportal.se efter Försäkringskassans
JSON-distributioner, så verktyget är självförsörjande utan att man behöver
känna till sökvägarna i förväg.

Ingångspunkter:
    lista_dataset(fritext=None, limit=50)
    hamta_dataset(sokvag_eller_url, sida=1, per_sida=500)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.paginering import paginera
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://www.forsakringskassan.se/api/sprstatistikrapportera/public/v1"
MYNDIGHET = "forsakringskassan"
USER_AGENT = "svensk-data-och-analys/ForsakringskassanClient (+https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Dataportal.se används för dataset-discovery — FK:s API-rot saknar katalog.
_SPARQL_URL = "https://admin.dataportal.se/sparql"


# ============================================================================
# HTTP-anrop
# ============================================================================


async def _hamta_json(url: str) -> Any:
    """GET mot en fullständig URL (JSON-svar)."""
    await hamta_grind(MYNDIGHET).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as klient:
        svar = await klient.get(url)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"FK-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


# ============================================================================
# Discovery via dataportal.se
# ============================================================================


async def lista_dataset(
    fritext: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Listar Försäkringskassans JSON-dataset via dataportal.se.

    Frågar dataportal.se efter dataset publicerade av Försäkringskassan med
    en JSON-distribution. `fritext` filtrerar på titel. Returnerar
    `[{titel, url}]` där `url` matas till `hamta_dataset`.
    """
    titel_filter = (
        f'FILTER(CONTAINS(LCASE(STR(?titel)), LCASE("{fritext}")))'
        if fritext
        else ""
    )
    fraga = f"""
PREFIX dcat: <http://www.w3.org/ns/dcat#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
SELECT DISTINCT ?titel ?url WHERE {{
  ?ds a dcat:Dataset ; dcterms:publisher ?p .
  ?p foaf:name ?n .
  FILTER(CONTAINS(LCASE(STR(?n)), "försäkringskassan"))
  ?ds dcat:distribution ?d .
  ?d dcat:accessURL ?url .
  OPTIONAL {{ ?ds dcterms:title ?titel }}
  FILTER(CONTAINS(STR(?url), ".json"))
  {titel_filter}
}}
LIMIT {int(limit)}
"""
    await hamta_grind(MYNDIGHET).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
    ) as klient:
        svar = await klient.get(_SPARQL_URL, params={"query": fraga})
    svar.raise_for_status()
    bindings = svar.json().get("results", {}).get("bindings", [])
    return [
        {
            "titel": b.get("titel", {}).get("value"),
            "url": b.get("url", {}).get("value"),
        }
        for b in bindings
    ]


# ============================================================================
# Data
# ============================================================================


def platta_poster(poster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plattar FK:s `{dimensions, observations}`-poster till rader.

    Varje post blir en rad med dimensionsfälten plus ett fält per mått
    (måttets `value`). Röjda mått (`rojd=true`) får `None` som värde och
    en `<matt>_rojd`-flagga sätts så att maskeringen syns.
    """
    rader: list[dict[str, Any]] = []
    for post in poster:
        rad: dict[str, Any] = dict(post.get("dimensions", {}))
        for matt, mvarde in post.get("observations", {}).items():
            if isinstance(mvarde, dict):
                rojd = mvarde.get("rojd", False)
                rad[matt] = None if rojd else mvarde.get("value")
                if rojd:
                    rad[f"{matt}_rojd"] = True
            else:
                rad[matt] = mvarde
        rader.append(rad)
    return rader


async def hamta_dataset(
    sokvag_eller_url: str,
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Hämtar och plattar ett FK-dataset.

    `sokvag_eller_url` är antingen en full URL (från `lista_dataset`) eller
    en relativ sökväg under API-roten (t.ex.
    `"sjp-avslag-efter180/SJPAVSLAGEfter180LAN"`). Saknas `.json` läggs det
    till.

    Svaret är paginerat: en rad per post med dimensions- och måttfält. Se
    `platta_poster` för måttmaskeringen (röjda värden blir `None`).
    """
    if sokvag_eller_url.startswith("http"):
        url = sokvag_eller_url
    else:
        url = f"{BAS_URL}/{sokvag_eller_url.lstrip('/')}"
    if not url.endswith(".json"):
        url += ".json"

    poster = await _hamta_json(url)
    rader = platta_poster(poster if isinstance(poster, list) else [poster])
    return paginera(rader, sida=sida, per_sida=per_sida)
