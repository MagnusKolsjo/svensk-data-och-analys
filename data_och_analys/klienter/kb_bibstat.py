# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Kungliga bibliotekets biblioteksstatistik (Bibstat).

KB ansvarar för Sveriges officiella biblioteksstatistik och publicerar den
som öppna data via Bibstat (bibstat.kb.se/data). Datat följer RDF Data Cube
(W3C) och serveras som JSON-LD: en `DataSet` med en `observations`-lista,
paginerad via en `next`-länk (`?limit=&offset=`).

Varje observation beskriver ett biblioteks svar för ett undersökningsår:

    {"@id": "...", "library": {"@id": "...", "name": "Alingsås bibliotek"},
     "sampleYear": 2025, "targetGroup": "Folkbibliotek",
     "Titlar499": 101528, "published": "...", "modified": "..."}

`targetGroup` är bibliotekstyp (Folkbibliotek, Forskningsbibliotek,
Skolbibliotek m.fl.). Utöver de gemensamma fälten bär varje observation
enkätens variabler som egna nycklar (variabelnamnen är Bibstats egna term-
koder). Klienten plattar `library` till namn + id och låter måttfälten stå
kvar som de är.

Datamängden är stor (varje bibliotek × år × variabel), så hämtningen följer
`next`-kedjan upp till ett tak (`max_poster`) och paginerar på vår sida.
`from_date` begränsar till poster ändrade efter ett datum — för inkrementell
skörd.

Ingångspunkter:
    hamta_observationer(from_date=None, max_poster=2000, sida=1, per_sida=500)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.paginering import paginera
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://bibstat.kb.se/data"
MYNDIGHET = "kb_bibstat"
USER_AGENT = "svensk-data-och-analys/KbBibstatClient (+https://github.com/)"
_TIMEOUT = httpx.Timeout(90.0, connect=10.0)

# Backstop mot att följa en oväntat lång next-kedja.
_MAX_SIDOR = 100


# ============================================================================
# HTTP-anrop
# ============================================================================


async def _hamta_json(url: str, parametrar: dict[str, Any] | None = None) -> Any:
    """GET mot en URL med JSON-LD-svar."""
    await hamta_grind(MYNDIGHET).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/ld+json"},
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Bibstat-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


# ============================================================================
# Plattning
# ============================================================================


def _platta_observation(obs: dict[str, Any]) -> dict[str, Any]:
    """Plattar en observation: library -> namn + id, övriga fält behålls.

    JSON-LD-interna nycklar (`@id`, `@type`) släpps utom observationens eget
    `@id` som behålls som `observation_id`. `library`-objektet plattas till
    `bibliotek` (namn) och `bibliotek_id`.
    """
    rad: dict[str, Any] = {}
    for nyckel, varde in obs.items():
        if nyckel == "@id":
            rad["observation_id"] = varde
        elif nyckel == "@type":
            continue
        elif nyckel == "library" and isinstance(varde, dict):
            rad["bibliotek"] = varde.get("name")
            rad["bibliotek_id"] = varde.get("@id")
        else:
            rad[nyckel] = varde
    return rad


# ============================================================================
# Publikt API
# ============================================================================


async def hamta_observationer(
    from_date: str | None = None,
    max_poster: int = 2000,
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Hämtar biblioteksstatistik-observationer (plattade, paginerade).

    Följer Bibstats `next`-kedja tills `max_poster` samlats eller datat tar
    slut, och paginerar resultatet på vår sida. `from_date` (YYYY-MM-DD)
    begränsar till poster ändrade efter datumet — använd för inkrementell
    skörd snarare än att läsa hela datasetet varje gång.

    Varje rad har `observation_id`, `bibliotek`, `bibliotek_id`,
    `sampleYear`, `targetGroup`, `published`, `modified` plus enkätens
    variabelfält. `_avbrott` sätts om `max_poster`-taket slog innan datat
    var slut.
    """
    parametrar = {"from_date": from_date} if from_date else None
    samlade: list[dict[str, Any]] = []
    url: str | None = BAS_URL
    sidor = 0
    avbrott: str | None = None

    while url:
        if sidor >= _MAX_SIDOR:
            avbrott = f"max_sidor={_MAX_SIDOR} nått"
            break
        svar = await _hamta_json(url, parametrar if sidor == 0 else None)
        for obs in svar.get("observations", []):
            samlade.append(_platta_observation(obs))
            if len(samlade) >= max_poster:
                break
        sidor += 1
        if len(samlade) >= max_poster:
            avbrott = f"max_poster={max_poster} nått — fler poster finns"
            break
        # `next` är relativ (`?limit=&offset=`) — slå ihop med bas-URL:en.
        nasta = svar.get("next")
        url = f"{BAS_URL}{nasta}" if nasta else None

    paket = paginera(samlade, sida=sida, per_sida=per_sida)
    paket["_sidor_hamtade"] = sidor
    paket["_avbrott"] = avbrott
    return paket
