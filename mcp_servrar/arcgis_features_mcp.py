# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för ArcGIS REST FeatureServer / MapServer.

Exponerar den generiska ArcGIS-klienten över MCP. Många svenska
myndigheter publicerar geo-data via ESRI-stack istället för OGC-WFS —
Länsstyrelserna är största enskilda källan med 66 services bara i
`LST/`-foldern (EBH-stödet med 85 000+ förorenade områden, miljö-
riskområden, naturvård m.fl.).

Mönstret är samma som de övriga katalogklienterna: ett instansregister
med bas-URL per myndighet, navigering via folder/service/layer.

Verktyg:
    arcgis_lista_instanser
    arcgis_lista_folders
    arcgis_lista_services
    arcgis_lista_layers
    arcgis_hamta_layer_info
    arcgis_hamta_count
    arcgis_hamta_features
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

from data_och_analys.infra import paginering
from data_och_analys.infra.mcp_annotationer import (
    CACHE_HINTAR,
    LASNING_EXTERN,
)
from data_och_analys.infra.mcp_transport import starta
from data_och_analys.infra.paginering import Paket
from data_och_analys.katalog_klienter import arcgis_features

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "arcgis",
    instructions=(
        "MCP-server för ArcGIS REST FeatureServer/MapServer. Verktygen har "
        "prefixet arcgis_. Många svenska myndigheter publicerar geodata via "
        "ESRI-stack i stället för OGC-WFS; Länsstyrelserna är största "
        "enskilda källan (66 services bara i LST-foldern, bl.a. EBH-stödet "
        "med 85 000+ potentiellt förorenade områden). "
        "NAVIGERINGSKEDJA: arcgis_lista_folders → arcgis_lista_services → "
        "arcgis_lista_layers → arcgis_hamta_layer_info → arcgis_hamta_count "
        "→ arcgis_hamta_features. `service_path` är folder plus servicenamn "
        "med snedstreck, t.ex. "
        "'LST/LST_Potentiellt_fororenade_omraden_EBH_EXT' — det kommer ur "
        "arcgis_lista_services, konstruera det aldrig själv. "
        "RÄKNA FÖRE HÄMTNING: arcgis_hamta_count är snabbt och billigt. "
        "Lager med tiotusentals features är vanliga här, så ta reda på "
        "storleken innan du hämtar. "
        "WHERE-SATSER: SQL-liknande syntax med fältnamn exakt som i "
        "arcgis_hamta_layer_info, t.ex. \"Lan='STOCKHOLMS LÄN'\". "
        "SVARSSTORLEK: sätt `out_fields` till en kommaseparerad lista i "
        "stället för '*', och `return_geometry=False` när bara attributen "
        "behövs — geometrin dominerar svaret."
    ),
    cache_hints=CACHE_HINTAR,
)


@mcp.tool(title="Lista ArcGIS-kataloger", annotations=LASNING_EXTERN)
async def arcgis_lista_instanser() -> list[dict[str, str]]:
    """Listar registrerade ArcGIS REST-kataloger.

    Baslinjen är `lst_publik` (Länsstyrelsernas publika geodata).
    Fler instanser läggs till i `katalog_klienter.arcgis_features.INSTANSER`
    eller överstyras via `DOA_ARCGIS_<NAMN>_BAS_URL` i .env.
    """
    return arcgis_features.lista_instanser()


@mcp.tool(title="Lista folders", annotations=LASNING_EXTERN)
async def arcgis_lista_folders(
    instans: Annotated[
        str, Field(description="Instansnyckel ur arcgis_lista_instanser")
    ] = "lst_publik",
) -> list[str]:
    """Returnerar alla folders i instansens rotkatalog.

    Folders grupperar services tematiskt. Länsstyrelsernas katalog har
    29 folders (LST, INSPIRE, kulturmiljö osv).
    """
    return await arcgis_features.lista_folders(instans)


@mcp.tool(title="Lista services i en folder", annotations=LASNING_EXTERN)
async def arcgis_lista_services(
    instans: Annotated[
        str, Field(description="Instansnyckel ur arcgis_lista_instanser")
    ] = "lst_publik",
    folder: Annotated[
        str | None, Field(description="Foldernamn ur arcgis_lista_folders")
    ] = None,
) -> list[dict[str, str]]:
    """Listar services i en folder.

    Varje service har `name` (folder/service) och `type`
    (`FeatureServer`, `MapServer`, `GPServer` osv). FeatureServer är
    vad du oftast vill query:a — det är där features bor.
    """
    return await arcgis_features.lista_services(instans, folder=folder)


@mcp.tool(title="Lista lager i en service", annotations=LASNING_EXTERN)
async def arcgis_lista_layers(
    service_path: Annotated[
        str,
        Field(
            description=(
                "Folder/servicenamn ur arcgis_lista_services, t.ex. "
                "'LST/LST_Potentiellt_fororenade_omraden_EBH_EXT'"
            )
        ),
    ],
    instans: Annotated[
        str, Field(description="Instansnyckel ur arcgis_lista_instanser")
    ] = "lst_publik",
    server_typ: Annotated[
        str, Field(description="'FeatureServer' eller 'MapServer'")
    ] = "FeatureServer",
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[dict[str, Any]]:
    """Listar lager i en service.

    `service_path` är folder + service-namn separerade med `/`, t.ex.
    `LST/LST_Potentiellt_fororenade_omraden_EBH_EXT`.
    """
    rader = await arcgis_features.lista_layers(
        instans, service_path, server_typ=server_typ
    )
    return Paket[dict[str, Any]](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Hämta lagermetadata", annotations=LASNING_EXTERN)
async def arcgis_hamta_layer_info(
    service_path: Annotated[
        str, Field(description="Folder/servicenamn ur arcgis_lista_services")
    ],
    layer_id: Annotated[
        int, Field(description="Lager-ID ur arcgis_lista_layers", ge=0)
    ],
    instans: Annotated[
        str, Field(description="Instansnyckel ur arcgis_lista_instanser")
    ] = "lst_publik",
    server_typ: Annotated[
        str, Field(description="'FeatureServer' eller 'MapServer'")
    ] = "FeatureServer",
) -> dict[str, Any]:
    """Returnerar layer-metadata: fält, geometrityp, extent, capabilities.

    Använd för att utforska vilka attribut ett lager har innan du
    formulerar `where`-satser i `arcgis_hamta_features`.
    """
    return await arcgis_features.hamta_layer_info(
        instans, service_path, layer_id, server_typ=server_typ
    )


@mcp.tool(title="Räkna features före hämtning", annotations=LASNING_EXTERN)
async def arcgis_hamta_count(
    service_path: Annotated[
        str, Field(description="Folder/servicenamn ur arcgis_lista_services")
    ],
    layer_id: Annotated[
        int, Field(description="Lager-ID ur arcgis_lista_layers", ge=0)
    ],
    where: Annotated[
        str,
        Field(
            description=(
                "SQL-liknande filter med fältnamn ur arcgis_hamta_layer_info; "
                "'1=1' betyder alla"
            )
        ),
    ] = "1=1",
    instans: Annotated[
        str, Field(description="Instansnyckel ur arcgis_lista_instanser")
    ] = "lst_publik",
    server_typ: Annotated[
        str, Field(description="'FeatureServer' eller 'MapServer'")
    ] = "FeatureServer",
) -> dict[str, int]:
    """Returnerar antalet features som matchar `where`-satsen.

    Snabbt och billigt — bra för att veta storleken innan man hämtar
    själva datat. Exempel: `where="Lan='STOCKHOLMS LÄN'"` på EBH-lagret.
    """
    n = await arcgis_features.hamta_count(
        instans, service_path, layer_id, where=where, server_typ=server_typ
    )
    return {"count": n}


@mcp.tool(title="Hämta features som GeoJSON", annotations=LASNING_EXTERN)
async def arcgis_hamta_features(
    service_path: Annotated[
        str, Field(description="Folder/servicenamn ur arcgis_lista_services")
    ],
    layer_id: Annotated[
        int, Field(description="Lager-ID ur arcgis_lista_layers", ge=0)
    ],
    where: Annotated[
        str,
        Field(
            description=(
                "SQL-liknande filter med fältnamn ur arcgis_hamta_layer_info; "
                "'1=1' betyder alla"
            )
        ),
    ] = "1=1",
    out_fields: Annotated[
        str,
        Field(
            description=(
                "Kommaseparerade fältnamn, eller '*' för alla — ange en lista "
                "för att hålla svaret litet"
            )
        ),
    ] = "*",
    offset: Annotated[int, Field(description="Startindex", ge=0)] = 0,
    count: Annotated[
        int, Field(description="Antal features per sida", ge=1, le=1000)
    ] = 100,
    instans: Annotated[
        str, Field(description="Instansnyckel ur arcgis_lista_instanser")
    ] = "lst_publik",
    return_geometry: Annotated[
        bool,
        Field(description="False när bara attributen behövs — geometrin dominerar"),
    ] = True,
    out_sr: Annotated[
        int, Field(description="EPSG-kod för svaret, 4326 är WGS84")
    ] = 4326,
    server_typ: Annotated[
        str, Field(description="'FeatureServer' eller 'MapServer'")
    ] = "FeatureServer",
) -> dict[str, Any]:
    """Hämtar en sida med features ur ett lager.

    Returnerar GeoJSON FeatureCollection. `where` är SQL-like-sats —
    fältnamnen exakt som i layer-metadata. `out_fields="*"` ger alla
    attribut; ange kommaseparerad lista för att spara bandbredd.
    `out_sr=4326` reprojicerar serverside till WGS84 (default).
    """
    return await arcgis_features.hamta_features(
        instans=instans,
        service_path=service_path,
        layer_id=layer_id,
        where=where,
        out_fields=out_fields,
        offset=offset,
        count=count,
        format="geojson",
        return_geometry=return_geometry,
        out_sr=out_sr,
        server_typ=server_typ,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
