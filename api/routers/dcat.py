# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för DCAT-discovery mot dataportal.se.

Tunn proxy över `data_och_analys.katalog_klienter.dcat`. Returnerar platta
rader (SPARQL-bindningar) för Power Query.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.katalog_klienter import dcat

router = APIRouter()


@router.get("/sok")
async def sok_dataset(
    fritext: str | None = Query(None, description="Fritext mot titel/beskrivning"),
    limit: int = Query(20, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Dataset i dataportal.se ({dataset, titel, beskrivning, utgivare})."""
    return await dcat.sok_dataset(fritext=fritext, limit=limit)


@router.get("/distributioner")
async def hitta_distributioner(
    url_monster: str = Query(..., description="Delsträng i accessURL, t.ex. PXWeb"),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Dataset vars distributions-accessURL matchar ett mönster."""
    return await dcat.hitta_distributioner_med_url(url_monster, limit=limit)


@router.get("/pxweb-instanser")
async def hitta_pxweb(
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Alla PxWeb-instanser i dataportal.se."""
    return await dcat.hitta_pxweb_instanser(limit=limit)
