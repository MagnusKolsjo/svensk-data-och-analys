# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Tunn wrapper runt OAI-PMH-skördaren för researchdata.se (SND).

researchdata.se är SND:s (Svensk nationell datatjänst) nationella katalog
över forskningsdata. Det dedikerade REST-API:et är ännu inte publikt
lanserat; tills dess är OAI-PMH-endpointen den stabila vägen in. Den här
modulen fyller bara i researchdata.se:s bas-URL och delegerar allt
protokollarbete till den generiska `oai_pmh`-klienten.

OAI-PMH-bas-URL:en kan överstyras via `DOA_RESEARCHDATA_OAI_URL` i .env
om SND flyttar endpointen.

Utöver metadata finns ett odokumenterat fil-API för att hämta själva
datafilerna per dataset och version:

    https://api.researchdata.se/dataset/{id}/{version}/file/{filnamn}

`bygg_fil_url` konstruerar den URL:en. Den är odokumenterad och kan kräva
att filnamnet hämtas ur datasetets metadata först; behåll den som hjälpare
snarare än en garanterad nedladdningsväg.

Ingångspunkter:
    identify()
    lista_metadataformat(identifier=None)
    lista_set(resumption_token=None)
    sok_dataset(metadata_prefix="oai_dc", set_spec=None, from_=None, until=None, resumption_token=None)
    lista_dataset_id(...)
    hamta_dataset(identifier, metadata_prefix="oai_dc")
    bygg_fil_url(dataset_id, version, filnamn="")
"""

from __future__ import annotations

import os
from typing import Any

from data_och_analys.katalog_klienter import oai_pmh

# OAI-PMH-endpointen för researchdata.se. Överstyrbar via .env om SND
# flyttar den.
_OAI_URL = "https://api.researchdata.se/oai-pmh"
_FIL_API = "https://api.researchdata.se/dataset"


def _bas_url() -> str:
    return os.getenv("DOA_RESEARCHDATA_OAI_URL", _OAI_URL).strip().rstrip("/")


# ============================================================================
# Metadata via OAI-PMH
# ============================================================================


async def identify() -> dict[str, Any]:
    """Identify mot researchdata.se — repository-metadata."""
    return await oai_pmh.identify(_bas_url())


async def lista_metadataformat(
    identifier: str | None = None,
) -> list[dict[str, Any]]:
    """Tillgängliga metadataformat (t.ex. `oai_dc`, `aocat`)."""
    return await oai_pmh.lista_metadataformat(_bas_url(), identifier=identifier)


async def lista_set(resumption_token: str | None = None) -> dict[str, Any]:
    """Set i katalogen (ämnes-/organisationsindelning)."""
    return await oai_pmh.lista_set(_bas_url(), resumption_token=resumption_token)


async def sok_dataset(
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    from_: str | None = None,
    until: str | None = None,
    resumption_token: str | None = None,
) -> dict[str, Any]:
    """Listar dataset med full metadata, en sida i taget (ListRecords).

    `from_`/`until` är datumavgränsningar (YYYY-MM-DD) — användbart för
    inkrementell skörd. `set_spec` smalnar till ett set. Paginera via
    `resumption_token` i svaret.
    """
    return await oai_pmh.lista_poster(
        _bas_url(),
        metadata_prefix=metadata_prefix,
        set_spec=set_spec,
        from_=from_,
        until=until,
        resumption_token=resumption_token,
    )


async def lista_dataset_id(
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    from_: str | None = None,
    until: str | None = None,
    resumption_token: str | None = None,
) -> dict[str, Any]:
    """Listar bara dataset-headers (id + datestamp) — snabb översikt."""
    return await oai_pmh.lista_identifierare(
        _bas_url(),
        metadata_prefix=metadata_prefix,
        set_spec=set_spec,
        from_=from_,
        until=until,
        resumption_token=resumption_token,
    )


async def hamta_dataset(
    identifier: str, metadata_prefix: str = "oai_dc"
) -> dict[str, Any] | None:
    """Hämtar ett enskilt dataset via OAI-identifierare.

    Identifieraren har formen `oai:researchdata.se:<id>/<version>`
    (ur `lista_dataset_id` eller `sok_dataset`).
    """
    return await oai_pmh.hamta_post(
        _bas_url(), identifier=identifier, metadata_prefix=metadata_prefix
    )


# ============================================================================
# Fil-API (odokumenterat)
# ============================================================================


def bygg_fil_url(dataset_id: str, version: str | int, filnamn: str = "") -> str:
    """Bygger URL till researchdata.se:s odokumenterade fil-API.

    `https://api.researchdata.se/dataset/{id}/{version}/file/{filnamn}` —
    t.ex. `bygg_fil_url("2023-101-1", 1, "zip")`. Filnamnet kan behöva
    hämtas ur datasetets metadata först; URL:en är odokumenterad och kan
    ändras.
    """
    bas = os.getenv("DOA_RESEARCHDATA_FIL_URL", _FIL_API).strip().rstrip("/")
    return f"{bas}/{dataset_id}/{version}/file/{filnamn}".rstrip("/")
