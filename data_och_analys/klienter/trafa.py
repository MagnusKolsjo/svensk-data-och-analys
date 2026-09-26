# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Trafikanalys statistik-API (odokumenterat REST, JSON).

Trafikanalys (api.trafa.se) publicerar officiell transportstatistik:
fordonsbestånd, körsträckor, kollektivtrafik, vägtrafikskador, bantrafik,
sjöfart och luftfart. API:et är öppet utan auth men odokumenterat — formen
nedan är härledd genom direkt observation mot den levande tjänsten.

Två endpoints, båda styrda av en `query`-parameter på Trafas egen DSL:

    GET /api/structure[?query=<produkt>]
        Utan query: katalogen av produkter (`StructureItems` med
        `Type:"P"`). Med en produkt i query: produktens variabler och
        mått som barn-`StructureItems`.

    GET /api/data?query=<produkt>|<variabel>:<värde>|<mått>
        Datatabell. `Header.Column` beskriver kolumnerna; `Rows[].Cell`
        bär värdena. Mått-celler har `IsMeasure:true`.

Query-DSL (pipe-separerad):
    "t10016"                      -> produkten Personbilar, alla mått
    "t10016|ar:2022"             -> filtrerat på år 2022
    "t10016|ar:2022,2023|nyreg"  -> två år, ett specifikt mått

Eftersom DSL:en är odokumenterad och varierar per produkt exponerar
klienten en rå strukturpassthrough (`hamta_struktur`) så att variabel-
och måttnamn kan utforskas innan en dataförfrågan byggs — i linje med
projektets regel att aldrig anta endpoint-detaljer från träningsdata.

Trafikanalys uppgår i Tillväxtanalys 2027-01-01; bas-URL kan ändras då.

Ingångspunkter:
    lista_produkter()
    hamta_struktur(query=None)
    hamta_data(query)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

BAS_URL = "https://api.trafa.se/api"
MYNDIGHET = "trafa"
USER_AGENT = "svensk-data-och-analys/TrafaClient (+https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# ============================================================================
# HTTP-anrop
# ============================================================================


async def _hamta_json(sokvag: str, parametrar: dict[str, Any] | None = None) -> Any:
    """GET mot en relativ sökväg under BAS_URL."""
    await hamta_grind(MYNDIGHET).vanta()
    url = f"{BAS_URL}/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Trafa-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


# ============================================================================
# Struktur — produkter, variabler och mått
# ============================================================================


async def hamta_struktur(query: str | None = None) -> dict[str, Any]:
    """Returnerar Trafas råa strukturkatalog (eller en produkts struktur).

    Utan `query` listas alla produkter. Med en produkt i `query`
    (t.ex. `"t10016"`) returneras produktens variabler och mått som
    barn-`StructureItems`. Svaret behålls rått så att hela DSL-vokabuläret
    (namn, typ, beskrivning per nod) är synligt — bygg en dataförfrågan
    utifrån det, gissa inte.
    """
    parametrar = {"query": query} if query else None
    return await _hamta_json("structure", parametrar)


async def lista_produkter() -> list[dict[str, Any]]:
    """Listar Trafas statistikprodukter (förenklat ur strukturkatalogen).

    Returnerar en rad per produkt med `{namn, label, beskrivning,
    aktiv_fran}`. Använd `namn` (t.ex. `"t10016"`) som produktdel i
    `query` till `hamta_struktur` och `hamta_data`.
    """
    res = await hamta_struktur()
    produkter: list[dict[str, Any]] = []
    for item in res.get("StructureItems", []):
        # Type "P" = produkt (Product); övriga typer är variabler/mått.
        if item.get("Type") == "P":
            produkter.append({
                "namn": item.get("Name"),
                "label": item.get("Label"),
                "beskrivning": item.get("Description", ""),
                "aktiv_fran": item.get("ActiveFrom"),
            })
    return produkter


# ============================================================================
# Data
# ============================================================================


def platta_datatabell(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Plattar Trafas `Header`/`Rows`-svar till en lista av rad-dictar.

    Varje rad blir `{kolumnnamn: värde}` med måttkolumnernas numeriska
    värden och dimensionskolumnernas etiketter. Trafa packar varje cell
    som `{Name, Column, Value, FormattedValue, IsMeasure, ...}`; vi
    behåller `Value` (råvärdet) per `Column`.
    """
    rader: list[dict[str, Any]] = []
    for rad in raw.get("Rows", []):
        platt: dict[str, Any] = {}
        for cell in rad.get("Cell", []):
            kolumn = cell.get("Column") or cell.get("Name")
            platt[kolumn] = cell.get("Value")
        rader.append(platt)
    return rader


async def hamta_data(query: str) -> dict[str, Any]:
    """Hämtar en datatabell för en Trafa-query (rå + plattad form).

    `query` är Trafas pipe-separerade DSL, t.ex.
    `"t10016|ar:2022"`. Bygg den utifrån `hamta_struktur` så att
    variabel- och måttnamnen stämmer för produkten.

    Returnerar `{namn, kolumner, datapunkter, fel}` där `kolumner` är
    `Header.Column` (kolumndefinitioner med enhet och datatyp) och
    `datapunkter` är den plattade radlistan. `fel` speglar API:ets
    `Errors`-fält så att en feltolkad query syns.
    """
    raw = await _hamta_json("data", {"query": query})
    return {
        "namn": raw.get("Name"),
        "original_namn": raw.get("OriginalName"),
        "kolumner": raw.get("Header", {}).get("Column", []),
        "datapunkter": platta_datatabell(raw),
        "fel": raw.get("Errors"),
    }
