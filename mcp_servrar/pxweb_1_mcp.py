# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för PxWeb v1 — alla myndigheter som ännu talar v1.

Exponerar PxWeb v1-klienten över MCP. En enda server hanterar alla
v1-myndigheter via en `myndighet`-parameter — det undviker att spawna
en process per källa och gör jämförelser mellan myndigheter till ett
enskilt tool-anrop.

Vilka myndigheter som syns här styrs av registret i
`data_och_analys.klienter.myndigheter` och dess env-överstyrningar.
När en myndighet rullar över till v2 flyttas den till `pxweb_2_mcp`
genom en ändring av registret eller en .env-rad — ingen kodändring
behövs här.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.
DB-initiering sker i __main__ och servern startar även om DB är nere.

Verktyg:
    pxweb_1_lista_myndigheter
    pxweb_1_lista_databaser
    pxweb_1_lista_noder
    pxweb_1_hamta_metadata
    pxweb_1_uppskatta_celler
    pxweb_1_hamta_data
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

import json
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
from data_och_analys.klienter import myndigheter, pxweb_1

logger = logging.getLogger(__name__)

_PXWEB_INSTRUKTIONER = (
    "ARBETSORDNING: lista_myndigheter → lista_databaser → lista_noder "
    "(upprepas nedåt i trädet) → hamta_metadata → uppskatta_celler → "
    "hamta_data. Hoppa inte över metadatasteget: variabelkoder och "
    "värdelistor skiljer sig mellan myndigheter och årgångar, och ett "
    "tabell-ID ur förhandskunskap är nästan alltid fel. "
    "UPPSKATTA CELLER ÄR INTE VALFRITT: varje myndighet har ett hårt "
    "celltak (SCB 150 000). Överskrids det avvisas frågan hos källan, "
    "inte hos oss, och felet blir svårtolkat. uppskatta_celler svarar "
    "{antal, tak, far_plats} innan något hämtas. "
    "TYST EXPANSION: med forhandskontrollera=True fylls saknade "
    "obligatoriska variabler med wildcard, medan variabler som källan kan "
    "summera över lämnas ute. Det är därför ett urval på tre variabler kan "
    "bli hundratusentals celler — räkna först."
)

mcp = MCPServer(
    "pxweb_1",
    instructions=(
        "MCP-server för myndigheter som ännu talar PxWeb API v1 — "
        "Jordbruksverket, Folkhälsomyndigheten, Konjunkturinstitutet, CSN, "
        "Energimyndigheten, Skogsstyrelsen m.fl. Verktygen har prefixet "
        "pxweb_1_. "
        "VERSIONSREGISTRET AVGÖR VILKEN SERVER SOM GÄLLER: en myndighet "
        "som rullat över till API v2 försvinner ur den här serverns lista "
        "och dyker upp i doa-pxweb-2. Kör pxweb_1_lista_myndigheter först; "
        "får du inte träff, prova doa-pxweb-2 innan du drar slutsatsen att "
        "myndigheten saknas. PxWeb v1 stöds av källorna t.o.m. 2026-12-31. "
        + _PXWEB_INSTRUKTIONER
    ),
    cache_hints=CACHE_HINTAR,
)


# ============================================================================
# Verktyg
# ============================================================================


@mcp.tool(title="Lista myndigheter på PxWeb v1", annotations=LASNING_EXTERN)
async def pxweb_1_lista_myndigheter() -> list[str]:
    """Listar de myndigheter som för närvarande betjänas av PxWeb v1.

    Listan kommer från det centrala registret — en myndighet som rullat
    över till v2 syns inte här utan i `pxweb_2_lista_myndigheter`.
    """
    return pxweb_1.lista_myndigheter()


@mcp.tool(title="Lista databaser hos en myndighet", annotations=LASNING_EXTERN)
async def pxweb_1_lista_databaser(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_1_lista_myndigheter")
    ],
) -> list[dict[str, Any]]:
    """Listar databaser hos en v1-myndighet.

    Många myndigheter har bara en databas, men dess `dbid` behövs ändå
    som första segment i `sokvag` för efterföljande anrop.
    """
    return [
        d.model_dump(by_alias=True)
        for d in await pxweb_1.lista_databaser(myndighet)
    ]


@mcp.tool(title="Lista noder i tabellträdet", annotations=LASNING_EXTERN)
async def pxweb_1_lista_noder(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_1_lista_myndigheter")
    ],
    sokvag: Annotated[
        list[str],
        Field(
            description=(
                "Stig från databas-ID och nedåt, t.ex. ['JO', 'JO0103']. "
                "Tom lista ger trädets rot."
            )
        ),
    ],
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[dict[str, Any]]:
    """Listar mappar och tabeller på en given stig i tabellträdet.

    `sokvag` byggs från databas-ID och vidare nedåt, t.ex.
    `["JO", "JO0103"]` hos Jordbruksverket. `typ="l"` är en undermapp,
    `typ="t"` är en tabell vars metadata kan hämtas med
    `pxweb_1_hamta_metadata`.
    """
    rader = [
        n.model_dump(by_alias=True)
        for n in await pxweb_1.lista_noder(myndighet, sokvag)
    ]
    return Paket[dict[str, Any]](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Hämta tabellmetadata", annotations=LASNING_EXTERN)
async def pxweb_1_hamta_metadata(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_1_lista_myndigheter")
    ],
    tabell_sokvag: Annotated[
        list[str],
        Field(description="Full stig till tabellen (typ='t') ur pxweb_1_lista_noder"),
    ],
) -> dict[str, Any]:
    """Hämtar variabel- och titelmetadata för en tabell.

    Anropas alltid innan `pxweb_1_hamta_data` — variabel-koder och
    värdelistor får aldrig antas från träningsdata.
    """
    metadata = await pxweb_1.hamta_metadata(myndighet, tabell_sokvag)
    return metadata.model_dump(by_alias=True)


@mcp.tool(title="Räkna dataceller före hämtning", annotations=LASNING_EXTERN)
async def pxweb_1_uppskatta_celler(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_1_lista_myndigheter")
    ],
    tabell_sokvag: Annotated[
        list[str], Field(description="Full stig till tabellen ur pxweb_1_lista_noder")
    ],
    query: Annotated[
        list[dict[str, Any]],
        Field(description="Samma query-DSL som pxweb_1_hamta_data tar"),
    ],
) -> dict[str, Any]:
    """Beräknar hur många dataceller ett urval skulle ge.

    Symmetriskt med `pxweb_2_uppskatta_celler`. Variabler som saknas
    i `query` räknas som "alla värden" om de är `elimination=False`
    (de auto-fylls med wildcard innan POST) och som 1 cell om de är
    `elimination=True` (källan summerar). Returnerar
    `{antal, tak, far_plats}`.
    """
    m = myndigheter.hamta(myndighet)
    metadata = await pxweb_1.hamta_metadata(myndighet, tabell_sokvag)
    antal = pxweb_1.uppskatta_celler(metadata, query)
    return {
        "antal": antal,
        "tak": m.max_celler,
        "far_plats": antal <= m.max_celler,
    }


@mcp.tool(title="Hämta data ur en PxWeb v1-tabell", annotations=LASNING_EXTERN)
async def pxweb_1_hamta_data(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_1_lista_myndigheter")
    ],
    tabell_sokvag: Annotated[
        list[str], Field(description="Full stig till tabellen ur pxweb_1_lista_noder")
    ],
    query: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "PxWeb v1 query-DSL: lista av selektorer på formen "
                '{"code": "Region", "selection": {"filter": "item", '
                '"values": ["00"]}}'
            )
        ),
    ],
    format: Annotated[
        str, Field(description="'json-stat2' (standard), 'csv' eller 'px'")
    ] = "json-stat2",
    forhandskontrollera: Annotated[
        bool,
        Field(
            description=(
                "Hämta metadata först, fyll obligatoriska variabler med "
                "wildcard och avvisa frågor över celltaket"
            )
        ),
    ] = True,
    max_tecken: Annotated[
        int,
        Field(
            description="Kapa svaret vid så här många tecken",
            ge=1000, le=120_000,
        ),
    ] = 60_000,
) -> dict[str, Any]:
    """Skickar en datafråga mot en PxWeb v1-tabell.

    `query` är PxWeb v1:s query-DSL — en lista av selektorer på formen:
        {"code": "Region", "selection": {"filter": "item", "values": ["00"]}}

    Med `forhandskontrollera=True` (default) hämtas metadata först och
    klienten gör två saker:

    1. Saknade `elimination=False`-variabler i `query` fylls automatiskt
       med wildcard så källan inte avvisar med "Missing selection for
       mandatory variable". `elimination=True`-variabler som saknas
       lämnas ute så källan summerar över dem.
    2. Cellantalet räknas upp och frågor som överskrider myndighetens
       celltak avvisas med ett tydligt svenskt fel.

    Sätt False bara om du explicit vill skicka en oexpanderad query
    rakt till källan (då gäller källans egen felhantering).

    `format` styr svarsformatet. "json-stat2" är standardvalet och
    håller dimensionsstrukturen läsbar; "csv" och "px" stöds också av
    de flesta installationer.
    """
    metadata = (
        await pxweb_1.hamta_metadata(myndighet, tabell_sokvag)
        if forhandskontrollera
        else None
    )
    svar = await pxweb_1.hamta_data(
        myndighet, tabell_sokvag, query, format=format, metadata=metadata,
    )

    # Cellkontrollen ovan skyddar källan från att avvisa frågan, inte vår
    # kanal från att spränga klientens svarsgräns. En fråga som ryms inom
    # myndighetens celltak — SCB tillåter 150 000 celler — kan ändå ge ett
    # svar på hundratusentals tecken. Kapningen är därför ett eget skydd,
    # inte en dubblering av celltaket.
    text = json.dumps(svar, ensure_ascii=False)
    if len(text) <= max_tecken:
        return svar
    return {
        "trunkerad": True,
        "tecken_totalt": len(text),
        "max_tecken": max_tecken,
        "rad": (
            f"Svaret är {len(text)} tecken och kapades vid {max_tecken}. "
            f"Smalna av `query` — välj färre värden per variabel — eller höj "
            f"`max_tecken`. Cellkontrollen släppte igenom frågan eftersom "
            f"den ryms hos myndigheten; det är klientens svarsgräns som är "
            f"tröskeln här."
        ),
        "delsvar": text[:max_tecken],
    }


# ============================================================================
# Ingångspunkt
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
