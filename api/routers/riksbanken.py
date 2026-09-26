# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Riksbanken (SWEA — räntor och valutakurser).

Tunn proxy över `data_och_analys.klienter.riksbanken`. Observationer
returneras som platta `{date, value}`-rader för Power Query.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import riksbanken

router = APIRouter()


@router.get("/serier")
async def lista_serier(
    grupp_id: int | None = Query(None, description="Filtrera på groupId"),
    inkludera_stangda: bool = Query(True),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(500, ge=1, le=5000),
) -> list[dict[str, Any]]:
    """SWEA-serier (seriesId matas till observationer)."""
    res = await riksbanken.lista_serier(
        grupp_id=grupp_id, inkludera_stangda=inkludera_stangda,
        sida=sida, per_sida=per_sida,
    )
    return res.get("datapunkter", [])


@router.get("/serie/{serie_id}")
async def hamta_serie(serie_id: str) -> dict[str, Any]:
    """Metadata för en serie (datumintervall, beskrivning)."""
    return await riksbanken.hamta_serie(serie_id)


@router.get("/observationer/{serie_id}")
async def hamta_observationer(
    serie_id: str,
    fran: str | None = Query(None, description="YYYY-MM-DD"),
    till: str | None = Query(None, description="YYYY-MM-DD"),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(5000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    """Observationer ({date, value}) för en serie inom ett intervall."""
    res = await riksbanken.hamta_observationer(
        serie_id, fran=fran, till=till, sida=sida, per_sida=per_sida
    )
    return res.get("datapunkter", [])


@router.get("/korsvalutakurs")
async def hamta_korsvalutakurs(
    serie_1: str = Query(..., description="T.ex. SEKEURPMI"),
    serie_2: str = Query(..., description="T.ex. SEKUSDPMI"),
    fran: str = Query(..., description="YYYY-MM-DD"),
    till: str = Query(..., description="YYYY-MM-DD"),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(5000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    """Korsvalutakurs mellan två valutaserier ({date, value})."""
    res = await riksbanken.hamta_korsvalutakurs(
        serie_1, serie_2, fran, till, sida=sida, per_sida=per_sida
    )
    return res.get("datapunkter", [])
