# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Försäkringskassan (öppna data, JSON).

Tunn proxy över `data_och_analys.klienter.forsakringskassan`. `data`
returnerar platta rader (dimensioner + mått) för Power Query.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import forsakringskassan

router = APIRouter()


@router.get("/dataset")
async def lista_dataset(
    fritext: str | None = Query(None, description="Matchar dataset-titel"),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """FK:s JSON-dataset via dataportal.se ({titel, url})."""
    return await forsakringskassan.lista_dataset(fritext=fritext, limit=limit)


@router.get("/data")
async def hamta_data(
    sokvag: str = Query(
        ..., description="Full URL eller relativ sökväg till ett FK-dataset"
    ),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(5000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    """Ett FK-dataset plattat till rader (dimensioner + mått)."""
    res = await forsakringskassan.hamta_dataset(
        sokvag, sida=sida, per_sida=per_sida
    )
    return res.get("datapunkter", [])
