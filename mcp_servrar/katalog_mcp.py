# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för DCAT-discovery, researchdata.se och dataportal.se.

Katalogservern svarar på frågan "var finns datat?" snarare än att
returnera datat självt. Två källor:

    dataportal.se (DCAT-AP-SE via SPARQL)
        Sveriges nationella katalog över öppna data. Frågar man katalogen
        får man tillbaka distributionernas accessURL — PxWeb, OGC API,
        CKAN, nedladdning — som sedan matas till rätt hämtningsklient.
        Det är discovery-ledet i suiten.

    researchdata.se (SND, via OAI-PMH)
        Nationell katalog över forskningsdata. Det dedikerade REST-API:et
        är ännu inte lanserat; OAI-PMH är den stabila vägen in. Bär bl.a.
        valforskningsdata (VALU, SNES) som kombineras med SCB och val.se.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.

Verktyg:
    dcat_sok_dataset
    dcat_hitta_distributioner_med_url
    dcat_hitta_pxweb_instanser
    dcat_kor_sparql
    researchdata_identify
    researchdata_lista_metadataformat
    researchdata_lista_dataset_id
    researchdata_sok_dataset
    researchdata_hamta_dataset
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
from data_och_analys.infra.paginering import Paket, paketera_forpaginerat
from data_och_analys.katalog_klienter import dcat, researchdata

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "katalog",
    instructions=(
        "MCP-server för discovery av svenska öppna data. Två källor: "
        "dataportal.se (DCAT-AP-SE via SPARQL, prefix dcat_) och "
        "researchdata.se/SND (OAI-PMH, prefix researchdata_). "
        "DETTA ÄR DISCOVERY-LEDET: verktygen svarar på var data finns, "
        "aldrig vad datat innehåller. Kedjan är dcat_sok_dataset → "
        "dcat_hitta_distributioner_med_url → läs accessURL → mata rätt "
        "hämtarserver (doa-pxweb-1/2, doa-wfs, doa-oafeat, doa-arcgis). "
        "dcat_hitta_pxweb_instanser är genvägen för att upptäcka nya "
        "PxWeb-myndigheter att lägga in i suitens myndighetsregister. "
        "RESEARCHDATA.SE HAR INGET FRITEXTFILTER — OAI-PMH stöder bara "
        "skörd per set eller datumintervall. För att hitta en specifik "
        "studie: sök i dataportal.se via dcat_sok_dataset och korsreferera "
        "identifieraren, eller skörda set-vis. Paginera alltid via "
        "`resumption_token`; att utelämna den ger bara första sidan. "
        "METADATAFORMAT: 'oai_dc' (Dublin Core) räcker för att hitta en "
        "studie. För variabel- och kodboksnivå på samhällsvetenskapliga "
        "studier som VALU och SNES, begär 'ddi33' eller 'ddi25' — det är "
        "där enkätfrågornas variabelnamn står. "
        "Verifiera alltid att en endpoint existerar innan den används; "
        "använd aldrig ett dataset-ID ur förhandskunskap."
    ),
    cache_hints=CACHE_HINTAR,
)


# ============================================================================
# DCAT-discovery — dataportal.se
# ============================================================================


@mcp.tool(title="Sök dataset i dataportal.se", annotations=LASNING_EXTERN)
async def dcat_sok_dataset(
    fritext: Annotated[
        str | None, Field(description="Fritext mot titel och beskrivning")
    ] = None,
    limit: Annotated[int, Field(description="Antal träffar", ge=1, le=500)] = 20,
) -> Paket[dict[str, Any]]:
    """Söker dataset i Sveriges dataportal (dataportal.se) på fritext.

    Matchar mot titel och beskrivning. Returnerar `[{dataset, titel,
    beskrivning, utgivare}]` där `dataset` är en URI. Utan `fritext`
    returneras de första `limit` dataseten (utforskning).

    Det här är ingångspunkten till discovery: hitta en datamängd, ta sedan
    dess distributioner via `dcat_hitta_distributioner_med_url` eller en
    egen SPARQL-fråga för att se exakt var och hur den nås.
    """
    rader = await dcat.sok_dataset(fritext=fritext, limit=limit)
    return Paket[dict[str, Any]](
        **paketera_forpaginerat(rader, total=len(rader), per_sida=limit)
    )


@mcp.tool(title="Hitta distributioner på URL-mönster", annotations=LASNING_EXTERN)
async def dcat_hitta_distributioner_med_url(
    url_monster: Annotated[
        str,
        Field(
            description=(
                "Delsträng i accessURL, t.ex. 'PXWeb', 'wfs', 'ogc', 'ckan' "
                "eller en myndighetsdomän"
            )
        ),
    ],
    limit: Annotated[int, Field(description="Antal träffar", ge=1, le=1000)] = 50,
) -> Paket[dict[str, Any]]:
    """Hittar dataset vars distributions-accessURL matchar ett mönster.

    `url_monster` är en delsträng i accessURL:en, t.ex. `"PXWeb"`,
    `"wfs"`, `"ogc"`, `"ckan"` eller en myndighetsdomän. Returnerar
    `[{dataset, titel, accessURL, format}]`.

    Använd det för att hitta alla datamängder som nås via en viss teknik
    och para dem med rätt hämtningsklient (PxWeb-MCP, OGC-MCP osv).
    """
    rader = await dcat.hitta_distributioner_med_url(url_monster, limit=limit)
    return Paket[dict[str, Any]](
        **paketera_forpaginerat(rader, total=len(rader), per_sida=limit)
    )


@mcp.tool(title="Hitta alla PxWeb-instanser", annotations=LASNING_EXTERN)
async def dcat_hitta_pxweb_instanser(
    limit: Annotated[int, Field(description="Antal träffar", ge=1, le=1000)] = 200,
) -> Paket[dict[str, Any]]:
    """Hittar alla PxWeb-instanser i dataportal.se.

    Bekvämlighetsvariant av `dcat_hitta_distributioner_med_url("PXWeb")`.
    Användbar för att upptäcka nya PxWeb-myndigheter att lägga in i
    suitens myndighetsregister.
    """
    rader = await dcat.hitta_pxweb_instanser(limit=limit)
    return Paket[dict[str, Any]](
        **paketera_forpaginerat(rader, total=len(rader), per_sida=limit)
    )


@mcp.tool(title="Kör godtycklig SPARQL-fråga", annotations=LASNING_EXTERN)
async def dcat_kor_sparql(
    fraga: Annotated[
        str,
        Field(
            description=(
                "SPARQL-fråga; prefixen dcat:, dcterms: och foaf: injiceras "
                "automatiskt om frågan saknar PREFIX"
            )
        ),
    ],
    format: Annotated[
        str,
        Field(description="'json' för SELECT/ASK, 'raw' för CONSTRUCT/DESCRIBE"),
    ] = "json",
    # Returtypen var tidigare `Any`, vilket gav outputSchema: null och gjorde
    # verktyget till det enda i suiten utan structuredContent. Unionen speglar
    # vad klienten faktiskt returnerar beroende på `format`.
) -> dict[str, Any] | str:
    """Kör en godtycklig SPARQL-fråga mot dataportal.se.

    Prefixen `dcat:`, `dcterms:` och `foaf:` injiceras automatiskt om
    frågan inte redan deklarerar `PREFIX`. `format="json"` ger SPARQL-
    resultat-JSON; `format="raw"` ger råtext (för CONSTRUCT/DESCRIBE).

    För avancerade discovery-frågor mot DCAT-AP-SE-modellen:
    `dcat:Dataset`, `dcat:distribution`, `dcat:accessService`,
    `dcat:endpointURL`, `dcterms:format`.
    """
    return await dcat.kor_sparql(fraga, format=format)


# ============================================================================
# researchdata.se — OAI-PMH
# ============================================================================


@mcp.tool(title="Repository-metadata för researchdata.se", annotations=LASNING_EXTERN)
async def researchdata_identify() -> dict[str, Any]:
    """Repository-metadata för researchdata.se (namn, granularitet, admin)."""
    return await researchdata.identify()


@mcp.tool(title="Lista metadataformat", annotations=LASNING_EXTERN)
async def researchdata_lista_metadataformat(
    identifier: Annotated[
        str | None,
        Field(description="Begränsa till format tillgängliga för en viss post"),
    ] = None,
) -> list[dict[str, Any]]:
    """Listar metadataformat researchdata.se stöder.

    T.ex. `oai_dc` (Dublin Core), `datacite`, `ddi25`/`ddi33` (samhälls-
    vetenskaplig kodbok), `dcat`. `identifier` begränsar till format
    tillgängliga för en specifik post.
    """
    return await researchdata.lista_metadataformat(identifier=identifier)


@mcp.tool(title="Lista dataset-ID (snabb översikt)", annotations=LASNING_EXTERN)
async def researchdata_lista_dataset_id(
    metadata_prefix: Annotated[
        str, Field(description="'oai_dc', 'datacite', 'ddi25', 'ddi33' eller 'dcat'")
    ] = "oai_dc",
    set_spec: Annotated[str | None, Field(description="OAI-set att skörda")] = None,
    from_: Annotated[
        str | None,
        Field(
            description="Ändrade från och med (YYYY-MM-DD)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    until: Annotated[
        str | None,
        Field(
            description="Ändrade till och med (YYYY-MM-DD)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    resumption_token: Annotated[
        str | None, Field(description="Token ur föregående svar för nästa sida")
    ] = None,
) -> dict[str, Any]:
    """Listar dataset-headers (id + datestamp) — snabb katalogöversikt.

    Returnerar `{identifierare, resumption_token}`. Mata tillbaka
    `resumption_token` i nästa anrop för nästa sida. `from_`/`until`
    (YYYY-MM-DD) avgränsar på ändringsdatum för inkrementell skörd.
    """
    return await researchdata.lista_dataset_id(
        metadata_prefix=metadata_prefix, set_spec=set_spec,
        from_=from_, until=until, resumption_token=resumption_token,
    )


@mcp.tool(title="Lista dataset med full metadata", annotations=LASNING_EXTERN)
async def researchdata_sok_dataset(
    metadata_prefix: Annotated[
        str, Field(description="'oai_dc', 'datacite', 'ddi25', 'ddi33' eller 'dcat'")
    ] = "oai_dc",
    set_spec: Annotated[str | None, Field(description="OAI-set att skörda")] = None,
    from_: Annotated[
        str | None,
        Field(
            description="Ändrade från och med (YYYY-MM-DD)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    until: Annotated[
        str | None,
        Field(
            description="Ändrade till och med (YYYY-MM-DD)",
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ] = None,
    resumption_token: Annotated[
        str | None, Field(description="Token ur föregående svar för nästa sida")
    ] = None,
) -> dict[str, Any]:
    """Listar dataset med full metadata, en sida i taget (ListRecords).

    Returnerar `{poster, resumption_token}` där varje post är
    `{header, metadata}`. För `oai_dc` har metadatan Dublin Core-fält
    (`title`, `creator`, `subject`, `description`, `identifier` …).
    Paginera via `resumption_token`.

    OAI-PMH har inget fritextfilter — för att hitta ett specifikt dataset,
    skörda set-vis (`set_spec`) eller datumvis, eller sök i dataportal.se
    via DCAT och korsreferera identifieraren.
    """
    return await researchdata.sok_dataset(
        metadata_prefix=metadata_prefix, set_spec=set_spec,
        from_=from_, until=until, resumption_token=resumption_token,
    )


@mcp.tool(title="Hämta ett enskilt dataset", annotations=LASNING_EXTERN)
async def researchdata_hamta_dataset(
    identifier: Annotated[
        str,
        Field(
            description=(
                "OAI-identifierare på formen "
                "'oai:researchdata.se:<id>/<version>' ur listverktygen"
            )
        ),
    ],
    metadata_prefix: Annotated[
        str,
        Field(
            description=(
                "'oai_dc' för översikt; 'ddi33' eller 'ddi25' för variabel- "
                "och kodboksnivå (VALU, SNES)"
            )
        ),
    ] = "oai_dc",
) -> dict[str, Any] | None:
    """Hämtar ett enskilt dataset via OAI-identifierare.

    Identifieraren har formen `oai:researchdata.se:<id>/<version>` (ur
    `researchdata_lista_dataset_id` eller `researchdata_sok_dataset`).
    Returnerar `{header, metadata}`. Välj `metadata_prefix="ddi33"` för
    variabel- och kodboksnivå på samhällsvetenskapliga studier (VALU,
    SNES) i stället för `oai_dc`.
    """
    return await researchdata.hamta_dataset(
        identifier, metadata_prefix=metadata_prefix
    )


# ============================================================================
# Ingångspunkt
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
