# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för PxWebApi 2 — alla myndigheter som rullat över till v2.

Exponerar PxWebApi 2-klienten över MCP. En enda server hanterar alla
v2-myndigheter via en `myndighet`-parameter. SCB var först över; övriga
följer stegvis och syns automatiskt här när deras post i registret går
över (eller överstyrs i .env) — ingen kodändring behövs.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.
DB-initiering sker i __main__ och servern startar även om DB är nere.

Verktyg:
    pxweb_2_lista_myndigheter
    pxweb_2_lista_tabeller
    pxweb_2_hamta_metadata
    pxweb_2_hamta_dimensionsvarden
    pxweb_2_uppskatta_celler
    pxweb_2_hamta_data
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
from data_och_analys.klienter import myndigheter, pxweb_2

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "pxweb_2",
    instructions=(
        "MCP-server för myndigheter som rullat över till PxWebApi 2 — "
        "SCB är den stora. Verktygen har prefixet pxweb_2_. "
        "VERSIONSREGISTRET AVGÖR VILKEN SERVER SOM GÄLLER: myndigheter som "
        "ännu talar API v1 ligger i doa-pxweb-1. Kör "
        "pxweb_2_lista_myndigheter först; får du inte träff, prova "
        "doa-pxweb-1 innan du drar slutsatsen att myndigheten saknas. "
        "V2 SKILJER SIG FRÅN V1: tabeller adresseras med ett platt "
        "`tabell_id` i stället för en stig genom ett träd, och urvalet "
        "skrivs som {dimension: [värden]} i stället för v1:s selektorlista. "
        "pxweb_2_lista_tabeller söker direkt på fritext — det finns inget "
        "träd att navigera. "
        "ARBETSORDNING: lista_myndigheter → lista_tabeller → hamta_metadata "
        "→ (vid behov hamta_dimensionsvarden) → uppskatta_celler → "
        "hamta_data. Hoppa inte över metadatasteget: dimensions-ID och "
        "värdekoder skiljer sig mellan tabeller och årgångar, och ett "
        "tabell-ID ur förhandskunskap är nästan alltid fel. "
        "STORA DIMENSIONER: hamta_metadata kapar värdelistor per dimension. "
        "En dimension med tusentals värden (regioner, varugrupper) läses i "
        "stället med pxweb_2_hamta_dimensionsvarden, som filtrerar på "
        "`prefix` eller `innehaller` och paginerar. "
        "UPPSKATTA CELLER ÄR INTE VALFRITT: SCB:s tak är 150 000 dataceller "
        "per anrop. Överskrids det avvisas frågan hos källan, inte hos oss. "
        "uppskatta_celler svarar {antal, tak, far_plats} innan något hämtas."
    ),
    cache_hints=CACHE_HINTAR,
)


# ============================================================================
# Verktyg
# ============================================================================


@mcp.tool(title="Lista myndigheter på PxWebApi 2", annotations=LASNING_EXTERN)
async def pxweb_2_lista_myndigheter() -> list[str]:
    """Listar de myndigheter som för närvarande betjänas av PxWebApi 2.

    Listan kommer från det centrala registret — en myndighet som ännu
    talar v1 syns inte här utan i `pxweb_1_lista_myndigheter`.
    """
    return pxweb_2.lista_myndigheter()


@mcp.tool(title="Sök tabeller", annotations=LASNING_EXTERN)
async def pxweb_2_lista_tabeller(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_2_lista_myndigheter")
    ],
    sok: Annotated[str | None, Field(description="Fritext mot tabelltitel")] = None,
    query: Annotated[
        str | None, Field(description="Strukturerat filteruttryck enligt API v2")
    ] = None,
    sprak: Annotated[
        str | None, Field(description="Språkkod, t.ex. 'sv' eller 'en'")
    ] = None,
    sida_storlek: Annotated[
        int, Field(description="Träffar per sida", ge=1, le=500)
    ] = 30,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
) -> dict[str, Any]:
    """Söker eller listar tabeller hos en v2-myndighet.

    `sok` är fritext mot tabellnamn och beskrivning (t.ex. "RegSO",
    "arbetslöshet"). `query` accepteras som alias. Utan sökterm listas
    alla tabeller — hämta nästa sida med `sida=2` osv.

    Svaret är kompakt (id, titel, period, variabelnamn) och sidsatt —
    använd `pxweb_2_hamta_metadata` för en tabells fulla struktur.
    """
    sokterm = sok or query
    tabeller = await pxweb_2.lista_tabeller(
        myndighet,
        query=sokterm,
        sprak=sprak,
        sida_storlek=sida_storlek,
        sida=sida,
    )
    # Kurerat urval av fält — full model_dump ger svar på 100k+ tecken
    # som spränger klientens tokengräns redan vid default-sidstorlek.
    rader = []
    for t in tabeller:
        d = t.model_dump(by_alias=True)
        rader.append({
            "id": d.get("id"),
            "label": d.get("label"),
            "forsta_period": d.get("firstPeriod"),
            "sista_period": d.get("lastPeriod"),
            "variabler": d.get("variableNames"),
            "uppdaterad": d.get("updated"),
        })
    return {
        "sokterm": sokterm,
        "sida": sida,
        "sida_storlek": sida_storlek,
        "antal_pa_sidan": len(rader),
        "tips": (
            "Fler träffar kan finnas — hämta nästa sida med sida=2. "
            "Tom lista + sokterm: prova synonym (SCB:s fritextsök "
            "matchar tabellnamn/beskrivning)."
        ),
        "tabeller": rader,
    }


# Standardtak för antal värden per dimension i MCP-svaret. Tabeller med
# geo-dimensioner kan ha 19 000+ värden — hela svaret får inte plats i
# MCP-kanalens 1 MB-cap. 200 räcker för alla rimligt små dimensioner
# (Tid, Kön, ContentsCode) utan att trunkeras, medan Region trunkeras
# och vägen vidare blir `pxweb_2_hamta_dimensionsvarden`.
_MAX_VARDEN_PER_DIM_DEFAULT = 200


@mcp.tool(title="Hämta tabellmetadata", annotations=LASNING_EXTERN)
async def pxweb_2_hamta_metadata(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_2_lista_myndigheter")
    ],
    tabell_id: Annotated[
        str, Field(description="Tabell-ID ur pxweb_2_lista_tabeller")
    ],
    sprak: Annotated[str | None, Field(description="Språkkod, t.ex. 'sv'")] = None,
    max_varden_per_dim: Annotated[
        int,
        Field(
            description=(
                "Tak för hur många värden som visas per dimension; större "
                "listor läses med pxweb_2_hamta_dimensionsvarden"
            ),
            ge=1,
        ),
    ] = _MAX_VARDEN_PER_DIM_DEFAULT,
) -> dict[str, Any]:
    """Hämtar metadata för en tabell — variabler, källa, uppdatering.

    Anropas alltid innan `pxweb_2_hamta_data` så att variabel-ID:n och
    värdelistor i urvalet är aktuella.

    Dimensioner med fler värden än `max_varden_per_dim` returneras
    trunkerade — `truncated=true` och `total_values=N` markerar det.
    Standardvärdet 200 räcker för Tid/Kön/ContentsCode utan trunkering;
    Region i geo-tabeller (TAB6571 har 19 182 regioner) trunkeras och
    den fullständiga listan hämtas via
    `pxweb_2_hamta_dimensionsvarden` med t.ex. `prefix="0123"` för
    alla värden under Järfälla kommun. Cellberäkningen
    (`pxweb_2_uppskatta_celler`) räknar mot `total_values` så
    förhandskontrollen mot celltaket fortsätter att stämma.
    """
    metadata = await pxweb_2.hamta_metadata(
        myndighet,
        tabell_id,
        sprak=sprak,
        max_varden_per_dim=max_varden_per_dim,
    )
    return metadata.model_dump(by_alias=True)


@mcp.tool(title="Läs en dimensions värdelista", annotations=LASNING_EXTERN)
async def pxweb_2_hamta_dimensionsvarden(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_2_lista_myndigheter")
    ],
    tabell_id: Annotated[
        str, Field(description="Tabell-ID ur pxweb_2_lista_tabeller")
    ],
    dimension_id: Annotated[
        str, Field(description="Dimensions-ID ur pxweb_2_hamta_metadata")
    ],
    sprak: Annotated[str | None, Field(description="Språkkod, t.ex. 'sv'")] = None,
    prefix: Annotated[
        str | None, Field(description="Filtrera på värdekodens inledning")
    ] = None,
    innehaller: Annotated[
        str | None, Field(description="Filtrera på delsträng i värdetexten")
    ] = None,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    sida_storlek: Annotated[
        int, Field(description="Värden per sida", ge=1, le=2000)
    ] = 500,
) -> dict[str, Any]:
    """Slår upp en enskild dimensions värden med filter och paginering.

    Komplement till `pxweb_2_hamta_metadata` när en dimension är så
    stor att den trunkerats i metadatasvaret. Typiskt användningsfall:
    Region för en geo-tabell. `prefix="0123"` plockar alla värden under
    Järfälla kommun — kommun, DeSO och RegSO med ledande 0123.
    `innehaller="malmö"` matchar etikett case-insensitivt. Filtren är AND.

    Returnerar `{dimension_id, label, totalt_i_kallan, matchande, sida,
    sida_storlek, varden: [{kod, etikett}]}`.
    """
    sida_data = await pxweb_2.hamta_dimensionsvarden(
        myndighet,
        tabell_id,
        dimension_id,
        sprak=sprak,
        prefix=prefix,
        innehaller=innehaller,
        sida=sida,
        sida_storlek=sida_storlek,
    )
    return sida_data.model_dump(by_alias=True)


@mcp.tool(title="Räkna dataceller före hämtning", annotations=LASNING_EXTERN)
async def pxweb_2_uppskatta_celler(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_2_lista_myndigheter")
    ],
    tabell_id: Annotated[
        str, Field(description="Tabell-ID ur pxweb_2_lista_tabeller")
    ],
    urval: Annotated[
        dict[str, list[str]],
        Field(description="{dimension_id: [värdekoder]} — samma form som hamta_data"),
    ],
    sprak: Annotated[str | None, Field(description="Språkkod, t.ex. 'sv'")] = None,
) -> dict[str, Any]:
    """Beräknar hur många dataceller ett urval skulle ge.

    Användbart för att kontrollera om en planerad fråga ryms inom
    myndighetens celltak innan den skickas. Returnerar
    `{antal, tak, far_plats}`. Taket per myndighet kommer från det
    centrala registret (SCB:s dokumenterade 150 000 som standard).

    Trunkerade dimensioner räknas mot `total_values` (faktiskt antal i
    källan) — uppskattningen påverkas inte av att metadatat trunkerats
    på vägen ut.
    """
    m = myndigheter.hamta(myndighet)
    # Trunkerar dimensioner aggressivt här — vi är bara intresserade av
    # antal och `total_values`, inte värdelistorna själva.
    metadata = await pxweb_2.hamta_metadata(
        myndighet, tabell_id, sprak=sprak, max_varden_per_dim=0
    )
    antal = pxweb_2.uppskatta_celler(metadata, urval)
    return {
        "antal": antal,
        "tak": m.max_celler,
        "far_plats": antal <= m.max_celler,
    }


@mcp.tool(title="Hämta data ur en PxWebApi 2-tabell", annotations=LASNING_EXTERN)
async def pxweb_2_hamta_data(
    myndighet: Annotated[
        str, Field(description="Myndighetsnyckel ur pxweb_2_lista_myndigheter")
    ],
    tabell_id: Annotated[
        str, Field(description="Tabell-ID ur pxweb_2_lista_tabeller")
    ],
    urval: Annotated[
        dict[str, list[str]],
        Field(
            description=(
                "{dimension_id: [värdekoder]} med ID och koder ur "
                "pxweb_2_hamta_metadata"
            )
        ),
    ],
    sprak: Annotated[str | None, Field(description="Språkkod, t.ex. 'sv'")] = None,
    format: Annotated[
        str, Field(description="'json-stat2' (standard), 'csv' eller 'px'")
    ] = "json-stat2",
    forhandskontrollera: Annotated[
        bool,
        Field(description="Hämta metadata först och avvisa frågor över celltaket"),
    ] = True,
) -> dict[str, Any]:
    """Hämtar data från en v2-tabell.

    `urval` mappar variabel-ID till en lista av värden:
        {"Region": ["00", "01"], "Tid": ["2024"]}
    Värdet `["*"]` betyder "alla värden".

    Med `forhandskontrollera=True` hämtas metadata först och cellantalet
    räknas upp; överskrider det myndighetens tak kastas ett tydligt fel
    innan API-anropet. Med False skickas frågan direkt och API:t
    returnerar 400 om gränsen överskrids.
    """
    # Förhandskontrollen behöver inte värdelistorna — bara `total_values`
    # per dimension. Trunkera aggressivt så svaret går snabbt och lätt.
    metadata = (
        await pxweb_2.hamta_metadata(
            myndighet, tabell_id, sprak=sprak, max_varden_per_dim=0
        )
        if forhandskontrollera
        else None
    )
    return await pxweb_2.hamta_data(
        myndighet,
        tabell_id,
        urval=urval,
        sprak=sprak,
        format=format,
        metadata=metadata,
    )


# ============================================================================
# Ingångspunkt
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
