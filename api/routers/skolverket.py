# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Skolverket.

Tunn proxy över `data_och_analys.klienter.skolverket`. Listande endpoints
returnerar platta JSON-arrays (paketens `datapunkter`) så att Power Query
bygger tabeller direkt.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import skolverket

router = APIRouter()


@router.get("/skolenheter")
async def lista_skolenheter(
    fritext: str | None = Query(None, description="Matchar skolenhetsnamn"),
    status: str | None = Query(None, description="Aktiv, Vilande eller Planerad"),
    kommunkod: str | None = Query(None, description="SCB:s 4-siffriga kommunkod"),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(500, ge=1, le=10000),
) -> list[dict[str, Any]]:
    """Skolenheter ur registret, filtrerbara och platta för Excel."""
    res = await skolverket.lista_skolenheter(
        fritext=fritext, status=status, kommunkod=kommunkod,
        sida=sida, per_sida=per_sida,
    )
    return res.get("datapunkter", [])


@router.get("/skolenhet/{skolenhetskod}")
async def hamta_skolenhet(skolenhetskod: str) -> dict[str, Any] | None:
    """Fullständig info om en skolenhet (inkl. geokoordinater)."""
    return await skolverket.hamta_skolenhet(skolenhetskod)


@router.get("/planerade")
async def sok_planerade(
    kommunkod: str | None = Query(None),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Skolenheter i Utbildningsinfo (planned educations)."""
    res = await skolverket.sok_planerade_utbildningar(
        kommunkod=kommunkod, sida=sida, per_sida=per_sida
    )
    return res.get("datapunkter", [])


@router.get("/statistik/{skolenhetskod}")
async def hamta_statistik(skolenhetskod: str) -> dict[str, Any]:
    """Statistik för en skolenhet (följer Utbildningsinfos statistics-länk)."""
    return await skolverket.hamta_skolenhet_statistik(skolenhetskod)
