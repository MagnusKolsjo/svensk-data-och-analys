# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för Kolada.

Kolada är RKA:s databas för kommun- och regionstatistik. ~6 000 KPI:er
aggregerade från SCB, FK, Socialstyrelsen, Skolverket m.fl. — i samma
4-siffriga kommunkoder som resten av vår geo-stack, vilket gör det
trivialt att kombinera Kolada-data med klassificeringar i `doa-geo`.

Verktyg:
    kolada_sok_kpier
    kolada_hamta_kpi
    kolada_lista_grupper
    kolada_hamta_data
    kolada_hamta_data_for_kommun
    kolada_lista_kommuner
    kolada_lista_organisationsenheter
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
from data_och_analys.klienter import kolada

logger = logging.getLogger(__name__)

mcp = MCPServer(
    "kolada",
    instructions=(
        "MCP-server för Kolada — RKA:s databas för kommun- och "
        "regionstatistik, ~6 000 KPI:er aggregerade från SCB, "
        "Försäkringskassan, Socialstyrelsen och Skolverket. Verktygen har "
        "prefixet kolada_. "
        "KOMBINERBART MED DOA-GEO: Kolada använder SCB:s fyrsiffriga "
        "kommunkoder, samma som geo-servern. En Kolada-serie kan därför "
        "brytas ned på FA-region, kommungrupp eller länstillhörighet utan "
        "mellanled. "
        "SLÅ UPP KPI:N FÖRST: kolada_sok_kpier → kolada_hamta_kpi innan "
        "kolada_hamta_data. Metadatan säger om serien är könsuppdelad "
        "(`is_divided_by_gender`) och om den finns per organisationsenhet "
        "(`has_ou_data`) — utan det går svaret inte att tolka rätt. "
        "KÖNSFILTRET STYR SVARSSTORLEKEN: `kon='T'` (totalt) är default "
        "och tar bort två tredjedelar av raderna. Sätt `kon=None` bara när "
        "skillnaden mellan kvinnor och män är själva frågan. "
        "SVARSSTORLEK: utan `municipality_id` returneras 290 kommuner + 21 "
        "regioner + riket per KPI och år. Svaren är plattade och paginerade "
        "({total, sida, per_sida, antal_sidor, datapunkter}) — läs "
        "`antal_sidor` och hämta vidare med `sida=2`. "
        "`folj_paginering=True` följer Koladas egen next_url-kedja så "
        "longitudinella frågor inte tyst tappar rader."
    ),
    cache_hints=CACHE_HINTAR,
)


@mcp.tool(title="Sök nyckeltal", annotations=LASNING_EXTERN)
async def kolada_sok_kpier(
    title: Annotated[
        str | None,
        Field(description="Fritext mot KPI-titel, t.ex. 'arbetslöshet'"),
    ] = None,
    per_page: Annotated[int, Field(description="Träffar per sida", ge=1, le=500)] = 20,
    page: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
) -> dict[str, Any]:
    """Söker KPI:er i Kolada.

    `title` är fritextsökning mot KPI-titel (t.ex. "arbetslöshet",
    "barnomsorg", "skola"). Saknas `title` listas alla KPI:er
    (paginerat — använd `next_url` i svaret för nästa sida).

    Returvärdets `values` har metadata per KPI: `id`, `title`,
    `description`, `operating_area`, `perspective`, `is_divided_by_gender`,
    `has_ou_data`.
    """
    return await kolada.sok_kpier(title=title, per_page=per_page, page=page)


@mcp.tool(title="Hämta nyckeltalsmetadata", annotations=LASNING_EXTERN)
async def kolada_hamta_kpi(
    kpi_id: Annotated[
        str, Field(description="KPI-ID ur kolada_sok_kpier, t.ex. 'N01951'")
    ],
) -> dict[str, Any] | None:
    """Returnerar full metadata för en specifik KPI.

    Inkluderar källan (t.ex. "SCB"), publiceringsdatum och om datat är
    könsuppdelat. Använd före `kolada_hamta_data` för att veta hur svaret
    ska tolkas.
    """
    return await kolada.hamta_kpi(kpi_id)


@mcp.tool(title="Lista nyckeltalsgrupper", annotations=LASNING_EXTERN)
async def kolada_lista_grupper(
    title: Annotated[
        str | None, Field(description="Fritext mot gruppnamn")
    ] = None,
    per_page: Annotated[int, Field(description="Grupper per sida", ge=1, le=500)] = 50,
    page: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
) -> dict[str, Any]:
    """Listar KPI-grupper (tematiska samlingar).

    Användbart för att hitta relaterade KPI:er — t.ex. en grupp om
    äldreomsorg innehåller flera KPI:er som rapporteras tillsammans.
    """
    return await kolada.lista_kpi_grupper(title=title, per_page=per_page, page=page)


# Kolada rapporterar oftast T + M + K per datapunkt. Att filtrera på
# totalvärdet tar bort två tredjedelar av raderna innan de ens paginerats.
_KON_DEFAULT = "T"


@mcp.tool(title="Hämta nyckeltalsdata", annotations=LASNING_EXTERN)
async def kolada_hamta_data(
    kpi_id: Annotated[
        str | list[str],
        Field(description="Ett eller flera KPI-ID, t.ex. 'N01951'"),
    ],
    municipality_id: Annotated[
        str | list[str] | None,
        Field(
            description=(
                "Fyrsiffrig kommunkod med inledande nolla ('0180') eller "
                "tvåsiffrigt region-ID ('01'). Utan filter returneras alla 312 "
                "enheter per KPI och år."
            )
        ),
    ] = None,
    year: Annotated[
        int | list[int] | None, Field(description="Ett eller flera kalenderår")
    ] = None,
    kon: Annotated[
        str | None,
        Field(description="'T' totalt, 'M', 'K', eller None för alla tre"),
    ] = _KON_DEFAULT,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Datapunkter per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
    folj_paginering: Annotated[
        bool,
        Field(description="Följ Koladas next_url-kedja så inga rader tappas"),
    ] = True,
) -> dict[str, Any]:
    """Hämtar datapunkter för KPI:er, kommuner och år.

    `kpi_id` är obligatoriskt — en eller flera (`"N01951"` eller
    `["N01951","N00945"]`). `municipality_id` är SCB:s 4-siffriga
    kommunkoder eller region-id (`"0180"` Stockholm, `"01"` Region
    Stockholm). `year` är ett eller flera årtal. Utan `municipality_id`
    returneras alla 290 kommuner + 21 regioner + riket = 312 enheter
    per (kpi, år, kön).

    `kon` filtrerar bort onödigt brus: `"T"` (default, totalt), `"M"`
    eller `"K"`, eller `None` för alla. Sätt `None` när Kvinnor/Män-
    skillnaden är poängen.

    `folj_paginering` (default True) följer Koladas `next_url`-kedja så
    longitudinella frågor inte tyst tappar rader vid >5 000 datapunkter.
    Sätt False bara om du vill ha exakt en API-sida.

    Svaret är plattat och paginerat på MCP-sidan:

        {total, sida, per_sida, antal_sidor,
         datapunkter: [{kpi, kommun, ar, kon, varde, status, count}]}

    `total` är antal datapunkter efter `kon`-filtret. När
    `antal_sidor > 1` hämtas nästa sida med `sida=2`.
    """
    if folj_paginering:
        raw = await kolada.hamta_data_alla(
            kpi_id=kpi_id, municipality_id=municipality_id, year=year
        )
    else:
        raw = await kolada.hamta_data(
            kpi_id=kpi_id, municipality_id=municipality_id, year=year
        )
    rader = kolada.platta_datapunkter(raw, kon=kon)
    return paginering.paginera(rader, sida=sida, per_sida=per_sida)


@mcp.tool(title="Hämta alla nyckeltal för en kommun", annotations=LASNING_EXTERN)
async def kolada_hamta_data_for_kommun(
    municipality_id: Annotated[
        str,
        Field(
            description="Fyrsiffrig kommunkod med inledande nolla, t.ex. '0180'",
            pattern=r"^\d{2,4}$",
        ),
    ],
    year: Annotated[
        int | list[int] | None,
        Field(
            description=(
                "Ett eller flera kalenderår — utan filter blir svaret enormt"
            )
        ),
    ] = None,
    kon: Annotated[
        str | None,
        Field(description="'T' totalt, 'M', 'K', eller None för alla tre"),
    ] = _KON_DEFAULT,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Datapunkter per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
    folj_paginering: Annotated[
        bool, Field(description="Följ Koladas next_url-kedja så inga rader tappas")
    ] = True,
) -> dict[str, Any]:
    """Hämtar alla KPI-värden för en kommun ett givet år (eller alla år).

    Kommunens bredsida av nyckeltal. Kolada har ~6 000 KPI:er — utan
    `year`-filter blir svaret många tiotusentals rader. Använd
    `year=[2024]` eller liknande för att smalna ner.

    `folj_paginering` (default True) följer Koladas `next_url`-kedja.

    Samma plattnings- och pagineringskontrakt som `kolada_hamta_data`.
    """
    if folj_paginering:
        raw = await kolada.hamta_data_for_kommun_alla(
            municipality_id=municipality_id, year=year
        )
    else:
        raw = await kolada.hamta_data_for_kommun(
            municipality_id=municipality_id, year=year
        )
    rader = kolada.platta_datapunkter(raw, kon=kon)
    return paginering.paginera(rader, sida=sida, per_sida=per_sida)


@mcp.tool(title="Lista Koladas kommuner och regioner", annotations=LASNING_EXTERN)
async def kolada_lista_kommuner(
    per_page: Annotated[int, Field(description="Poster per sida", ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    """Returnerar Koladas kommun- och regionlista.

    Mest för att verifiera namngivning eller hitta region-ID:n.
    Använd hellre `geo_lista_kommuner` om du vill ha lokala kommun-data.
    """
    return await kolada.lista_kommuner(per_page=per_page)


@mcp.tool(title="Lista organisationsenheter", annotations=LASNING_EXTERN)
async def kolada_lista_organisationsenheter(
    kpi_id: Annotated[
        str | None,
        Field(description="KPI-ID med has_ou_data=true, t.ex. betyg per skola"),
    ] = None,
    municipality_id: Annotated[
        str | None,
        Field(
            description="Fyrsiffrig kommunkod med inledande nolla",
            pattern=r"^\d{2,4}$",
        ),
    ] = None,
    per_page: Annotated[int, Field(description="Enheter per sida", ge=1, le=500)] = 100,
    page: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
) -> dict[str, Any]:
    """Listar organisationsenheter (skolor, äldreboende osv).

    OU:er är finare granularitet än kommun. KPI:er med `has_ou_data:true`
    rapporteras per enhet — t.ex. betygsresultat per skola.
    """
    return await kolada.lista_organisationsenheter(
        kpi_id=kpi_id,
        municipality_id=municipality_id,
        per_page=per_page,
        page=page,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
