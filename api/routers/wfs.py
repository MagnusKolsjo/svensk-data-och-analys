# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för WFS-tjänster.

Tunn proxy mot `data_och_analys.katalog_klienter.wfs`. För Excel är
geometrier sällan användbart i en cell — `features`-endpointen har en
flagga som tar bort polygonkoordinaterna och returnerar bara
properties (en flat tabell).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.katalog_klienter import wfs

router = APIRouter()


@router.get("/instanser")
async def lista_instanser() -> list[dict[str, str]]:
    """Registrerade WFS-tjänster (SCB, SMHI, SJV, NV osv)."""
    return wfs.lista_instanser()


@router.get("/lager")
async def lista_lager(
    instans: str = Query("scb_stat", description="Vilken instans"),
) -> list[dict[str, str]]:
    """Alla FeatureType (lager) i en tjänst."""
    return await wfs.lista_lager(instans)


@router.get("/features")
async def hamta_features(
    type_name: str = Query(..., description="t.ex. 'stat:Tatorter_2023'"),
    instans: str = Query("scb_stat"),
    cql_filter: str | None = Query(
        None, description="GeoServer CQL, t.ex. \"kommunnamn='Mora'\""
    ),
    count: int = Query(100, ge=1, le=10000),
    start_index: int = Query(0, ge=0),
    utan_geometri: bool = Query(
        True,
        description=(
            "True (default) tar bort `geometry`-fältet — bra för Excel "
            "där polygoner är onödig last. False ger full GeoJSON."
        ),
    ),
) -> list[dict[str, Any]]:
    """Hämtar features. Returnerar en flat lista av property-objekt
    (eller fulla GeoJSON-features om `utan_geometri=false`).
    """
    res = await wfs.hamta_features(
        instans=instans,
        type_name=type_name,
        count=count,
        start_index=start_index,
        cql_filter=cql_filter,
    )
    features = res.get("features", []) or []
    if utan_geometri:
        return [f.get("properties", {}) for f in features]
    return features
