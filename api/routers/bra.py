# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Brottsförebyggande rådet (Brå), anmälda brott.

Tunn proxy över `data_och_analys.klienter.bra` (SolWebb-skrap). `statistik`
returnerar platta rader; koder anges kommaseparerade. Eftersom varje
kombination kostar en sökrunda mot Brå, håll uttagen små.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.klienter import bra

router = APIRouter()


@router.get("/brottstyper")
async def lista_brottstyper(
    menyid: int = Query(bra.MENYID_KOMMUN_AR),
) -> list[dict[str, Any]]:
    """Valbara brottstyper (kod, namn, niva)."""
    res = await bra.lista_brottstyper(menyid=menyid)
    return res.get("datapunkter", [])


@router.get("/regioner")
async def lista_regioner(
    menyid: int = Query(bra.MENYID_KOMMUN_AR),
) -> list[dict[str, Any]]:
    """Valbara regioner (Brås interna regionkod + namn)."""
    res = await bra.lista_regioner(menyid=menyid)
    return res.get("datapunkter", [])


@router.get("/perioder")
async def lista_perioder(
    menyid: int = Query(bra.MENYID_KOMMUN_AR),
) -> list[dict[str, Any]]:
    """Valbara perioder (kod, år)."""
    return await bra.lista_perioder(menyid=menyid)


@router.get("/statistik")
async def hamta_statistik(
    brottstyp: str = Query(..., description="Brottstypskoder kommaseparerade"),
    region: str = Query(..., description="Regionkoder kommaseparerade"),
    period: str = Query(..., description="Periodkoder kommaseparerade"),
    per_100k: bool = Query(False, description="Antal per 100 000 invånare"),
    menyid: int = Query(bra.MENYID_KOMMUN_AR),
) -> list[dict[str, Any]]:
    """Anmälda brott för brottstyp × region × period, platt för Excel."""
    res = await bra.hamta_statistik(
        brottstyp_koder=[k for k in brottstyp.split(",") if k],
        region_koder=[k for k in region.split(",") if k],
        period_koder=[k for k in period.split(",") if k],
        per_100k=per_100k,
        menyid=menyid,
        per_sida=10000,
    )
    return res.get("datapunkter", [])
