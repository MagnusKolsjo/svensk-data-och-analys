# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för researchdata.se (SND, via OAI-PMH).

Tunn proxy över `data_och_analys.katalog_klienter.researchdata`. Returnerar
listan med headers/poster för Power Query; `resumption_token` följer med så
att Excel kan paginera.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.katalog_klienter import researchdata

router = APIRouter()


@router.get("/dataset-id")
async def lista_dataset_id(
    metadata_prefix: str = Query("oai_dc"),
    set_spec: str | None = Query(None),
    from_: str | None = Query(None, alias="from", description="YYYY-MM-DD"),
    until: str | None = Query(None, description="YYYY-MM-DD"),
    resumption_token: str | None = Query(None),
) -> dict[str, Any]:
    """Dataset-headers (id + datestamp) med `resumption_token` för paginering."""
    return await researchdata.lista_dataset_id(
        metadata_prefix=metadata_prefix, set_spec=set_spec,
        from_=from_, until=until, resumption_token=resumption_token,
    )


@router.get("/dataset")
async def sok_dataset(
    metadata_prefix: str = Query("oai_dc"),
    set_spec: str | None = Query(None),
    from_: str | None = Query(None, alias="from", description="YYYY-MM-DD"),
    until: str | None = Query(None, description="YYYY-MM-DD"),
    resumption_token: str | None = Query(None),
) -> dict[str, Any]:
    """Dataset med full metadata (ListRecords) + `resumption_token`."""
    return await researchdata.sok_dataset(
        metadata_prefix=metadata_prefix, set_spec=set_spec,
        from_=from_, until=until, resumption_token=resumption_token,
    )


@router.get("/hamta")
async def hamta_dataset(
    identifier: str = Query(..., description="oai:researchdata.se:<id>/<version>"),
    metadata_prefix: str = Query("oai_dc"),
) -> dict[str, Any] | None:
    """Ett enskilt dataset (GetRecord)."""
    return await researchdata.hamta_dataset(
        identifier, metadata_prefix=metadata_prefix
    )
