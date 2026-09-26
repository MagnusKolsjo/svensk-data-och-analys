# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för ArcGIS REST FeatureServer / MapServer.

Tunn proxy mot `data_och_analys.katalog_klienter.arcgis_features`.
Samma `utan_geometri`-flagga som WFS-routern för Excel-vänlig output.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.katalog_klienter import arcgis_features

router = APIRouter()


@router.get("/instanser")
async def lista_instanser() -> list[dict[str, str]]:
    """Registrerade ArcGIS REST-kataloger (LST publik m.fl.)."""
    return arcgis_features.lista_instanser()


@router.get("/folders")
async def lista_folders(
    instans: str = Query("lst_publik"),
) -> list[str]:
    """Folders i en katalog (LST har 29 — LST, INSPIRE osv)."""
    return await arcgis_features.lista_folders(instans)


@router.get("/services")
async def lista_services(
    instans: str = Query("lst_publik"),
    folder: str | None = Query(None),
) -> list[dict[str, str]]:
    """Services i en folder."""
    return await arcgis_features.lista_services(instans, folder=folder)


@router.get("/layers")
async def lista_layers(
    service_path: str = Query(..., description="folder/service-namn"),
    instans: str = Query("lst_publik"),
    server_typ: str = Query("FeatureServer"),
) -> list[dict[str, Any]]:
    """Lager i en service."""
    return await arcgis_features.lista_layers(
        instans, service_path, server_typ=server_typ
    )


@router.get("/count")
async def hamta_count(
    service_path: str = Query(...),
    layer_id: int = Query(...),
    where: str = Query("1=1"),
    instans: str = Query("lst_publik"),
) -> dict[str, int]:
    """Antalet features som matchar WHERE-satsen."""
    n = await arcgis_features.hamta_count(
        instans, service_path, layer_id, where=where
    )
    return {"count": n}


@router.get("/features")
async def hamta_features(
    service_path: str = Query(...),
    layer_id: int = Query(...),
    where: str = Query("1=1"),
    out_fields: str = Query("*"),
    offset: int = Query(0, ge=0),
    count: int = Query(100, ge=1, le=10000),
    instans: str = Query("lst_publik"),
    out_sr: int = Query(4326),
    utan_geometri: bool = Query(True),
) -> list[dict[str, Any]]:
    """Hämtar features. Default `utan_geometri=true` ger ren property-tabell."""
    res = await arcgis_features.hamta_features(
        instans=instans,
        service_path=service_path,
        layer_id=layer_id,
        where=where,
        out_fields=out_fields,
        offset=offset,
        count=count,
        format="geojson",
        return_geometry=not utan_geometri,
        out_sr=out_sr,
    )
    features = res.get("features", []) or []
    if utan_geometri:
        return [f.get("properties", {}) for f in features]
    return features
