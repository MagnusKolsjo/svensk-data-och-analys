# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för PxWeb v1 (Jordbruksverket, FoHM, KI, CSN m.fl.).

Tunn proxy mot `data_och_analys.klienter.pxweb_1`. För Excel platt-
formateras datasvaren till en rad per (variabel-kombination, värde).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from data_och_analys.klienter import pxweb_1

router = APIRouter()


@router.get("/myndigheter")
async def lista_myndigheter() -> list[str]:
    """De myndigheter som för närvarande talar PxWeb v1."""
    return pxweb_1.lista_myndigheter()


@router.get("/databaser")
async def lista_databaser(
    myndighet: str = Query(..., description="t.ex. 'jordbruksverket'"),
) -> list[dict[str, Any]]:
    """Databaser hos en myndighet."""
    return [
        d.model_dump(by_alias=True)
        for d in await pxweb_1.lista_databaser(myndighet)
    ]


@router.get("/noder")
async def lista_noder(
    myndighet: str = Query(...),
    sokvag: str = Query(
        "",
        description=(
            "Stigsegment från databas-ID, kommaseparerade. "
            "Tom = lista databaser."
        ),
    ),
) -> list[dict[str, Any]]:
    """Mappar och tabeller på en given stig i tabellträdet."""
    sokvag_lista = pxweb_1.dela_stig(sokvag)
    return [
        n.model_dump(by_alias=True)
        for n in await pxweb_1.lista_noder(myndighet, sokvag_lista)
    ]


@router.get("/metadata")
async def hamta_metadata(
    myndighet: str = Query(...),
    sokvag: str = Query(..., description="Stigsegment separerade med | (komma stöds för äldre länkar), inklusive tabellnamn"),
) -> dict[str, Any]:
    """Variabel- och titelmetadata för en tabell."""
    sokvag_lista = pxweb_1.dela_stig(sokvag)
    metadata = await pxweb_1.hamta_metadata(myndighet, sokvag_lista)
    return metadata.model_dump(by_alias=True)


# ============================================================================
# Datafrågor
# ============================================================================


def _platta_json_stat2(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Packar upp PxWebs json-stat2-respons till en rad per cell.

    json-stat2 är hierarkisk (`dataset.value` är en linjär lista där index
    räknas ut från `dataset.size` och `dataset.id`). Vi packar upp till
    `[{dim1: vardetext, dim2: vardetext, ..., varde: float}, ...]` så att
    Power Query bygger en pivot-bar tabell direkt.
    """
    dataset = data.get("dataset", data)
    dim_ids: list[str] = dataset.get("id", [])
    sizes: list[int] = dataset.get("size", [])
    varden: list[float | None] = dataset.get("value", [])
    dim_meta = dataset.get("dimension", {})

    # Per dimension: lista av (id, etikett) i ordning
    per_dim: list[list[tuple[str, str]]] = []
    for did in dim_ids:
        d = dim_meta.get(did, {})
        kat = d.get("category", {})
        index = kat.get("index", {})
        label = kat.get("label", {})
        # `index` kan vara dict (kategorinamn→position) eller lista (position→kategori)
        if isinstance(index, dict):
            ordered = sorted(index.items(), key=lambda kv: kv[1])
            ids = [k for k, _ in ordered]
        else:
            ids = list(index)
        rader = [(k, label.get(k, k)) for k in ids]
        per_dim.append(rader)

    # Iterera över alla cell-positioner
    if not sizes:
        return []
    rader: list[dict[str, Any]] = []
    # Radmajor enligt json-stat: snabbaste indexet är sist
    multiplikatorer = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        multiplikatorer[i] = multiplikatorer[i + 1] * sizes[i + 1]

    for idx, v in enumerate(varden):
        rad: dict[str, Any] = {}
        kvar = idx
        for d, (did, mult) in enumerate(zip(dim_ids, multiplikatorer)):
            pos = kvar // mult
            kvar = kvar % mult
            kod, etikett = per_dim[d][pos]
            rad[f"{did}_kod"] = kod
            rad[did] = etikett
        rad["varde"] = v
        rader.append(rad)
    return rader


@router.get("/data")
async def hamta_data(
    myndighet: str = Query(...),
    sokvag: str = Query(..., description="Stigsegment separerade med | (komma stöds för äldre länkar), inklusive tabellnamn"),
    query: str = Query(
        ...,
        description=(
            "PxWeb v1 query-DSL som JSON-sträng, t.ex. "
            "'[{\"code\":\"Region\",\"selection\":{\"filter\":\"item\",\"values\":[\"00\"]}}]'"
        ),
    ),
    forhandskontrollera: bool = Query(
        True,
        description=(
            "Hämta metadata först, auto-fyll wildcard för saknade "
            "obligatoriska variabler och avbryt om urvalet överskrider "
            "myndighetens celltak."
        ),
    ),
    platt: bool = Query(
        True,
        description=(
            "True (default): platta json-stat2 till tabellformat "
            "(rad per cell). False: returnera rå json-stat2."
        ),
    ),
) -> list[dict[str, Any]] | dict[str, Any]:
    """Skickar en datafråga och returnerar resultatet."""
    sokvag_lista = pxweb_1.dela_stig(sokvag)
    try:
        query_lista = json.loads(query)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"ogiltig query-JSON: {e}")
    metadata = (
        await pxweb_1.hamta_metadata(myndighet, sokvag_lista)
        if forhandskontrollera
        else None
    )
    res = await pxweb_1.hamta_data(
        myndighet, sokvag_lista, query_lista, metadata=metadata,
    )
    if platt:
        return _platta_json_stat2(res)
    return res
