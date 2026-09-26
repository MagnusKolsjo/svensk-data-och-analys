# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Trafikanalys (Trafa).

Tunn proxy över `data_och_analys.klienter.trafa`. `data` returnerar den
plattade radlistan så att Power Query får en tabell direkt.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import trafa

router = APIRouter()


@router.get("/produkter")
async def lista_produkter() -> list[dict[str, Any]]:
    """Trafas statistikprodukter (namn matas till struktur/data)."""
    return await trafa.lista_produkter()


@router.get("/struktur")
async def hamta_struktur(
    query: str | None = Query(None, description="Produktkod, t.ex. t10016"),
) -> dict[str, Any]:
    """Rå strukturkatalog — utforska variabler och mått för en produkt."""
    return await trafa.hamta_struktur(query)


@router.get("/data")
async def hamta_data(
    query: str = Query(..., description="Trafas DSL, t.ex. t10016|ar:2022"),
) -> list[dict[str, Any]]:
    """Datatabell (plattad) för en Trafa-query."""
    res = await trafa.hamta_data(query)
    return res.get("datapunkter", [])
