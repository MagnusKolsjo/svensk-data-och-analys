# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Socialstyrelsens statistikdatabas (SDB), REST/JSON.

SDB (sdb.socialstyrelsen.se) publicerar officiell hälso- och
socialtjänststatistik: diagnoser i sluten och öppen vård, läkemedel,
dödsorsaker, amning, DRG m.fl. API:et är öppet utan auth.

API:et är hierarkiskt och självbeskrivande — varje nivå listar nästa:

    GET /api/v1/sv/                          -> databaser [{namn, text}]
    GET /api/v1/sv/{db}                      -> dimensioner [{namn, text, info}]
    GET /api/v1/sv/{db}/{dimension}          -> värden [{id, kod, text}]
    GET /api/v1/sv/{db}/resultat/{dim}/{vals}/{dim}/{vals}/...
                                             -> {data: [{...Id, ar, varde}]}

Resultat-sökvägen varvar dimensionsnamn och valda värde-id:n (komma-
separerade för flera). Exempel:

    resultat/region/0/ar/2022          -> Riket, år 2022, alla övriga dim
    resultat/region/0,1/ar/2022,2023   -> Riket+Stockholm, två år

Databaserna har olika dimensioner (region, ålder, kön, mått, diagnos…),
så de upptäcks via `hamta_dimensioner`/`hamta_dimensionsvarden` innan ett
urval byggs — i linje med projektets regel att verifiera struktur mot
källan i stället för att anta den.

Ingångspunkter:
    lista_databaser()
    hamta_dimensioner(databas)
    hamta_dimensionsvarden(databas, dimension)
    hamta_resultat(databas, urval, sida=1, per_sida=...)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.paginering import paginera
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://sdb.socialstyrelsen.se/api/v1/sv"
MYNDIGHET = "socialstyrelsen"
USER_AGENT = "svensk-data-och-analys/SocialstyrelsenClient (+https://github.com/)"
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
            f"Socialstyrelsen-anrop misslyckades ({svar.status_code}) mot "
            f"{url}: {svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


def _csv(varde: str | int | list[str | int]) -> str:
    """Kommaseparerar enskilt värde eller lista (för fler-valsdimensioner)."""
    if isinstance(varde, (list, tuple)):
        return ",".join(str(v) for v in varde)
    return str(varde)


# ============================================================================
# Katalog — databaser och dimensioner
# ============================================================================


async def lista_databaser() -> list[dict[str, Any]]:
    """Listar SDB:s databaser med `{namn, text}`.

    `namn` (t.ex. `"diagnoserislutenvard"`) är identifieraren som matas
    till övriga funktioner. `text` är den svenska benämningen.
    """
    return await _hamta_json("")


async def hamta_dimensioner(databas: str) -> list[dict[str, Any]]:
    """Listar en databas dimensioner med `{namn, text, info}`.

    Dimensionerna varierar per databas — typiskt `region`, `alder`,
    `kon`, `matt` plus databasspecifika (t.ex. `diagnos`). `info` är en
    HTML-beskrivning av dimensionen. Använd `namn` som nyckel i `urval`
    till `hamta_resultat`.
    """
    return await _hamta_json(databas)


async def hamta_dimensionsvarden(
    databas: str, dimension: str
) -> list[dict[str, Any]]:
    """Listar giltiga värden för en dimension med `{id, kod, text}`.

    `id` är det numeriska id:t som används i resultat-urvalet; `kod` är
    källans egen kod (t.ex. länskod `"01"`); `text` är benämningen
    (t.ex. `"Stockholms län"`).
    """
    return await _hamta_json(f"{databas}/{dimension}")


# ============================================================================
# Resultat — data
# ============================================================================


def _bygg_urvalssokvag(urval: dict[str, str | int | list[str | int]]) -> str:
    """Bygger resultat-sökvägens dimension/värde-segment ur ett urval.

    `{"region": 0, "ar": [2022, 2023]}` blir `region/0/ar/2022,2023`.
    Dimensionsordningen följer dict-ordningen, vilket SDB tål.
    """
    delar: list[str] = []
    for dimension, varden in urval.items():
        delar.append(dimension)
        delar.append(_csv(varden))
    return "/".join(delar)


async def hamta_resultat(
    databas: str,
    urval: dict[str, str | int | list[str | int]],
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Hämtar dataresultat för ett urval och paginerar lokalt.

    `urval` mappar dimensionsnamn till valt id eller en lista av id:n,
    t.ex. `{"region": 0, "ar": [2022, 2023]}`. Dimensioner som utelämnas
    returneras i sin helhet av API:et (alla värden). Slå upp giltiga
    id:n med `hamta_dimensionsvarden`.

    Returnerar ett pagineringspaket där varje datapunkt är en rad ur
    API:ets `data`-lista — typiskt `{regionId, alderId, konId, mattId,
    ar, varde, ...}` plus databasspecifika id-fält (t.ex. `diagnosId`).
    Svaret kan bli stort (alla diagnoser × åldrar × kön), så paginering
    behövs för att hålla MCP-svaret under kanalens 1 MB-cap.
    """
    sokvag = f"{databas}/resultat/{_bygg_urvalssokvag(urval)}"
    res = await _hamta_json(sokvag)
    rader = res.get("data", []) if isinstance(res, dict) else res
    return paginera(rader, sida=sida, per_sida=per_sida)
