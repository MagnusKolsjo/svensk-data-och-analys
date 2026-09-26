# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för Geodataportalen (GeoNetwork-katalog).

Exponerar Geodataportalen-discovery-klienten över MCP. Sveriges
nationella geodatakatalog (www.geodata.se) aggregerar metadata från
~25 myndigheter — kort sökning här ersätter manuell webbsökning hos
varje enskild myndighet. Sökresultat innehåller länkar till själva
datat (WFS, OGC API Features, STAC, nedladdning) som matas till
respektive hämtningsklient.

Mönstret är samma som Huwise- och PxWeb-servrarna: en server, många
instanser via parameter. Andra svenska eller europeiska GeoNetwork-
instanser läggs in i `katalog_klienter.geodataportalen.INSTANSER` när
de blir aktuella.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.

Verktyg:
    geodata_lista_instanser
    geodata_lista_teman
    geodata_lista_organisationer
    geodata_sok
    geodata_hamta_metadata
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
from data_och_analys.katalog_klienter import geodataportalen

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "geodata",
    instructions=(
        "MCP-server för Geodataportalen (www.geodata.se) — Sveriges "
        "nationella geodatakatalog med metadata från ~25 myndigheter. "
        "Verktygen har prefixet geodata_. "
        "DETTA ÄR EN KATALOG, INTE EN HÄMTARE: verktygen svarar på var "
        "geodata finns, aldrig vad geometrierna innehåller. Kedjan är "
        "geodata_sok → geodata_hamta_metadata → läs `link`-listan → mata "
        "rätt endpoint till doa-wfs, doa-oafeat eller doa-arcgis. Varje "
        "länk bär `protocol` (OGC:WFS, HTTP:OGC:API-Features, OGC:WMS, "
        "HTTP:Nedladdning:API) som avgör vilken server som är rätt. "
        "SÖKNING: `tema` tar ISO 19115-koder på engelska — 'boundaries' "
        "och 'planningCadastre' för administrativa gränser. Hämta listan "
        "med geodata_lista_teman i stället för att gissa koden. "
        "`organisation` filtrerar på publicistnamn ('Lantmäteriet', "
        "'Sjöfartsverket'); geodata_lista_organisationer visar vilka som "
        "faktiskt bidrar och med hur mycket. "
        "Ett kort sök här ersätter manuell webbsökning hos varje enskild "
        "myndighet."
    ),
    cache_hints=CACHE_HINTAR,
)


# ============================================================================
# Verktyg
# ============================================================================


@mcp.tool(title="Lista GeoNetwork-instanser", annotations=LASNING_EXTERN)
async def geodata_lista_instanser() -> list[str]:
    """Listar GeoNetwork-instanser i registret.

    Baslinjen är `geodata` (= www.geodata.se). Fler kan läggas till i
    `katalog_klienter.geodataportalen.INSTANSER` eller överstyras via
    `DOA_GEODATA_<NAMN>_BAS_URL` i .env.
    """
    return geodataportalen.lista_instanser()


@mcp.tool(title="Lista ISO 19115-teman", annotations=LASNING_EXTERN)
async def geodata_lista_teman() -> list[dict[str, str]]:
    """Returnerar ISO 19115-temalistan med svenska benämningar.

    Använd `kod`-värdet (engelska, t.ex. `boundaries`, `planningCadastre`)
    som `tema`-parameter i `geodata_sok`.
    """
    return geodataportalen.lista_teman()


@mcp.tool(title="Lista publicistorganisationer", annotations=LASNING_EXTERN)
async def geodata_lista_organisationer(
    instans: Annotated[
        str, Field(description="Instansnyckel ur geodata_lista_instanser")
    ] = "geodata",
    topp: Annotated[
        int, Field(description="Antal organisationer att returnera", ge=1, le=500)
    ] = 50,
) -> list[dict[str, Any]]:
    """Listar publicistorganisationer rangerade efter antal dataset.

    Användbart för att se vilka myndigheter som bidrar och för att
    smala ner en sökning med `organisation`-parametern.
    """
    return await geodataportalen.lista_organisationer(instans, topp=topp)


@mcp.tool(title="Sök dataset i katalogen", annotations=LASNING_EXTERN)
async def geodata_sok(
    query: Annotated[
        str | None, Field(description="Fritext mot titel och abstrakt")
    ] = None,
    tema: Annotated[
        str | None,
        Field(
            description=(
                "ISO 19115-temakod på engelska ur geodata_lista_teman, "
                "t.ex. 'boundaries'"
            )
        ),
    ] = None,
    organisation: Annotated[
        str | None, Field(description="Publicistnamn, t.ex. 'Lantmäteriet'")
    ] = None,
    instans: Annotated[
        str, Field(description="Instansnyckel ur geodata_lista_instanser")
    ] = "geodata",
    limit: Annotated[int, Field(description="Antal träffar", ge=1, le=100)] = 10,
    offset: Annotated[int, Field(description="Startindex", ge=0)] = 0,
) -> dict[str, Any]:
    """Söker dataset i katalogen.

    `query` är fritext mot titel/abstrakt. `tema` är ett ISO 19115-tema
    (t.ex. "boundaries" eller "planningCadastre" för administrativa
    gränser). `organisation` filtrerar på publicistnamn (t.ex.
    "Lantmäteriet", "Sjöfartsverket", "Trafikverket").

    Returnerar Elasticsearch-rå-svar med `hits.total.value` och en lista
    `hits.hits[].(_source)` per dataset. För full metadata och åtkomst-
    länkar, ta UUID från svaret och kalla `geodata_hamta_metadata`.
    """
    return await geodataportalen.sok(
        instans=instans,
        query=query, tema=tema, organisation=organisation,
        limit=limit, offset=offset,
    )


@mcp.tool(title="Hämta full metadata med åtkomstlänkar", annotations=LASNING_EXTERN)
async def geodata_hamta_metadata(
    uuid: Annotated[str, Field(description="Dataset-UUID ur geodata_sok")],
    instans: Annotated[
        str, Field(description="Instansnyckel ur geodata_lista_instanser")
    ] = "geodata",
) -> dict[str, Any]:
    """Hämtar full metadata för ett dataset.

    Inkluderar `link`-listan med åtkomstlänkar — varje länk har
    `protocol` (t.ex. `HTTP:OGC:API-Features`, `OGC:WFS`, `OGC:WMS`,
    `HTTP:Nedladdning:API`) och `url` (endpoint att hämta från).

    Det är dessa länkar som matas till respektive hämtningsklient
    för att läsa själva geometrierna.
    """
    return await geodataportalen.hamta_metadata(instans, uuid)


# ============================================================================
# Ingångspunkt
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
