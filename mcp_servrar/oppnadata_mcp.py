# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för att hitta, hämta och analysera öppna data från kommuner,
regioner och myndigheter.

Sökningen går direkt till varje organisations egen katalog — EntryScape,
CKAN, Huwise, ArcGIS Hub eller en publicerad DCAT-fil — enligt registret
som `cli/kartlagg_datadelning.py` fyller. Datamängderna beskrivs enligt
DCAT-AP-SE, och varje distribution får en hämtningsväg som avgör hur datat
läses. Tabeller beskrivs, filtreras och aggregeras på serversidan så att
bara resultatet går till klienten.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.

Verktyg:
    oppnadata_hitta_kataloger
    oppnadata_sok
    oppnadata_hamta_datamangd
    oppnadata_beskriv_data
    oppnadata_las_rader
    oppnadata_aggregera
"""

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
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from data_och_analys.infra import db
from data_och_analys.infra.mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from data_och_analys.infra.mcp_transport import starta
from data_och_analys.oppnadata import tabell as tabellager
from data_och_analys.oppnadata import tjanst
from data_och_analys.oppnadata.plattformar import Sokresultat
from data_och_analys.oppnadata.register import Katalog

logger = logging.getLogger(__name__)

# Klientens tak är ~150 000 tecken och svaret skickas två gånger (text och
# structuredContent), så radsvar hålls under hälften.
_MAX_TECKEN_RADER = 60_000
_MAX_TECKEN_VARDE = 300

mcp = MCPServer(
    "oppnadata",
    instructions=(
        "MCP-server för öppna data från svenska kommuner, regioner och myndigheter. "
        "Verktygen har prefixet oppnadata_. "
        "ARBETSORDNING: oppnadata_hitta_kataloger (vilka organisationer som delar data, "
        "var) → oppnadata_sok (fritext i deras kataloger) → oppnadata_hamta_datamangd "
        "(distributioner och hur de hämtas) → oppnadata_beskriv_data (kolumner, typer, "
        "värden) → oppnadata_aggregera eller oppnadata_las_rader. Hoppa inte över "
        "beskrivningen: kolumnnamn skiljer sig mellan organisationer och årgångar. "
        "KATALOGERNA: sökningen går direkt till varje organisations egen katalog — "
        "EntryScape, CKAN, Huwise, ArcGIS Hub eller en publicerad DCAT-fil — enligt "
        "ett register över kataloger som svarade när de kontrollerades. Dataportal.se "
        "används inte för sökning; en stor del av anmälningarna där pekar på adresser "
        "som slutat svara. En organisation som saknas i registret hade ingen fungerande "
        "katalog vid kontrollen. "
        "SÖK MED FLERA FORMULERINGAR: organisationer kallar samma sak olika saker "
        "(leverantörsreskontra, leverantörsfakturor, utbetalningar). Ange alternativen "
        "i `fragor`; träffarna slås ihop. Sökningen matchar ordbörjan. "
        "DCAT-AP-SE: en datamängd har en eller flera distributioner — samma data i olika "
        "format eller via olika åtkomst. Välj distribution efter `hamtningsvag`: 'fil' "
        "(CSV, Excel, JSON, GeoJSON), 'rowstore', 'ckan', 'huwise', 'wfs', "
        "'ogc_api_features' och 'arcgis_rest' läses här; 'pxweb', 'kolada' och "
        "'researchdata' läses med suitens servrar för dem (doa-pxweb-2 för SCB, "
        "doa-pxweb-1, doa-kolada, doa-katalog); 'karttjanst', 'webbsida' och 'api' går "
        "inte att läsa som data. Föredra en fil framför ett API när hela tabellen behövs — "
        "API:er sidhämtas och kapas vid 20 000 rader. "
        "PERIODER publiceras på två sätt. Antingen har en distribution flera filer, "
        "ofta en per månad (`filtitlar`) — välj med `filurval`, ett reguljärt uttryck mot "
        "titlarna. Läs titlarna först: formen skiljer sig ('2026-01', '202601', "
        "'Januari'), så första kvartalet kan vara '2026-0[1-3]' eller '20260[1-3]'. "
        "Eller har datamängden en distribution per period — ange då flera nummer i "
        "`distribution`, så slås de ihop. "
        "ANALYS: aggregera på servern. oppnadata_aggregera grupperar på kolumner eller "
        "på period ur en datumkolumn ('kvartal:datum', 'manad:datum', 'ar:datum') och "
        "summerar ett mått; belopp med decimalkomma tolkas rätt. Hämta aldrig råa rader "
        "för att räkna själv. För jämförelser mellan organisationer: aggregera var och "
        "en för sig med respektive kolumnnamn och jämför resultaten. "
        "VARNINGAR: svaren bär `varningar` — rader som inte gick att tolka, kapade "
        "hämtningar, olika kolumner mellan filer. Nämn dem när du redovisar resultat."
    ),
    cache_hints=CACHE_HINTAR,
)


class KatalogLista(BaseModel):
    antal: int
    kataloger: list[Katalog]


class Villkor(BaseModel):
    kolumn: str
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "innehaller", "borjar_med", "i", "mellan"] = "="
    varde: Any = Field(description="Ett värde; en lista för 'i'; två värden för 'mellan'")


_Id = Annotated[str, Field(description="Datamängdens id ur oppnadata_sok")]
_Distribution = Annotated[int | list[int], Field(
    description="Distributionens nummer ur oppnadata_hamta_datamangd, eller flera nummer som slås "
                "ihop — när en period är uppdelad på en distribution per månad")]
_Filurval = Annotated[str | None, Field(
    description="Reguljärt uttryck mot filernas titlar, t.ex. '2026-0[1-3]'. Utelämnat: alla filer")]
_Blad = Annotated[str | None, Field(description="Blad i ett kalkylark eller fil i ett ZIP-arkiv")]


# ============================================================================
# Kataloger och sök
# ============================================================================

@mcp.tool(title="Hitta öppna data-kataloger", annotations=LASNING_DB)
async def oppnadata_hitta_kataloger(
    namn: Annotated[str | None, Field(description="Del av organisationens namn")] = None,
    kategori: Annotated[Literal["kommun", "region", "myndighet"] | None,
                        Field(description="Organisationstyp")] = None,
    lan: Annotated[str | None, Field(
        description="Län som kod ('14'), bokstav ('O') eller namn ('Västra Götaland')")] = None,
    plattform: Annotated[Literal["entryscape", "ckan", "huwise", "arcgis_hub", "dcat_fil"] | None,
                         Field(description="Katalogplattform")] = None,
) -> KatalogLista:
    """Vilka organisationer som delar öppna data, i vilken katalog och på vilken plattform.

    Registret innehåller kataloger som svarade när de senast kontrollerades,
    med antal datamängder och kontrolldatum.
    """
    kataloger = await tjanst.hitta_kataloger(namn, kategori, lan, plattform)
    return KatalogLista(antal=len(kataloger), kataloger=kataloger)


@mcp.tool(title="Sök öppna data", annotations=LASNING_EXTERN)
async def oppnadata_sok(
    fragor: Annotated[list[str], Field(
        description="En eller flera formuleringar; träffarna slås ihop", min_length=1)],
    namn: Annotated[str | None, Field(description="Del av organisationens namn")] = None,
    kategori: Annotated[Literal["kommun", "region", "myndighet"] | None,
                        Field(description="Organisationstyp")] = None,
    lan: Annotated[str | None, Field(description="Län som kod, bokstav eller namn")] = None,
    plattform: Annotated[Literal["entryscape", "ckan", "huwise", "arcgis_hub", "dcat_fil"] | None,
                         Field(description="Katalogplattform")] = None,
    per_katalog: Annotated[int, Field(description="Högst så många träffar per katalog",
                                      ge=1, le=25)] = 5,
) -> Sokresultat:
    """Söker datamängder i de valda organisationernas egna kataloger.

    Svaret har träffarna och, per katalog, antalet träffar eller felet om
    katalogen inte gick att fråga. Utan urval söks alla kataloger i
    registret; avgränsa hellre med län, kategori eller namn.
    """
    kataloger = await tjanst.hitta_kataloger(namn, kategori, lan, plattform)
    if not kataloger:
        raise ValueError("Inga kataloger matchar urvalet. Se oppnadata_hitta_kataloger.")
    return await tjanst.sok(fragor, kataloger, per_katalog)


# ============================================================================
# Datamängd och data
# ============================================================================

@mcp.tool(title="Hämta datamängd", annotations=LASNING_EXTERN)
async def oppnadata_hamta_datamangd(id: _Id) -> tjanst.DatamangdSvar:
    """Datamängden enligt DCAT-AP-SE: beskrivning, period, utgivare och
    distributioner — var och en med sin hämtningsväg och, när den har flera
    filer, filernas titlar."""
    return await tjanst.datamangd(id)


@mcp.tool(title="Beskriv data", annotations=LASNING_EXTERN)
async def oppnadata_beskriv_data(
    id: _Id, distribution: _Distribution, filurval: _Filurval = None, blad: _Blad = None,
) -> tjanst.Tabellbeskrivning:
    """Kolumner med typ (tal, datum, text), exempel, antal tomma och — när
    värdena är få — deras fördelning. Kör före aggregering: kolumnnamnen
    skiljer sig mellan organisationer."""
    t = await tjanst.tabell(id, distribution, filurval, blad)
    return tjanst.Tabellbeskrivning(**tabellager.beskriv(t))


@mcp.tool(title="Läs rader", annotations=LASNING_EXTERN)
async def oppnadata_las_rader(
    id: _Id, distribution: _Distribution,
    villkor: Annotated[list[Villkor] | None, Field(description="Filter; alla måste gälla")] = None,
    kolumner: Annotated[list[str] | None, Field(description="Bara dessa kolumner")] = None,
    fran_rad: Annotated[int, Field(description="Första raden i urvalet", ge=0)] = 0,
    antal: Annotated[int, Field(description="Antal rader", ge=1, le=200)] = 50,
    filurval: _Filurval = None, blad: _Blad = None,
) -> tjanst.Radsvar:
    """Rader ur tabellen, filtrerade. För summor och fördelningar: använd
    oppnadata_aggregera i stället."""
    t = await tjanst.tabell(id, distribution, filurval, blad)
    df = tabellager.filtrera(t.df, [v.model_dump() for v in villkor or []])
    if kolumner:
        saknas = [k for k in kolumner if k not in df.columns]
        if saknas:
            raise ValueError(f"Kolumnerna finns inte: {', '.join(saknas)}")
        df = df[kolumner]
    rader = [{k: (str(v)[:_MAX_TECKEN_VARDE] if v is not None else None) for k, v in r.items()}
             for r in df.iloc[fran_rad:fran_rad + antal].to_dict("records")]
    varningar = list(t.varningar)
    while rader and len(json.dumps(rader, ensure_ascii=False, default=str)) > _MAX_TECKEN_RADER:
        rader = rader[: len(rader) // 2]
        if "Svaret kortades" not in " ".join(varningar):
            varningar.append("Svaret kortades för att rymmas; fortsätt med fran_rad.")
    return tjanst.Radsvar(antal_i_urvalet=len(df), fran_rad=fran_rad, rader=rader,
                          kallor=t.kallor, varningar=varningar)


@mcp.tool(title="Aggregera data", annotations=LASNING_EXTERN)
async def oppnadata_aggregera(
    id: _Id, distribution: _Distribution,
    gruppera: Annotated[list[str], Field(
        description="Kolumner, eller period ur en datumkolumn: 'ar:datum', 'kvartal:datum', "
                    "'manad:datum'. Tom lista ger en total")],
    funktion: Annotated[Literal["summa", "medel", "median", "min", "max", "antal"],
                        Field(description="Aggregeringsfunktion")] = "summa",
    matt: Annotated[str | None, Field(description="Kolumnen som aggregeras; behövs inte för 'antal'")] = None,
    villkor: Annotated[list[Villkor] | None, Field(description="Filter före aggregering")] = None,
    topp: Annotated[int, Field(description="Visa de största grupperna", ge=1, le=500)] = 50,
    filurval: _Filurval = None, blad: _Blad = None,
) -> tjanst.Aggregering:
    """Grupperad aggregering på servern. Svaret har de största grupperna,
    totalen över hela urvalet och hur många grupper som utelämnades."""
    t = await tjanst.tabell(id, distribution, filurval, blad)
    r = tabellager.aggregera(t, gruppera, matt, funktion,
                             [v.model_dump() for v in villkor or []], topp)
    return tjanst.Aggregering(
        rader_i_urvalet=r["rader_i_urvalet"], grupper=r["grupper"],
        utelamnade_grupper=r["utelamnade_grupper"], resultat=r["resultat"],
        totalt=r[f"{funktion}_totalt"], kallor=r["kallor"], varningar=r["varningar"])


# ============================================================================
# Ingångspunkt
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp, initiera=db.initiera_schema)
