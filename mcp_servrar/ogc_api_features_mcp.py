# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för OGC API Features (OAFeat).

Exponerar den generiska OAFeat-klienten över MCP. En instans hanteras
via `instans`-parametern — Lantmäteriets administrativ-indelning är
baslinje. Andra svenska myndigheter med OAFeat-endpoints läggs in i
`katalog_klienter.ogc_api_features.INSTANSER` när de blir aktuella.

Lantmäteriet kräver API-nyckel (`LANTMATERIET_API_NYCKEL` i `.env`) för
items-endpointen. Nyckeln hämtas från Lantmäteriets API-portal:
`apimanager.lantmateriet.se/devportal` — registrera dig, skapa en
application, prenumerera på OGC-Features API:t.

Verktyg:
    oafeat_lista_instanser
    oafeat_lista_collections
    oafeat_hamta_collection
    oafeat_hamta_items
"""

from __future__ import annotations

# --- PROJEKT_ROT_AUTODISCOVER ----------------------------------------------
# Claude Desktop:s nuvarande version filtrerar bort `env`-block i
# claude_desktop_config.json. Vi hittar projektroten via __file__ istället,
# så ingen PYTHONPATH behöver sättas i konfigurationen.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
# --------------------------------------------------------------------------

import logging
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from data_och_analys.infra.mcp_annotationer import (
    CACHE_HINTAR,
    LASNING_EXTERN,
)
from data_och_analys.infra.mcp_transport import starta
from data_och_analys.katalog_klienter import ogc_api_features

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "oafeat",
    instructions=(
        "MCP-server för OGC API Features — svensk geodata som GeoJSON. "
        "Verktygen har prefixet oafeat_. Baslinjen är Lantmäteriets "
        "administrativa indelning (kommuner, län, rike i senaste version "
        "plus årssnapshots). "
        "OAFEAT OCH WFS ÄR SAMMA DATA I TVÅ GENERATIONER: den här servern "
        "talar den moderna standarden, doa-wfs den äldre. Välj oafeat när "
        "myndigheten finns i båda; WFS täcker fler svenska myndigheter. "
        "API-NYCKEL: landningssidan och collections-listan är öppna, men "
        "oafeat_hamta_items kräver LANTMATERIET_API_NYCKEL i .env. "
        "Saknas den säger felmeddelandet hur man registrerar sig — det är "
        "en konfigurationsfråga, inte saknad data. "
        "ARBETSORDNING: oafeat_lista_collections → oafeat_hamta_collection "
        "(ger bbox, storageCrs och tidsutbredning) → oafeat_hamta_items. "
        "SVARSSTORLEK: administrativa gränser är polygontunga — en enda "
        "kommun kan vara tiotusentals tecken. Håll `limit` lågt och "
        "avgränsa med `bbox` eller `filter` i stället för att hämta en hel "
        "collection."
    ),
    cache_hints=CACHE_HINTAR,
)


@mcp.tool(title="Lista OAFeat-instanser", annotations=LASNING_EXTERN)
async def oafeat_lista_instanser() -> list[dict[str, str]]:
    """Listar OAFeat-instanser i registret.

    Returnerar namn, bas-URL och kort beskrivning per instans.
    """
    return ogc_api_features.lista_instanser()


@mcp.tool(title="Lista collections", annotations=LASNING_EXTERN)
async def oafeat_lista_collections(
    instans: Annotated[
        str, Field(description="Instansnyckel ur oafeat_lista_instanser")
    ] = "lantmateriet_admin",
) -> dict[str, Any]:
    """Listar alla collections (datalager) i en OAFeat-instans.

    Standardinstans är Lantmäteriets administrativa indelning, som har
    9 collections: kommuner, lan, rike — vardera i senaste-version
    samt snapshots för 2025 och 2026.

    Landningssidan och collections-listan är öppna; items-endpointen
    kräver API-nyckel.
    """
    return await ogc_api_features.lista_collections(instans)


@mcp.tool(title="Hämta collection-metadata", annotations=LASNING_EXTERN)
async def oafeat_hamta_collection(
    collection_id: Annotated[
        str, Field(description="Collection-ID ur oafeat_lista_collections")
    ],
    instans: Annotated[
        str, Field(description="Instansnyckel ur oafeat_lista_instanser")
    ] = "lantmateriet_admin",
) -> dict[str, Any]:
    """Returnerar metadata för en specifik collection.

    Inkluderar `extent.spatial.bbox` (geografisk utbredning),
    `storageCrs` (referenssystem), `temporal.interval` (tidsstämplar),
    `links` (åtkomstlänkar för items i olika format).
    """
    return await ogc_api_features.hamta_collection(instans, collection_id)


@mcp.tool(title="Hämta features som GeoJSON", annotations=LASNING_EXTERN)
async def oafeat_hamta_items(
    collection_id: Annotated[
        str, Field(description="Collection-ID ur oafeat_lista_collections")
    ],
    instans: Annotated[
        str, Field(description="Instansnyckel ur oafeat_lista_instanser")
    ] = "lantmateriet_admin",
    bbox: Annotated[
        list[float] | None,
        Field(description="[minx, miny, maxx, maxy] i collectionens CRS"),
    ] = None,
    # Administrativa gränser är polygontunga; taket hindrar att ett anrop
    # spränger klientens svarsgräns.
    limit: Annotated[
        int, Field(description="Antal features per sida", ge=1, le=1000)
    ] = 100,
    offset: Annotated[
        int | None, Field(description="Startindex för sidladdning", ge=0)
    ] = None,
    filter: Annotated[
        str | None, Field(description="CQL2/ECQL-uttryck (instansspecifikt)")
    ] = None,
) -> dict[str, Any]:
    """Hämtar en sida med features ur en collection (GeoJSON).

    Kräver API-nyckel om instansens `api_nyckel_env` är satt och saknas
    från miljön — felmeddelandet pekar då på hur du registrerar dig.

    `bbox` är `[minx, miny, maxx, maxy]` i collection-CRS. `filter` är
    CQL2/ECQL-sträng (instansspecifik). `limit` är sidstorlek.
    """
    bbox_tuple = tuple(bbox) if bbox else None  # type: ignore[arg-type]
    return await ogc_api_features.hamta_items(
        instans=instans,
        collection_id=collection_id,
        bbox=bbox_tuple,
        limit=limit,
        offset=offset,
        filter=filter,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
