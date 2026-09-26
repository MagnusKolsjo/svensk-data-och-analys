# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för Socialstyrelsens statistikdatabas (SDB).

Tunn proxy över `data_och_analys.klienter.socialstyrelsen`. `resultat`
returnerar platta rader för Excel; urvalet anges som kommaseparerade
värden per dimension via upprepade query-parametrar.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from data_och_analys.klienter import socialstyrelsen

router = APIRouter()


@router.get("/databaser")
async def lista_databaser() -> list[dict[str, Any]]:
    """SDB:s databaser (namn matas till övriga endpoints)."""
    return await socialstyrelsen.lista_databaser()


@router.get("/dimensioner/{databas}")
async def hamta_dimensioner(databas: str) -> list[dict[str, Any]]:
    """En databas dimensioner (region, ålder, kön, mått …)."""
    return await socialstyrelsen.hamta_dimensioner(databas)


@router.get("/dimensionsvarden/{databas}/{dimension}")
async def hamta_dimensionsvarden(
    databas: str, dimension: str
) -> list[dict[str, Any]]:
    """Giltiga värden för en dimension ({id, kod, text})."""
    return await socialstyrelsen.hamta_dimensionsvarden(databas, dimension)


@router.get("/resultat/{databas}")
async def hamta_resultat(
    databas: str,
    request: Request,
    sida: int = Query(1, ge=1),
    per_sida: int = Query(1000, ge=1, le=20000),
) -> list[dict[str, Any]]:
    """Dataresultat för ett urval, platt för Excel.

    Urvalet anges som query-parametrar per dimension med kommaseparerade
    id:n — t.ex. `?region=0&ar=2022,2023`. Reserverade parametrar (`sida`,
    `per_sida`) räknas inte som dimensioner.
    """
    reserverade = {"sida", "per_sida"}
    urval: dict[str, Any] = {}
    for nyckel in request.query_params:
        if nyckel in reserverade:
            continue
        varden = request.query_params.getlist(nyckel)
        # platta ut ev. kommaseparerade värden
        platt = [v for grupp in varden for v in grupp.split(",") if v]
        urval[nyckel] = platt if len(platt) > 1 else platt[0]
    res = await socialstyrelsen.hamta_resultat(
        databas, urval, sida=sida, per_sida=per_sida
    )
    return res.get("datapunkter", [])
