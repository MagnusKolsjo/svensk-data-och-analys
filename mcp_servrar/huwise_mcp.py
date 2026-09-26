# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för Huwise Explore API v2.

Exponerar den generiska Huwise-klienten över MCP. Instansen anges med
`instans`: ett namn ur registret (`huwise` för hub.huwise.com) eller en
adress till en kommunal portal, till exempel https://opendata.umea.se.

Mönstret är identiskt med PxWeb-MCP-servrarna: en server, många instanser
via parameter. Postnummer, andra geografiska indelningar och valfri annan
data på en Huwise-portal nås härifrån.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.

Verktyg:
    huwise_lista_instanser
    huwise_lista_datasets
    huwise_hamta_metadata
    huwise_hamta_records
    huwise_exportera
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
from data_och_analys.katalog_klienter import huwise

logger = logging.getLogger(__name__)

# Bulkexporten kapas här, inte hos källan. Claude Desktops tak för ett
# verktygssvar ligger runt 150 000 tecken; halva det lämnar marginal för
# protokolloverhead och för att modellen ska kunna arbeta vidare med svaret.
STANDARD_MAX_TECKEN = 75_000

mcp = MCPServer(
    "huwise",
    instructions=(
        "MCP-server för Huwise Explore API v2 — postnummer, "
        "geografiska indelningar och annan öppen data. Verktygen har "
        "prefixet huwise_. "
        "EN SERVER, MÅNGA INSTANSER: varje verktyg tar `instans`, antingen "
        "ett namn ur huwise_lista_instanser eller adressen till en portal "
        "(https://opendata.umea.se). Kommunernas portaler hittas via "
        "doa-oppnadata, inte här — gissa aldrig en adress. "
        "DATASET-ID: kan vara '<id>' eller '<id>@<namespace>', t.ex. "
        "'geonames-postal-code@public' för globalt delade dataset. "
        "ODSQL: `where`, `select`, `group_by` och `order_by` följer "
        "Huwise egen dialekt, inte SQL. Strängjämförelser skrivs "
        "med dubbla citattecken: country_code=\"SE\". "
        "VÄLJ RÄTT HÄMTARE: huwise_hamta_records tar max 100 poster per "
        "anrop och är rätt för analys. huwise_exportera är bulk och "
        "kapas vid `max_tecken` — filtrera hellre hårdare med `where` än "
        "att sidladda en hel export genom modellen. Ett kapat svar säger "
        "var det kapades; fortsätt med `fran_tecken`."
    ),
    cache_hints=CACHE_HINTAR,
)


# ============================================================================
# Verktyg
# ============================================================================


@mcp.tool(title="Lista Huwise-instanser", annotations=LASNING_EXTERN)
async def huwise_lista_instanser() -> list[str]:
    """Listar de namngivna Huwise-instanserna.

    `huwise` är hub.huwise.com. `global` är samma katalog på domänen
    data.opendatasoft.com, från tiden då bolaget hette Opendatasoft — den
    är reserven för dataset som ännu inte flyttats till hub.huwise.com.
    En kommunal portal anges i stället med sin adress. Namngivna instanser
    kan överstyras via `DOA_HUWISE_<NAMN>_BAS_URL` i .env.
    """
    return huwise.lista_instanser()


@mcp.tool(title="Sök dataset i instanskatalogen", annotations=LASNING_EXTERN)
async def huwise_lista_datasets(
    instans: Annotated[
        str, Field(description="Namn ur huwise_lista_instanser eller portalens adress")
    ],
    query: Annotated[str | None, Field(description="Fritextsökning")] = None,
    where: Annotated[
        str | None,
        Field(
            description=(
                'ODSQL-uttryck, t.ex. keyword="postal-code" AND country_code="SE"'
            )
        ),
    ] = None,
    limit: Annotated[int, Field(description="Antal träffar", ge=1, le=100)] = 10,
    offset: Annotated[int, Field(description="Startindex", ge=0)] = 0,
) -> dict[str, Any]:
    """Listar (eller söker) datasets i en instans katalog.

    `query` är fritext. `where` är ett ODSQL-uttryck — t.ex.
    `keyword="postal-code" AND country_code="SE"`. Limit max 100.
    Returnerar Huwise rå-svar med `total_count` och `results`.
    """
    return await huwise.lista_datasets(
        instans, query=query, where=where, limit=limit, offset=offset
    )


@mcp.tool(title="Hämta datasetmetadata", annotations=LASNING_EXTERN)
async def huwise_hamta_metadata(
    instans: Annotated[
        str, Field(description="Instans-ID ur huwise_lista_instanser")
    ],
    dataset_id: Annotated[
        str, Field(description="'<id>' eller '<id>@<namespace>'")
    ],
) -> dict[str, Any]:
    """Fullständig metadata för ett dataset — fält, källor, lisens.

    `dataset_id` kan vara `<id>` eller `<id>@<namespace>` (t.ex.
    `geonames-postal-code@public` för globalt delat dataset).
    """
    return await huwise.hamta_metadata(instans, dataset_id)


@mcp.tool(title="Hämta dataposter", annotations=LASNING_EXTERN)
async def huwise_hamta_records(
    instans: Annotated[
        str, Field(description="Instans-ID ur huwise_lista_instanser")
    ],
    dataset_id: Annotated[
        str, Field(description="'<id>' eller '<id>@<namespace>'")
    ],
    select: Annotated[
        str | None, Field(description="ODSQL-fältuttryck, kommaseparerat")
    ] = None,
    where: Annotated[str | None, Field(description="ODSQL-filteruttryck")] = None,
    group_by: Annotated[str | None, Field(description="ODSQL-gruppering")] = None,
    order_by: Annotated[str | None, Field(description="ODSQL-sortering")] = None,
    limit: Annotated[
        int, Field(description="Poster per anrop, API:ets tak är 100", ge=1, le=100)
    ] = 100,
    offset: Annotated[int, Field(description="Startindex", ge=0)] = 0,
) -> dict[str, Any]:
    """Hämtar dataposter (max 100 per anrop) med ODSQL-uttryck.

    `select`, `where`, `group_by` och `order_by` följer Huwise
    ODSQL-dialekt. Större mängder hämtas via `huwise_exportera`.
    """
    return await huwise.hamta_records(
        instans, dataset_id,
        select=select, where=where,
        group_by=group_by, order_by=order_by,
        limit=limit, offset=offset,
    )


@mcp.tool(
    title="Bulkexportera dataset",
    annotations=LASNING_EXTERN,
    # En str-retur ger outputSchema {"result": string}, vilket skickar hela
    # exporten en gång som textblock och en gång till i structuredContent.
    # För det här verktyget är dubbleringen skillnaden mellan att rymmas
    # och att spränga svarsgränsen.
    structured_output=False,
)
async def huwise_exportera(
    instans: Annotated[
        str, Field(description="Instans-ID ur huwise_lista_instanser")
    ],
    dataset_id: Annotated[
        str, Field(description="'<id>' eller '<id>@<namespace>'")
    ],
    format: Annotated[
        str, Field(description="Exportformat: json, csv, geojson m.fl.")
    ] = "json",
    where: Annotated[str | None, Field(description="ODSQL-filteruttryck")] = None,
    select: Annotated[
        str | None, Field(description="ODSQL-fältuttryck, kommaseparerat")
    ] = None,
    max_tecken: Annotated[
        int, Field(description="Tak för svarets längd", ge=1_000, le=150_000)
    ] = STANDARD_MAX_TECKEN,
    fran_tecken: Annotated[
        int, Field(description="Läs vidare från denna position", ge=0)
    ] = 0,
) -> str:
    """Bulk-export av ett dataset. Stöder csv, json, geojson m.fl.

    Svaret kapas vid `max_tecken` och avslutas då med en rad
    `[TRUNKERAD vid N av M tecken — fortsätt med fran_tecken=N]`.
    Citera aldrig ur ett kapat svar utan att först ha hämtat resten.

    Filtrera hellre hårdare med `where` än att sidladda en hel export
    genom modellen — för analys är `huwise_hamta_records` nästan alltid
    rätt verktyg.
    """
    bytes_data = await huwise.exportera(
        instans, dataset_id, format=format, where=where, select=select
    )
    text = bytes_data.decode("utf-8", errors="replace")

    totalt = len(text)
    bit = text[fran_tecken : fran_tecken + max_tecken]
    slut = fran_tecken + len(bit)
    if slut < totalt:
        bit += (
            f"\n[TRUNKERAD vid {slut} av {totalt} tecken — "
            f"fortsätt med fran_tecken={slut}]"
        )
    return bit


# ============================================================================
# Ingångspunkt
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
