# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för PxWebApi 2 (SCB m.fl. som rullat över till v2).

Tunn proxy mot `data_och_analys.klienter.pxweb_2`. Samma platt-format-
behandling som v1-routern så Power Query får tabellanpassad output.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from data_och_analys.klienter import pxweb_2

from api.routers.pxweb_1 import _platta_json_stat2

router = APIRouter()


@router.get("/myndigheter")
async def lista_myndigheter() -> list[str]:
    """Myndigheter som för närvarande betjänas av PxWebApi 2."""
    return pxweb_2.lista_myndigheter()


@router.get("/tabeller")
async def lista_tabeller(
    myndighet: str = Query("scb"),
    query: str | None = Query(None, description="Fritext mot tabellnamn"),
    sprak: str | None = Query(None),
    sida_storlek: int = Query(100, ge=1, le=1000),
    sida: int = Query(1, ge=1),
) -> list[dict[str, Any]]:
    """Söker eller listar tabeller."""
    tabeller = await pxweb_2.lista_tabeller(
        myndighet, query=query, sprak=sprak,
        sida_storlek=sida_storlek, sida=sida,
    )
    return [t.model_dump(by_alias=True) for t in tabeller]


@router.get("/metadata")
async def hamta_metadata(
    tabell_id: str = Query(...),
    myndighet: str = Query("scb"),
    sprak: str | None = Query(None),
    max_varden_per_dim: int | None = Query(
        None,
        description=(
            "Trunkera dimensioner med fler värden än taket. "
            "`truncated` och `total_values` flaggar då den fullständiga "
            "storleken; hela värdelistan hämtas via /dimensionsvarden."
        ),
    ),
) -> dict[str, Any]:
    """Metadata för en tabell — variabler, källa, uppdatering."""
    metadata = await pxweb_2.hamta_metadata(
        myndighet,
        tabell_id,
        sprak=sprak,
        max_varden_per_dim=max_varden_per_dim,
    )
    return metadata.model_dump(by_alias=True)


@router.get("/kodlista")
async def hamta_kodlista(
    codelist: str = Query(..., description="Kodliste-id, t.ex. vs_RegionKommun07"),
    myndighet: str = Query("scb"),
    sprak: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Värden för en kodlista (indelningstyp) — t.ex. bara kommuner.

    Region-dimensionens default-lista blandar Riket/län/kommun. Den här ger
    värdena för en vald typ (kodlistans `id` från metadatans `codelists`).
    """
    varden = await pxweb_2.hamta_kodlista(myndighet, codelist, sprak=sprak)
    return [v.model_dump(by_alias=True) for v in varden]


@router.get("/dimensionsvarden")
async def hamta_dimensionsvarden(
    tabell_id: str = Query(...),
    dimension_id: str = Query(..., description="t.ex. Region, Tid, ContentsCode"),
    myndighet: str = Query("scb"),
    sprak: str | None = Query(None),
    prefix: str | None = Query(
        None,
        description="Filtrera värden vars kod börjar med detta — t.ex. 0123 för Järfälla.",
    ),
    innehaller: str | None = Query(
        None,
        description="Filtrera värden vars etikett innehåller detta (case-insensitivt).",
    ),
    sida: int = Query(1, ge=1),
    sida_storlek: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Slår upp en enskild dimensions värden med filter och paginering.

    Komplement till /metadata när en dimension är så stor att den
    trunkerats. Filtren är AND.
    """
    sida_data = await pxweb_2.hamta_dimensionsvarden(
        myndighet,
        tabell_id,
        dimension_id,
        sprak=sprak,
        prefix=prefix,
        innehaller=innehaller,
        sida=sida,
        sida_storlek=sida_storlek,
    )
    return sida_data.model_dump(by_alias=True)


@router.get("/uppskatta-celler")
async def uppskatta_celler(
    tabell_id: str = Query(...),
    urval: str = Query(..., description="JSON-objekt {variabel_id: [värden]}"),
    myndighet: str = Query("scb"),
    sprak: str | None = Query(None),
) -> dict[str, Any]:
    """Beräknar hur många celler ett urval skulle ge.

    Användbart för att kolla om frågan ryms inom myndighetens celltak
    (SCB: 150 000) innan den skickas.
    """
    try:
        urval_dict = json.loads(urval)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"ogiltig urval-JSON: {e}")
    # Trunkera aggressivt — vi behöver bara `total_values` per dim.
    metadata = await pxweb_2.hamta_metadata(
        myndighet, tabell_id, sprak=sprak, max_varden_per_dim=0
    )
    antal = pxweb_2.uppskatta_celler(metadata, urval_dict)
    from data_och_analys.klienter import myndigheter as reg
    m = reg.hamta(myndighet)
    return {
        "antal": antal,
        "tak": m.max_celler,
        "far_plats": antal <= m.max_celler,
    }


@router.get("/data")
async def hamta_data(
    tabell_id: str = Query(...),
    urval: str = Query(
        ...,
        description=(
            "JSON-objekt {variabel_id: [värden]}, t.ex. "
            "'{\"Region\":[\"00\",\"01\"],\"Tid\":[\"2024\"]}'. "
            "Värdet `[\"*\"]` betyder alla värden."
        ),
    ),
    myndighet: str = Query("scb"),
    sprak: str | None = Query(None),
    forhandskontrollera: bool = Query(
        True,
        description=(
            "Hämta metadata först och avbryta om urvalet överskrider "
            "myndighetens celltak."
        ),
    ),
    platt: bool = Query(
        True,
        description="True (default): platta json-stat2 till tabellformat.",
    ),
) -> list[dict[str, Any]] | dict[str, Any]:
    """Hämtar data från en tabell."""
    try:
        urval_dict = json.loads(urval)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"ogiltig urval-JSON: {e}")
    # Förhandskontrollen behöver inte värdelistorna — bara `total_values`.
    metadata = (
        await pxweb_2.hamta_metadata(
            myndighet, tabell_id, sprak=sprak, max_varden_per_dim=0
        )
        if forhandskontrollera
        else None
    )
    res = await pxweb_2.hamta_data(
        myndighet, tabell_id, urval=urval_dict, sprak=sprak, metadata=metadata,
    )
    if platt:
        return _platta_json_stat2(res)
    return res
