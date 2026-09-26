# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Huwise Explore API v2 (ODSQL, geo-filtrering).

Huwise är en plattform för publicering av öppna data. Svenska kommuner kör
den på egna domäner — Umeå på opendata.umea.se — och bolaget har en egen
katalog på hub.huwise.com med bland annat Geonames-data.

`instans` är antingen ett namn ur registret nedan eller en instansadress
(https://opendata.umea.se). Adressen är vägen in för kommunernas portaler:
de står i katalogregistret och i DCAT-distributionerna, inte här.
Env-överstyrning per namngiven instans via `DOA_HUWISE_<NAMN>_BAS_URL`.

Huvudoperationer mot Explore API v2:
    GET /api/explore/v2.1/catalog/datasets         -> lista datasets
    GET /api/explore/v2.1/catalog/datasets/{id}    -> metadata
    GET /api/explore/v2.1/catalog/datasets/{id}/records  -> data (max 100/anrop)
    GET /api/explore/v2.1/catalog/datasets/{id}/exports/{format}  -> bulk

ODSQL-parametrar (`where`, `select`, `group_by`, `order_by`) följer
Huwise frågespråk och dokumenteras hos varje instans under
`/api/explore/v2.1/console/`.

Ingångspunkter:
    lista_instanser() -> list[str]
    lista_datasets(instans, query=None, where=None, limit=10, offset=0)
    hamta_metadata(instans, dataset_id)
    hamta_records(instans, dataset_id, **odsql)
    exportera(instans, dataset_id, format="csv", **odsql)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ============================================================================
# Instansregister
# ============================================================================


@dataclass(frozen=True)
class HuwiseInstans:
    namn: str
    bas_url: str


# `hub.huwise.com` är bolagets katalog. `data.opendatasoft.com` är samma
# katalog på domänen från tiden då bolaget hette Opendatasoft. Den används
# som reserv: dataset som ännu inte flyttats till hub.huwise.com finns bara
# där, och den gamla domänen svarar fortfarande.
_BASLINJE: dict[str, HuwiseInstans] = {
    "huwise": HuwiseInstans(
        namn="huwise",
        bas_url="https://hub.huwise.com",
    ),
    "global": HuwiseInstans(
        namn="global",
        bas_url="https://data.opendatasoft.com",
    ),
}


def _med_overstyrning(bas: HuwiseInstans) -> HuwiseInstans:
    """Tillämpar env-överstyrning på en baslinjepost."""
    overstyrd = os.getenv(f"DOA_HUWISE_{bas.namn.upper()}_BAS_URL", "").strip()
    if not overstyrd:
        return bas
    return HuwiseInstans(namn=bas.namn, bas_url=overstyrd.rstrip("/"))


def hamta_instans(namn: str) -> HuwiseInstans:
    """Konfiguration för en namngiven instans, eller en instans ur en adress.

    En adress gör att klienten når vilken Huwise-portal som helst — det är
    så katalogregistret och DCAT-distributionerna pekar ut dem.
    """
    if namn.startswith(("http://", "https://")):
        p = urlparse(namn)
        return HuwiseInstans(namn=p.hostname or namn, bas_url=f"{p.scheme}://{p.netloc}")
    if namn not in _BASLINJE:
        kanda = ", ".join(sorted(_BASLINJE))
        raise ValueError(f"okänd huwise-instans: {namn!r}. Tillgängliga: {kanda}")
    return _med_overstyrning(_BASLINJE[namn])


def lista_instanser() -> list[str]:
    return sorted(_BASLINJE)


# ============================================================================
# Wire-modeller
# ============================================================================


class DatasetSammanfattning(BaseModel):
    """En post ur datasetslistan — id + grundläggande metadata."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dataset_id: str
    titel: str | None = Field(default=None, alias="title")
    records_count: int | None = None
    modified: str | None = None


# ============================================================================
# HTTP-anrop
# ============================================================================

USER_AGENT = (
    "svensk-data-och-analys/HuwiseClient "
    "(Huwise Explore API v2; +https://github.com/)"
)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


async def _hamta_json(
    instans_namn: str, sokvag: str, parametrar: dict[str, Any] | None = None
) -> Any:
    inst = hamta_instans(instans_namn)
    url = f"{inst.bas_url}/api/explore/v2.1/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Huwise-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


async def _hamta_bytes(
    instans_namn: str, sokvag: str, parametrar: dict[str, Any] | None = None
) -> bytes:
    inst = hamta_instans(instans_namn)
    url = f"{inst.bas_url}/api/explore/v2.1/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=300.0, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Huwise-export misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.content


# ============================================================================
# Publikt API
# ============================================================================


async def lista_datasets(
    instans: str,
    query: str | None = None,
    where: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    """Listar (eller söker) datasets i en instans katalog.

    `query` är fritext, `where` är ODSQL-uttryck (t.ex.
    `keyword="postal-code" AND country_code="SE"`). Limit max 100.

    Fritexten skickas som `search(...)` i `where`. Explore API v2.1 har
    ingen `q`-parameter för katalogen — den ignoreras tyst och ger hela
    katalogen tillbaka, oavsett söktext.
    """
    parametrar: dict[str, Any] = {"limit": limit, "offset": offset}
    villkor = []
    if query:
        villkor.append('search("' + query.replace('"', '\\"') + '")')
    if where:
        villkor.append(f"({where})")
    if villkor:
        parametrar["where"] = " AND ".join(villkor)
    return await _hamta_json(instans, "catalog/datasets", parametrar)


async def hamta_metadata(instans: str, dataset_id: str) -> dict[str, Any]:
    """Returnerar fullständig metadata för ett dataset.

    `dataset_id` kan vara på formen `<id>` eller `<id>@<instans-namespace>`
    (t.ex. `geonames-postal-code@public` för det globalt delade datasetet).
    """
    return await _hamta_json(instans, f"catalog/datasets/{dataset_id}")


async def hamta_records(
    instans: str,
    dataset_id: str,
    select: str | None = None,
    where: str | None = None,
    group_by: str | None = None,
    order_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Hämtar dataposter (max 100 per anrop) med ODSQL-uttryck.

    Returnerar API:ets råsvar inklusive `total_count` och `results`.
    """
    parametrar: dict[str, Any] = {"limit": limit, "offset": offset}
    for namn, varde in [
        ("select", select),
        ("where", where),
        ("group_by", group_by),
        ("order_by", order_by),
    ]:
        if varde is not None:
            parametrar[namn] = varde
    return await _hamta_json(
        instans, f"catalog/datasets/{dataset_id}/records", parametrar
    )


async def exportera(
    instans: str,
    dataset_id: str,
    format: str = "csv",
    where: str | None = None,
    select: str | None = None,
    order_by: str | None = None,
) -> bytes:
    """Bulk-export av ett dataset i givet format.

    Stöder bl.a. `csv`, `json`, `geojson`, `parquet`, `xlsx`. Returnerar
    rå-bytes — anroparen avkodar enligt format. Använd när du behöver
    fler än ~10 000 rader; pagination via `hamta_records` blir då onödigt
    många anrop.
    """
    parametrar: dict[str, Any] = {}
    for namn, varde in [
        ("where", where),
        ("select", select),
        ("order_by", order_by),
    ]:
        if varde is not None:
            parametrar[namn] = varde
    return await _hamta_bytes(
        instans, f"catalog/datasets/{dataset_id}/exports/{format}", parametrar
    )
