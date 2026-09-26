# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för KB:s biblioteksstatistik (Bibstat).

Tunn proxy över `data_och_analys.klienter.kb_bibstat`. Returnerar platta
observationer för Power Query.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import kb_bibstat

router = APIRouter()


@router.get("/observationer")
async def hamta_observationer(
    from_date: str | None = Query(
        None, description="YYYY-MM-DD — bara poster ändrade efter datumet"
    ),
    max_poster: int = Query(2000, ge=1, le=50000),
    sida: int = Query(1, ge=1),
    per_sida: int = Query(2000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    """Biblioteksstatistik-observationer, platta för Excel.

    En rad per bibliotek/undersökningsår med målgrupp och enkätens
    variabelfält. `max_poster` tak mot enorma uttag.
    """
    res = await kb_bibstat.hamta_observationer(
        from_date=from_date, max_poster=max_poster, sida=sida, per_sida=per_sida
    )
    return res.get("datapunkter", [])
