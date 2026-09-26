# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Kolada.

Tunn proxy över `data_och_analys.klienter.kolada` (Kolada v3).
Tabellanpassar data-svaret så att Power Query får direkt användbar
struktur — ett flatt record per (kpi, kommun, år, kön) istället för
Koladas nested `{kpi, municipality, period, values: [{gender,...}]}`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import kolada

router = APIRouter()


# ============================================================================
# KPI-metadata
# ============================================================================


@router.get("/kpier")
async def sok_kpier(
    title: str | None = Query(None, description="Fritext mot KPI-titel"),
    per_page: int = Query(20, ge=1, le=200),
    page: int = Query(1, ge=1),
) -> list[dict[str, Any]]:
    """Lista KPI:er, valfritt med fritextsökning.

    Returnerar bara `values`-listan från Koladas svar (paginerat) så
    att Power Query får en tabell direkt utan att packa upp wrappern.
    """
    res = await kolada.sok_kpier(title=title, per_page=per_page, page=page)
    return res.get("values", [])


@router.get("/kpi/{kpi_id}")
async def hamta_kpi(kpi_id: str) -> dict[str, Any] | None:
    """Metadata för en specifik KPI."""
    return await kolada.hamta_kpi(kpi_id)


@router.get("/grupper")
async def lista_grupper(
    title: str | None = None,
    per_page: int = Query(50, ge=1, le=200),
    page: int = Query(1, ge=1),
) -> list[dict[str, Any]]:
    """KPI-grupper (tematiska samlingar)."""
    res = await kolada.lista_kpi_grupper(title=title, per_page=per_page, page=page)
    return res.get("values", [])


# ============================================================================
# Datapunkter (tabellplattat för Excel)
# ============================================================================


@router.get("/data")
async def hamta_data(
    kpi: str = Query(..., description="KPI-id eller flera kommaseparerade"),
    kommun: str | None = Query(
        None, description="Kommunkoder kommaseparerade; alla om utelämnat"
    ),
    ar: str | None = Query(
        None, description="Årtal kommaseparerade; alla om utelämnat"
    ),
    kon: str | None = Query(
        None,
        description=(
            "Filtrera på kön: T (total), M, K. Tomt = alla kön. "
            "Power Query brukar vilja ha alla för pivotering."
        ),
    ),
    folj_paginering: bool = Query(
        True,
        description=(
            "Följ Koladas `next_url`-kedja och returnera hela datasetet "
            "i ett svar. Sätt False bara om Excel ska hämta sida för sida."
        ),
    ),
) -> list[dict[str, Any]]:
    """Datapunkter platta för Excel.

    Kolada returnerar nested struktur — vi packar upp till en rad per
    (kpi, kommun, år, kön) så att Power Query bygger en pivot-bar
    tabell direkt.

    Exempel-svar:
        [{"kpi": "N00945", "kommun": "0180", "ar": 2023, "kon": "T", "varde": 39.3, "status": ""}, ...]
    """
    kpi_lista = [k.strip() for k in kpi.split(",") if k.strip()]
    kommun_lista = (
        [k.strip() for k in kommun.split(",") if k.strip()] if kommun else None
    )
    ar_lista = (
        [int(a.strip()) for a in ar.split(",") if a.strip()] if ar else None
    )
    if folj_paginering:
        res = await kolada.hamta_data_alla(
            kpi_id=kpi_lista, municipality_id=kommun_lista, year=ar_lista
        )
    else:
        res = await kolada.hamta_data(
            kpi_id=kpi_lista, municipality_id=kommun_lista, year=ar_lista
        )
    return kolada.platta_datapunkter(res, kon=kon)


@router.get("/kommun-bredsida/{kommun_kod}")
async def kommun_bredsida(
    kommun_kod: str,
    ar: str | None = Query(None, description="Årtal kommaseparerade"),
    kon: str | None = Query(None, description="T/M/K eller tomt för alla"),
    folj_paginering: bool = Query(
        True, description="Följ Koladas `next_url`-kedja till slutet."
    ),
) -> list[dict[str, Any]]:
    """Alla KPI:er för en kommun ett eller flera år, plattat.

    Användbart i Excel för att bygga en "kommun-profil"-tabell.
    """
    ar_lista = (
        [int(a.strip()) for a in ar.split(",") if a.strip()] if ar else None
    )
    if folj_paginering:
        res = await kolada.hamta_data_for_kommun_alla(
            municipality_id=kommun_kod, year=ar_lista
        )
    else:
        res = await kolada.hamta_data_for_kommun(
            municipality_id=kommun_kod, year=ar_lista
        )
    return kolada.platta_datapunkter(res, kon=kon)


# ============================================================================
# Stöd-listor
# ============================================================================


@router.get("/kommuner")
async def lista_kommuner() -> list[dict[str, Any]]:
    """Koladas kommun- och regionlista (för verifiering av kod/typ)."""
    res = await kolada.lista_kommuner()
    return res.get("values", [])
