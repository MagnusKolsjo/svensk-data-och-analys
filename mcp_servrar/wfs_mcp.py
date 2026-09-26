# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för OGC Web Feature Service (WFS).

Exponerar den generiska WFS-klienten över MCP. SCB:s öppna geodata är
baslinje (`scb_stat` med 49 lager). Fler myndigheters WFS-endpoints läggs
in i `katalog_klienter.wfs.INSTANSER` när de blir aktuella.

WFS är OGC:s äldre standard — moderna myndigheter publicerar parallellt
via OGC API Features (hanteras av `doa-oafeat`-servern). WFS täcker dock
bredare i Sverige: SCB, SMHI, Trafikverket, Naturvårdsverket m.fl.

Verktyg:
    wfs_lista_instanser
    wfs_lista_lager
    wfs_hamta_features
"""

from __future__ import annotations

# --- PROJEKT_ROT_AUTODISCOVER ----------------------------------------------
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
from data_och_analys.katalog_klienter import wfs

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "wfs",
    instructions=(
        "MCP-server för OGC Web Feature Service — svensk geodata som "
        "GeoJSON. Verktygen har prefixet wfs_. "
        "WFS OCH OGC API FEATURES ÄR SAMMA DATA I TVÅ GENERATIONER: den "
        "här servern talar den äldre WFS-standarden, doa-oafeat talar den "
        "moderna. WFS täcker bredare i Sverige (SCB, SMHI, Trafikverket, "
        "Naturvårdsverket); välj oafeat när myndigheten finns i båda. "
        "ARBETSORDNING: wfs_lista_instanser → wfs_lista_lager → "
        "wfs_hamta_features. Lagernamnet i `type_name` måste vara det "
        "fullständiga namnet med workspace, t.ex. 'stat:DeSO_2025' — det "
        "kommer ur wfs_lista_lager, gissa det aldrig. "
        "REFERENSSYSTEM: EPSG:3006 (SWEREF99 TM) är svensk geodatas "
        "original. EPSG:4326 (WGS84) reprojiceras av servern på vägen och "
        "är rätt val för PostGIS-import och kartvisning. "
        "SVARSSTORLEK: GeoJSON med polygongeometri är tungt — en enda "
        "kommungräns kan vara tiotusentals tecken. Håll `count` lågt och "
        "avgränsa med `bbox` eller `cql_filter` i stället för att hämta "
        "ett helt lager. Sidladda med `start_index`."
    ),
    cache_hints=CACHE_HINTAR,
)


@mcp.tool(title="Lista WFS-tjänster", annotations=LASNING_EXTERN)
async def wfs_lista_instanser() -> list[dict[str, str]]:
    """Listar registrerade WFS-tjänster.

    Baslinjen är `scb_stat` (SCB:s öppna geodata). Fler instanser läggs
    till i `katalog_klienter.wfs.INSTANSER` eller överstyras via
    `DOA_WFS_<NAMN>_BAS_URL` i .env.
    """
    return wfs.lista_instanser()


@mcp.tool(title="Lista lager i en WFS-tjänst", annotations=LASNING_EXTERN)
async def wfs_lista_lager(
    instans: Annotated[
        str, Field(description="Instansnyckel ur wfs_lista_instanser")
    ] = "scb_stat",
) -> list[dict[str, str]]:
    """Listar alla FeatureType (lager) som tjänsten erbjuder.

    Varje lager har `name` (det som används som `type_name` i hämtningar),
    `titel`, `abstrakt` och `default_crs` (originalreferenssystem).
    """
    return await wfs.lista_lager(instans)


@mcp.tool(title="Hämta features som GeoJSON", annotations=LASNING_EXTERN)
async def wfs_hamta_features(
    type_name: Annotated[
        str,
        Field(
            description=(
                "Fullständigt lagernamn med workspace ur wfs_lista_lager, "
                "t.ex. 'stat:DeSO_2025'"
            )
        ),
    ],
    instans: Annotated[
        str, Field(description="Instansnyckel ur wfs_lista_instanser")
    ] = "scb_stat",
    bbox: Annotated[
        list[float] | None,
        Field(description="[minx, miny, maxx, maxy] uttryckt i målets srs"),
    ] = None,
    # Polygontunga lager kan ge 5–50 KB per feature. Taket hindrar att
    # ett enda anrop spränger klientens svarsgräns.
    count: Annotated[
        int, Field(description="Antal features per sida", ge=1, le=1000)
    ] = 100,
    start_index: Annotated[
        int | None, Field(description="0-baserat startindex för sidladdning", ge=0)
    ] = None,
    srs: Annotated[
        str,
        Field(
            description="Målreferenssystem, t.ex. 'EPSG:4326' eller 'EPSG:3006'",
            pattern=r"^EPSG:\d{4,6}$",
        ),
    ] = "EPSG:4326",
    cql_filter: Annotated[
        str | None,
        Field(description="GeoServer CQL-uttryck, t.ex. \"lanskod='01'\""),
    ] = None,
) -> dict[str, Any]:
    """Hämtar en sida med features ur ett lager (GeoJSON).

    `type_name` är fullständigt namn med workspace, t.ex. `stat:DeSO_2025`.
    `bbox` är `[minx, miny, maxx, maxy]` i målet `srs`. `cql_filter` är
    GeoServers CQL-syntax (t.ex. `lanskod='01'` för Stockholm).

    `srs=EPSG:4326` (WGS84) reprojicerar servern på vägen — perfekt för
    PostGIS-import. För att hämta i originalreferenssystemet använd
    `EPSG:3006` (SWEREF99 TM).
    """
    bbox_t = tuple(bbox) if bbox else None  # type: ignore[arg-type]
    return await wfs.hamta_features(
        instans=instans,
        type_name=type_name,
        bbox=bbox_t,
        count=count,
        start_index=start_index,
        srs=srs,
        cql_filter=cql_filter,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
