# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för geografiska indelningar och geodata.

Exponerar `data_och_analys.geodata.indelningar` över MCP — både
redovisningsperspektivet ("dela upp på X") och urvalsperspektivet
("vilka enheter uppfyller kriterier"). Postnummer och valdistrikt
kommer in i takt med att deras synkar implementeras.

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.
Schemainitiering sker i __main__ och servern startar även om DB är nere.

Verktyg:

    Redovisning:
        geo_lista_lan
        geo_lista_regioner
        geo_lista_kommuner
        geo_kommun_info
        geo_lista_system
        geo_lista_grupper
        geo_lista_kommuner_i_grupp
        geo_oversatt

    Urval:
        geo_filtrera_kommuner

    Klassificering — synk och bootstrap:
        geo_lagg_till_kommuner
        geo_synka_skr_kommungrupp
        geo_synka_tillvaxtverket_fa_region
        geo_synka_tillvaxtverket_kommuntyper
        geo_synka_klassificering_rader

    Administrativa indelningar:
        geo_synka_regioner
        geo_synka_valkretsar
        geo_synka_nuts
        geo_synka_deso_och_regso
        geo_synka_valdistrikt
        geo_synka_postnummer

    Historiska tidsserier:
        geo_kommun_folkmangd_historik
        geo_kommun_folkmangd_aret
        geo_kommun_folkmangd_matris
        geo_kommun_folkmangd_topp
        geo_synka_kommun_folkmangd

    Geometri (kräver PostGIS):
        geo_synka_valdistrikt_geom
        geo_synka_kommun_geom
        geo_synka_lan_geom
        geo_synka_region_geom
        geo_synka_deso_geom
        geo_synka_regso_geom
        geo_bygg_valkrets_geom
        geo_punkt_till_valdistrikt
        geo_punkt_till_kommun
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

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from data_och_analys.geodata import (
    bevakning, folkmangd, geometrier, indelningar, synk, synkstatus,
)
from data_och_analys.geodata.geometrier import LICENS_ATTRIBUTION
from data_och_analys.infra import db, framsteg, paginering
from data_och_analys.infra.mcp_annotationer import (
    CACHE_HINTAR,
    LASNING_DB,
    LASNING_EXTERN,
    SYNK,
)
from data_och_analys.infra.models import (
    Folkmangdsrad,
    Grupp,
    KlassificeringsSystem,
    Kommun,
    KommunIGrupp,
    Lan,
    Region,
)
from data_och_analys.infra.mcp_transport import starta
from data_och_analys.infra.paginering import Paket
from data_och_analys.infra.tasks import TasksExtension

logger = logging.getLogger(__name__)

# Verktyg som får köras som tasks när klienten stöder det. Urvalet är de
# synkar som hämtar stora filer över nätet och därför riskerar klientens
# tidsgräns. Övriga synkar skriver hårdkodade kataloger eller några tiotal
# rader och går på millisekunder — ett task-handtag för dem vore bara krångel.
TASKBARA_VERKTYG = (
    "geo_synka_kommun_folkmangd",
    "geo_synka_valdistrikt_geom",
    "geo_synka_deso_geom",
    "geo_synka_regso_geom",
    "geo_synka_postnummer",
    "geo_synka_valdistrikt",
)

mcp = MCPServer(
    "geo",
    instructions=(
        "MCP-server för svenska geografiska indelningar och geodata. "
        "Verktygen har prefixet geo_. Datat ligger i suitens egen "
        "Postgres/SQLite, inte hos SCB direkt — svaren är alltså så färska "
        "som senaste synk. "
        "KOMMUNKODER: fyrsiffriga strängar med inledande nolla ('0180' "
        "Stockholm, inte 180). Länskoder är tvåsiffriga ('01'). Skicka "
        "alltid koden, aldrig namnet — namn är tvetydiga (Bollnäs kommun "
        "kontra tätorten) och stavningen varierar mellan källor. Servern "
        "har inget namnuppslag: har du bara ett namn, hämta kodlistan med "
        "geo_lista_kommuner och matcha där. "
        "geo_oversatt gör något annat än namnet antyder — den mappar "
        "kommunkoder till deras grupp i ett klassificeringssystem. "
        "KARTOR: geo_karta_folkmangd ritar rikets kommuner direkt. "
        "geo_karta_omraden ritar vilken statistik som helst över "
        "kommun, län, DeSO, RegSO eller valdistrikt — hämta värdena "
        "där de finns (doa-pxweb-2 har SCB:s RegSO-tabeller med samma "
        "koder som här) och skicka in dem som {kod: värde}. Widgeten "
        "hämtar geometrin själv, så den passerar aldrig kontexten. "
        "Avgränsa alltid deso, regso och valdistrikt med inom_kommun "
        "eller inom_lan; rikstäckande blir de oläsliga. "
        "geo_karta_geometri är widgetens egen hämtare — anropa den "
        "inte för att svara i text, svaret är koordinater. "
        "FOLKMÄNGD — TVÅ KÄLLOR MED OLIKA UPPGIFT: geo_kommun_folkmangd_* "
        "läser SCB:s historiska publikation 1950-2024, där samtliga år är "
        "omräknade till en och samma kommunindelning. Det är dess enda "
        "skäl att ligga lokalt: PxWeb ger varje år i det årets gränser, "
        "så en lång serie därifrån blandar befolkningsförändring med "
        "gränsdragning. Använd den här serien för utveckling över tid. "
        "För aktuell folkmängd — senaste året, eller ett år efter seriens "
        "slut — gå till doa-pxweb-2 och SCB:s befolkningstabeller, som "
        "uppdateras löpande. Serien här flyttas fram en gång om året med "
        "geo_synka_kommun_folkmangd. "
        "INDELNINGSÅR: kommunindelningen ändras över tid. Verktyg som tar "
        "`indelning_ar` tolkar data i den årgångens gränser. Jämför aldrig "
        "folkmängdsserier över olika indelning_ar — skillnaden blir "
        "gränsdragning, inte befolkningsförändring. "
        "TVÅ PERSPEKTIV: geo_lista_* och geo_kommun_info svarar på 'dela "
        "upp på X'. geo_filtrera_kommuner svarar på 'vilka enheter "
        "uppfyller kriterier' och är kompositionell — den tar flera "
        "klassificeringssystem i samma fråga. "
        "SKRIVANDE VERKTYG: geo_synka_*, geo_lagg_till_kommuner och "
        "geo_bygg_valkrets_geom skriver till databasen. De är idempotenta "
        "upsertar, men hämtar stora filer från SCB och val.se och kan ta "
        "minuter. Kör dem inte spekulativt — bara när användaren bett om "
        "en uppdatering, eller när ett läsverktyg rapporterat tom tabell. "
        "SVARSSTORLEK: geo_kommun_folkmangd_matris över alla 290 kommuner "
        "× 75 år överskrider svarsgränsen. Avgränsa på kommun eller "
        "årsintervall, eller använd geo_kommun_folkmangd_topp. "
        "GEOMETRI: geo_*_geom och geo_punkt_till_* kräver PostGIS. Utan "
        "den extensionen felar de — det är en konfigurationsfråga, inte "
        "saknad data."
    ),
    cache_hints=CACHE_HINTAR,
    extensions=[TasksExtension(TASKBARA_VERKTYG)],
)


# ============================================================================
# Redovisningsperspektiv
# ============================================================================


@mcp.tool(title="Lista län", annotations=LASNING_DB)
async def geo_lista_lan(
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[Lan]:
    """Listar alla län — kod, namn och länsbokstav. 21 stycken."""
    rader = await indelningar.lista_lan()
    return Paket[Lan](**paginering.paginera(rader, sida=sida, per_sida=per_sida))


@mcp.tool(title="Lista regioner", annotations=LASNING_DB)
async def geo_lista_regioner(
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[Region]:
    """Listar alla regioner — kod och namn. 21 stycken."""
    rader = await indelningar.lista_regioner()
    return Paket[Region](**paginering.paginera(rader, sida=sida, per_sida=per_sida))


@mcp.tool(title="Lista kommuner", annotations=LASNING_DB)
async def geo_lista_kommuner(
    lan_kod: Annotated[
        str | None,
        Field(description="Tvåsiffrig länskod, t.ex. '01'", pattern=r"^\d{2}$"),
    ] = None,
    region_kod: Annotated[
        str | None, Field(description="Tvåsiffrig regionkod")
    ] = None,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[Kommun]:
    """Listar kommuner, optionellt filtrerat på län eller region.

    Utan filter returneras alla kommuner i `kommun`-tabellen. Tabellen
    seedas via `geo_lagg_till_kommuner` (manuellt) eller via en kommande
    SCB-synk.
    """
    rader = await indelningar.lista_kommuner(
        lan_kod=lan_kod, region_kod=region_kod
    )
    return Paket[Kommun](**paginering.paginera(rader, sida=sida, per_sida=per_sida))


@mcp.tool(title="Hämta kommuninformation", annotations=LASNING_DB)
async def geo_kommun_info(
    kommun_kod: Annotated[
        str,
        Field(
            description="Fyrsiffrig kommunkod med inledande nolla, t.ex. '0180'",
            pattern=r"^\d{4}$",
        ),
    ],
) -> dict[str, Any] | None:
    """Detaljvy för en kommun — basinfo plus alla aktuella klassificeringar.

    "Aktuell" är senaste år per system, vilket inte nödvändigtvis är
    samma år över system. Returnerar None om kommunen varken finns i
    `kommun`-tabellen eller är klassificerad i något system.
    """
    return await indelningar.kommun_info(kommun_kod)


@mcp.tool(title="Lista klassificeringssystem", annotations=LASNING_DB)
async def geo_lista_system(
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[KlassificeringsSystem]:
    """Listar alla registrerade klassificeringssystem.

    Varje rad bär systemets ID, beskrivning, källa, senaste synktid
    och senaste tillgängliga år.
    """
    rader = await indelningar.lista_system()
    return Paket[KlassificeringsSystem](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Lista grupper i ett system", annotations=LASNING_DB)
async def geo_lista_grupper(
    system: Annotated[
        str, Field(description="Systemets ID ur geo_lista_system")
    ],
    ar: Annotated[
        int | None,
        Field(description="Årgång; utelämnad betyder senaste tillgängliga"),
    ] = None,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[Grupp]:
    """Listar alla unika grupper i ett system för ett givet år.

    Saknat `ar` tolkas som senaste tillgängliga år. Returnerar tom lista
    om systemet inte finns eller saknar data.
    """
    rader = await indelningar.lista_grupper(system, ar=ar)
    return Paket[Grupp](**paginering.paginera(rader, sida=sida, per_sida=per_sida))


@mcp.tool(title="Lista kommuner i en grupp", annotations=LASNING_DB)
async def geo_lista_kommuner_i_grupp(
    system: Annotated[
        str, Field(description="Systemets ID ur geo_lista_system")
    ],
    grupp_kod: Annotated[
        str, Field(description="Gruppens kod ur geo_lista_grupper")
    ],
    ar: Annotated[
        int | None,
        Field(description="Årgång; utelämnad betyder senaste tillgängliga"),
    ] = None,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[KommunIGrupp]:
    """Returnerar kommuner som hör till en specifik grupp i ett system.

    Kommun-namnet kan vara None om `kommun`-tabellen ännu inte seedats —
    klassificeringen finns ändå.
    """
    rader = await indelningar.lista_kommuner_i_grupp(system, grupp_kod, ar=ar)
    return Paket[KommunIGrupp](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Översätt kommunkoder till grupptillhörighet", annotations=LASNING_DB)
async def geo_oversatt(
    kommun_koder: Annotated[
        list[str],
        Field(description="Fyrsiffriga kommunkoder med inledande nolla"),
    ],
    system: str,
    ar: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Mappar en lista kommunkoder till deras grupp i ett givet system.

    Returnerar `{kommun_kod: {grupp_kod, grupp_namn}}`. Kommuner som
    saknar klassificering i systemet utelämnas ur svaret.
    """
    return await indelningar.oversatt(kommun_koder, system, ar=ar)


# ============================================================================
# Urvalsperspektiv
# ============================================================================


@mcp.tool(title="Filtrera kommuner på kriterier", annotations=LASNING_DB)
async def geo_filtrera_kommuner(
    kriterier: Annotated[
        dict[str, str | list[str]],
        Field(
            description=(
                "{system: grupp_kod} eller {system: [grupp_kod, ...]} — "
                "kriterier över flera system måste uppfyllas samtidigt"
            )
        ),
    ],
    ar: Annotated[
        int | None,
        Field(description="Årgång; utelämnad betyder senaste tillgängliga"),
    ] = None,
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[str]:
    """Returnerar kommunkoder som uppfyller alla kriterier samtidigt.

    `kriterier` mappar system till grupp_kod (sträng) eller lista av
    grupp_kod:er (matchar någon av dem). Exempel:

        {"skr_kommungrupp": "C9", "tillvaxtverket": "landsbygdskommun"}

    ger kommuner som SKR klassar som C9 OCH som Tillväxtverket klassar
    som landsbygd. Tomt kriterier-objekt returnerar tom lista — fråga
    utan kriterier ska vara explicit i andra verktyg.
    """
    rader = await indelningar.filtrera_kommuner(kriterier, ar=ar)
    return Paket[str](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


# ============================================================================
# Synk och bootstrap
# ============================================================================


@mcp.tool(title="Lägg till kommuner i databasen", annotations=SYNK)
async def geo_lagg_till_kommuner(
    rader: list[tuple[str, str, str | None]]
) -> dict[str, int]:
    """Bootstrap: lägger till eller uppdaterar rader i `kommun`-tabellen.

    `rader` är `[(kod, namn, lan_kod), ...]`. Idempotent — befintliga
    kommuner uppdateras. Avsedd för att seeda enstaka kommuner inför
    demos och tester innan en full SCB-baserad kommunlista finns.
    """
    antal = await indelningar.lagg_till_kommuner(rader)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka SKR:s kommungruppsindelning till DB", annotations=SYNK)
async def geo_synka_skr_kommungrupp(
    url: str | None = None,
    rader: list[tuple[str, str, str]] | None = None,
    ar: int = 2023,
) -> dict[str, int]:
    """Synkar SKR:s kommungruppsindelning till `kommun_klassificering`.

    Källalternativ i fallande prioritet:
        1. `rader` direkt — `[(kommun_kod, grupp_kod, grupp_namn), ...]`.
        2. `url` — SKR:s Excel-fil. Bladet "Bilaga1 Lista alla kommuner"
           läses och kolumnerna Gruppkod, Kommunkod, Kommungrupp 2023
           används direkt.
        3. Env-variabeln `DOA_SKR_KOMMUNGRUPP_URL`.

    Saknas alla tre kastas ValueError. Returnerar antalet skrivna rader.
    """
    antal = await indelningar.synka_skr_kommungrupp(
        rader=rader, ar=ar, url=url
    )
    await synk.journalfor("skr_kommungrupp", url, antal, kvittera=rader is None)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka Tillväxtverkets FA-regioner till DB", annotations=SYNK)
async def geo_synka_tillvaxtverket_fa_region(
    url: str | None = None,
    rader: list[tuple[str, str, str]] | None = None,
    ar: int = 2025,
    seeda_kommuner: bool = True,
) -> dict[str, int]:
    """Synkar Tillväxtverkets FA-regioner till `kommun_klassificering`.

    FA-regioner (funktionella analysregioner) är kommuner som hänger
    samman ekonomiskt via pendling — 60 regioner i FA25 (2025 års
    indelning) som täcker alla 290 kommuner.

    Källalternativ i fallande prioritet:
        1. `rader` — `[(kommun_kod, fa_nr, fa_namn), ...]`
        2. `url` — Tillväxtverkets `FA25 (...).xlsx`. Bladet `FA25` läses
           med kolumnerna `KommunKod`, `FA25Nr`, `FA25Namn`.
        3. Env-variabeln `DOA_TV_FA_REGION_URL`.

    Med `seeda_kommuner=True` (default) upsertas också kommunnamn i
    `kommun`-tabellen från filen — gratis bieffekt eftersom källan
    listar alla kommuner.
    """
    antal = await indelningar.synka_tillvaxtverket_fa_region(
        rader=rader, ar=ar, url=url, seeda_kommuner=seeda_kommuner
    )
    await synk.journalfor("tillvaxtverket_fa", url, antal, kvittera=rader is None)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka Tillväxtverkets kommuntyper till DB", annotations=SYNK)
async def geo_synka_tillvaxtverket_kommuntyper(
    niva: str,
    url: str | None = None,
    rader: list[tuple[str, str, str]] | None = None,
    ar: int = 2026,
    seeda_kommuner: bool = True,
    seeda_lan: bool = True,
) -> dict[str, int]:
    """Synkar Tillväxtverkets kommuntyper (stad/landsbygd).

    Filen publiceras med två klassifikationsnivåer:
        SL3 — grov indelning i 3 grupper
        SL6 — fin indelning i 6 grupper

    `niva` styr vilken som synkas. Anropa funktionen en gång per nivå
    du vill ha — de blir två olika system i registret:
    `tillvaxtverket_sl3` respektive `tillvaxtverket_sl6`.

    Källalternativ i fallande prioritet:
        1. `rader` — `[(kommun_kod, grupp_kod, grupp_namn), ...]`
        2. `url` — Tillväxtverkets `KommuntyperStadLand_2026.xlsx`. Bladet
           `KommuntyperSL2026` läses med rätt kolumner per nivå.
        3. Env-variabeln `DOA_TV_KOMMUNTYPER_URL`.

    Med `seeda_kommuner=True` och `seeda_lan=True` (default) fylls
    `kommun`- och `lan`-tabellerna samtidigt — bekvämt eftersom filen
    bär Län kod, Län namn, Kommunkod och Kommunnamn för alla 290.
    """
    antal = await indelningar.synka_tillvaxtverket_kommuntyper(
        niva=niva,
        rader=rader,
        ar=ar,
        url=url,
        seeda_kommuner=seeda_kommuner,
        seeda_lan=seeda_lan,
    )
    await synk.journalfor("tillvaxtverket_kommuntyper", url, antal, kvittera=rader is None)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka klassificeringsrader till DB", annotations=SYNK)
async def geo_synka_klassificering_rader(
    system: str,
    system_namn: str,
    kalla: str,
    beskrivning: str,
    rader: list[tuple[str, str, str]],
    ar: int,
) -> dict[str, int]:
    """Generisk synk av en klassificering från färdiga rader.

    `rader` är `[(kommun_kod, grupp_kod, grupp_namn), ...]`. Funktionen
    registrerar `system` i klassificeringssystem-tabellen, upsertar
    raderna och uppdaterar senaste synktid. Idempotent.

    Användbar för system som inte har en specifik parser — anroparen
    läser källfilen och normaliserar utanför. För SKR finns en
    specialiserad `geo_synka_skr_kommungrupp` med inbyggd grupp-
    koddatabas och kolumn-autodetektion.

    Förslag på `system`-id:n:
        "tillvaxtverket"      Tillväxtverkets kommunindelning
        "fa_region"           Tillväxtanalys FA-regioner
        "h_region"            SCB:s H-region
    """
    antal = await indelningar.synka_klassificering_rader(
        system=system,
        system_namn=system_namn,
        kalla=kalla,
        beskrivning=beskrivning,
        rader=rader,
        ar=ar,
    )
    return {"antal_skrivna": antal}


# ============================================================================
# Administrativa indelningar — hardkodade synktools + DeSO/RegSO
# ============================================================================


@mcp.tool(title="Synka regionkatalogen till DB", annotations=SYNK)
async def geo_synka_regioner() -> dict[str, int]:
    """Sätter de 21 regionerna i `region`-tabellen och fyller i
    `kommun.region_kod` där det saknas (region-kod = län-kod för standard-
    indelningen). Hardkodad data — stabil sedan 2019.
    """
    antal = await indelningar.synka_regioner()
    await synk.journalfor("regioner", None, antal)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka riksdagsvalkretsar till DB", annotations=SYNK)
async def geo_synka_valkretsar(ar: int = 2022) -> dict[str, int]:
    """Sätter de 29 riksdagsvalkretsarna för ett valår. Hardkodad — stabil
    sedan 2018 års valreform. Idempotent på (kod, ar).
    """
    antal = await indelningar.synka_valkretsar(ar=ar)
    await synk.journalfor("valkretsar", None, antal)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka NUTS-indelningen till DB", annotations=SYNK)
async def geo_synka_nuts() -> dict[str, int]:
    """Genererar NUTS-tabellen (tre rader per kommun — nivå 1, 2 och 3)
    från hardkodad katalog joinad mot kommunens `lan_kod`. Aktuell version:
    NUTS 2021.

    Kräver att `kommun`-tabellen är seedad — körs alltid efter
    Tillväxtverkets kommuntyp-synk (eller motsvarande).
    """
    antal = await indelningar.synka_nuts()
    await synk.journalfor("nuts", None, antal)
    return {"antal_skrivna": antal}


@mcp.tool(title="Synka SCB:s DeSO och RegSO till DB", annotations=SYNK)
async def geo_synka_deso_och_regso(
    url: str | None = None, ar: int = 2025
) -> dict[str, int]:
    """Synkar DeSO och RegSO från SCB:s kombinerade kopplings-xlsx.

    SCB publicerar en fil som mappar varje DeSO till RegSO och kommun.
    Aktuell version: `koppling-deso2025-regso2025_<datum>.xlsx`. URL ändras
    när SCB publicerar ny version — passa in den explicit eller sätt
    `DOA_DESO_REGSO_URL` i `.env`.

    Returnerar `{"antal_deso": ..., "antal_regso": ...}`.
    """
    resultat = await indelningar.synka_deso_och_regso(url=url, ar=ar)
    await synk.journalfor("deso_och_regso", url, resultat)
    return resultat


@mcp.tool(title="Synka postnummer till DB", annotations=SYNK)
async def geo_synka_postnummer(
    instans: str = "huwise",
    dataset_id: str = "geonames-postal-code",
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar svenska postnummer från ett Huwise-dataset.

    Standardkällan är Geonames-postnummerdatasetet på hub.huwise.com
    (instans `huwise`). Punktkoordinater, ej polygongränser — Geonames
    har inga officiella postnummerpolygoner.

    Källfältets `admin_code2` är SCB:s 4-siffriga kommunkod när den finns.
    Returnerar `{"antal_postnummer": ..., "antal_postnummer_kommun": ...}`.
    """
    resultat = await indelningar.synka_postnummer_via_huwise(
        instans=instans, dataset_id=dataset_id,
        rapportera=framsteg.fran_context(ctx),
    )
    await synk.journalfor("postnummer", None, resultat)
    return resultat


@mcp.tool(title="Synka valdistrikt från val.se till DB", annotations=SYNK)
async def geo_synka_valdistrikt(
    url: str | None = None, ar: int = 2026
) -> dict[str, int]:
    """Synkar valdistrikt och alla tre valkretstyper från val.se.

    Valmyndigheten publicerar en xlsx-fil per valår med ca 6 000
    valdistrikt och varje distrikts mappning mot kommun, riksdagsvalkrets,
    kommunvalkrets och regionvalkrets. En enda synk fyller `valdistrikt`-
    tabellen plus rader i `valkrets` med typ `kommun` respektive `region`
    (riksdagsvalkretsarna hardkodade via `geo_synka_valkretsar`).

    URL ändras per valår — passa in explicit eller sätt
    `DOA_VALDISTRIKT_URL` i `.env`.

    Returnerar `{antal_valdistrikt, antal_kommunvalkretsar,
    antal_regionvalkretsar}`.
    """
    resultat = await indelningar.synka_valdistrikt(url=url, ar=ar)
    await synk.journalfor("valdistrikt", url, resultat)
    return resultat



# ============================================================================
# Historiska tidsserier — SCB folkmängd 1950–
# ============================================================================
#
# SCB:s retroaktivt harmoniserade serie fyller en lucka som PxWebApi inte
# täcker: dagens 290 kommuner bakåt över hela perioden 1950→. Datat
# levereras som en Excel-fil från scb.se och lagras lokalt — verktygen
# nedan slår mot DB:n, inte mot SCB live.


@mcp.tool(title="Folkmängdshistorik för en kommun", annotations=LASNING_DB)
async def geo_kommun_folkmangd_historik(
    kommun_kod: Annotated[
        str,
        Field(
            description="Fyrsiffrig kommunkod med inledande nolla, t.ex. '0180'",
            pattern=r"^\d{4}$",
        ),
    ],
    fran: int | None = None,
    till: int | None = None,
    indelning_ar: int | None = None,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[Folkmangdsrad]:
    """Folkmängd per år för en kommun (1950→ enligt SCB:s harmoniserade serie).

    `kommun_kod` är 4-siffrig SCB-kod (`"0180"` Stockholm). `fran` och
    `till` är inklusiva årsintervall. `indelning_ar` väljer vilken
    kommunindelning serien följer; saknas väljs den senaste lokalt
    lagrade.

    Användbar för longitudinella analyser där dagens kommungränser
    måste gälla bakåt (jämförelse Knivsta 1980 vs 2020 osv).
    Kompletterar PxWebApi 2 — `pxweb_2_hamta_data` mot TAB6571 ger
    finkornigt aktuellt, denna ger lång retroaktiv serie.

    Svaret är paginerat (default 500 rader/sida); en kommun har ~75 år
    så sida 1 räcker normalt.
    """
    rader = await folkmangd.hamta_kommun_folkmangd_historik(
        kommun_kod=kommun_kod, fran=fran, till=till, indelning_ar=indelning_ar,
    )
    return Paket[Folkmangdsrad](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Folkmängd ett givet år", annotations=LASNING_DB)
async def geo_kommun_folkmangd_aret(
    ar: int,
    indelning_ar: int | None = None,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[Folkmangdsrad]:
    """Folkmängd för alla kommuner ett givet år.

    Bredsida — alla 290 kommuner ett år. Bra för rangordning, kvartiler,
    karta. `indelning_ar` styr indelningsår; saknas väljs senaste.

    Källan är SCB:s historiska serie 1950-2024 i en gemensam indelning.
    Efterfrågas ett år bortom seriens slut finns det inte här — hämta det
    ur SCB:s löpande befolkningstabeller via doa-pxweb-2.

    Svaret är paginerat — 290 rader får plats på en sida.
    """
    rader = await folkmangd.hamta_aret_per_kommun(ar=ar, indelning_ar=indelning_ar)
    return Paket[Folkmangdsrad](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Folkmängdsmatris kommun × år", annotations=LASNING_DB)
async def geo_kommun_folkmangd_matris(
    fran: int | None = None,
    till: int | None = None,
    kommun_koder: list[str] | None = None,
    indelning_ar: int | None = None,
) -> dict[str, Any]:
    """Hela folkmängdsmatrisen i kompakt form — bredbild i ett anrop.

    När hela datasetet behövs samtidigt (rangordningar över tid,
    persistens-analys, percentil-mönster) är rad-per-cell-formen
    olämplig — 290 × 75 = 21 750 rader spränger klientens svarsgräns.
    Matris-formen ger ~150 KB för hela perioden och laddas direkt till
    pandas.

    Format:

        {
          "indelning_ar": 2025,
          "fran_ar":      1950,  "till_ar": 2024,
          "ar":           [1950, ..., 2024],
          "kommuner":     [{"kod": "0114", "namn": "Upplands Väsby"}, ...],
          "matris":       [[12744, 12812, ...], ...]
        }

    `matris[i][j]` = folkmängden för `kommuner[i]` år `ar[j]`. `None`
    om värdet saknas.

    Pandas:

        import pandas as pd
        df = pd.DataFrame(res["matris"],
                          index=[k["kod"] for k in res["kommuner"]],
                          columns=res["ar"])

    `fran`, `till` är inklusiva årsintervall. `kommun_koder` smalnar
    till en delmängd. Utan filter: hela matrisen, 290 × 75 år.
    """
    return await folkmangd.hamta_matris(
        fran=fran, till=till, kommun_koder=kommun_koder,
        indelning_ar=indelning_ar,
    )


@mcp.tool(title="Största eller minsta kommuner", annotations=LASNING_DB)
async def geo_kommun_folkmangd_topp(
    ar: int,
    antal: Annotated[
        int, Field(description="Antal kommuner i topplistan", ge=1, le=290)
    ] = 50,
    indelning_ar: int | None = None,
) -> Paket[Folkmangdsrad]:
    """Topp N kommuner efter folkmängd ett givet år — server-side sortering.

    Slipper transportera hela bredsidan (290 rader) när bara
    rangordningen behövs. SQL gör `ORDER BY folkmangd DESC LIMIT N` —
    svaret är `antal` rader med `rang` på varje.

    Användbart för "historisk topp"-frågor: jämföra topp-50 1950 vs
    topp-50 2024, hitta kommuner som ramlat ut, osv.
    """
    rader = await folkmangd.hamta_topp(
        ar=ar, antal=antal, indelning_ar=indelning_ar,
    )
    # Sorteringen och taket görs i SQL — kuvertet beskriver bara resultatet.
    return Paket[Folkmangdsrad](
        **paginering.paketera_forpaginerat(
            rader, total=len(rader), sida=1, per_sida=max(antal, 1)
        )
    )


@mcp.tool(title="Synka SCB:s folkmängdsserie till DB", annotations=SYNK)
async def geo_synka_kommun_folkmangd(
    url: str | None = None, indelning_ar: int = folkmangd.NUVARANDE_INDELNING_AR
) -> dict[str, Any]:
    """Synkar SCB:s historiska folkmängdsserie från scb.se till DB:n.

    Hämtar Excel-filen (1950→), parsar och upsertar mot
    `kommun_folkmangd`. Befintliga rader för samma
    `(kommun_kod, ar, indelning_ar)` skrivs över; rader under annat
    `indelning_ar` bevaras för spårbarhet.

    URL ändras vid SCB:s årliga uppdatering — passa in nya URL:n när
    det är dags, eller lämna tomt för den hårdkodade defaulten.

    Returnerar `{rader_skrivna, kommuner_matchade, fran_ar, till_ar,
    indelning_ar, kommuner_omatchade: [namn där matchning misslyckades]}`.
    """
    res = await folkmangd.synka_kommun_folkmangd(
        url=url, indelning_ar=indelning_ar
    )
    await synk.journalfor("kommun_folkmangd", url, res)
    return {
        "rader_skrivna": res.rader_skrivna,
        "kommuner_matchade": res.kommuner_matchade,
        "fran_ar": res.fran_ar,
        "till_ar": res.till_ar,
        "indelning_ar": res.indelning_ar,
        "kommuner_omatchade": res.kommuner_omatchade,
    }


# ============================================================================
# Geometri (kräver PostGIS)
# ============================================================================


@mcp.tool(title="Synka valdistriktsgeometrier till PostGIS", annotations=SYNK)
async def geo_synka_valdistrikt_geom(
    url: str | None = None,
    ar: int = 2026,
    kalla_crs: int = 3006,
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar valdistrikt-polygoner från val.se:s zip-arkiv.

    Filen är en zip med en geojson i SWEREF99 TM (EPSG:3006); PostGIS
    reprojicerar till WGS84 (4326) vid INSERT. Kräver Postgres-backend
    med PostGIS-extension; SQLite stöds inte för geometri-spåret.

    URL ändras per valår — sätt explicit eller via
    `DOA_VALDISTRIKT_GEOM_URL` i `.env`.

    Returnerar `{"antal_skrivna": N}`.
    """
    n = await geometrier.synka_valdistrikt_geom(
        url=url, ar=ar, kalla_crs=kalla_crs,
        rapportera=framsteg.fran_context(ctx),
    )
    await synk.journalfor("valdistrikt_geom", url, n)
    return {"antal_skrivna": n}


@mcp.tool(title="Synka kommungeometrier till PostGIS", annotations=SYNK)
async def geo_synka_kommun_geom(
    collection_id: str = "kommuner-2026",
    ar: int = 2026,
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar kommungeometrier från Lantmäteriets OGC API Features.

    Default-collectionen är `kommuner-2026`. Kräver `LANTMATERIET_API_NYCKEL`
    i `.env` — hämta nyckeln från `apimanager.lantmateriet.se/devportal`.
    """
    n = await geometrier.synka_kommun_geom(collection_id=collection_id, ar=ar,
        rapportera=framsteg.fran_context(ctx),
    )
    return {"antal_skrivna": n}


@mcp.tool(title="Synka längeometrier till PostGIS", annotations=SYNK)
async def geo_synka_lan_geom(
    collection_id: str = "lan-2026",
    ar: int = 2026,
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar länsgeometrier från Lantmäteriet."""
    n = await geometrier.synka_lan_geom(collection_id=collection_id, ar=ar,
        rapportera=framsteg.fran_context(ctx),
    )
    return {"antal_skrivna": n}


@mcp.tool(title="Synka regiongeometrier till PostGIS", annotations=SYNK)
async def geo_synka_region_geom(
    collection_id: str = "lan-2026",
    ar: int = 2026,
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar regiongeometrier (samma som län i den svenska standardindelningen)."""
    n = await geometrier.synka_region_geom(collection_id=collection_id, ar=ar,
        rapportera=framsteg.fran_context(ctx),
    )
    return {"antal_skrivna": n}


@mcp.tool(title="Synka DeSO-geometrier till PostGIS", annotations=SYNK)
async def geo_synka_deso_geom(
    type_name: str = "stat:DeSO_2025",
    ar: int = 2025,
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar DeSO-geometrier från SCB:s WFS (workspace stat).

    Default är `stat:DeSO_2025` som matchar kodsystemet i våra övriga
    deso-tabellen. För 2018 års indelning används `stat:DeSO_2018`.
    Ingen auth krävs — SCB:s WFS är öppen.
    """
    n = await geometrier.synka_deso_geom_via_wfs(type_name=type_name, ar=ar,
        rapportera=framsteg.fran_context(ctx),
    )
    await synk.journalfor("deso_geom", None, n)
    return {"antal_skrivna": n}


@mcp.tool(title="Synka RegSO-geometrier till PostGIS", annotations=SYNK)
async def geo_synka_regso_geom(
    type_name: str = "stat:RegSO_2025",
    ar: int = 2025,
    ctx: Context | None = None,
) -> dict[str, int]:
    """Synkar RegSO-geometrier från SCB:s WFS (workspace stat).

    Default är `stat:RegSO_2025`. För 2020 års indelning används
    `stat:RegSO_2020`.
    """
    n = await geometrier.synka_regso_geom_via_wfs(type_name=type_name, ar=ar,
        rapportera=framsteg.fran_context(ctx),
    )
    await synk.journalfor("regso_geom", None, n)
    return {"antal_skrivna": n}


@mcp.tool(title="Bygg valkretsgeometrier ur valdistrikt", annotations=SYNK)
async def geo_bygg_valkrets_geom(ar: int = 2026) -> dict[str, int]:
    """Bygger valkretspolygoner genom att unionera valdistrikt-geometrier.

    Tre typer skapas: riksdag (~29 polygoner), kommun (~314), region (~62).
    Kräver att `valdistrikt_geom` är populerad. PostGIS `ST_Union` gör
    själva arbetet — Python triggar bara SQL:en. Tar några sekunder per
    typ för 6 312 distrikt.

    Returnerar antal byggda polygoner per typ.
    """
    return await geometrier.bygg_valkrets_geom_fran_distrikt(ar=ar)


@mcp.tool(title="Slå upp kommun för en koordinat", annotations=LASNING_DB)
async def geo_punkt_till_kommun(
    latitud: float, longitud: float, ar: int = 2026
) -> dict[str, str | None]:
    """Punkt-i-polygon: vilken kommun ligger en WGS84-koordinat i?

    Kräver att `kommun_geom`-tabellen är populerad via `geo_synka_kommun_geom`.
    Returnerar kommun-kod, namn, län och region — eller None-värden om
    koordinaten ligger utomlands eller i havet.
    """
    sql = (
        "SELECT k.kod, k.namn, l.namn AS lan, r.namn AS region "
        f"FROM {db.prefix()}kommun_geom kg "
        f"JOIN {db.prefix()}kommun k ON k.kod = kg.kod "
        f"LEFT JOIN {db.prefix()}lan l ON l.kod = k.lan_kod "
        f"LEFT JOIN {db.prefix()}region r ON r.kod = k.region_kod "
        f"WHERE ST_Contains(kg.geom, ST_SetSRID(ST_MakePoint({db.ph(1)}, {db.ph(2)}), 4326)) "
        f"  AND kg.ar = {db.ph(3)} "
        "LIMIT 1"
    )
    async with db.hamta_db() as anslutning:
        rad = await anslutning.fetchrow(sql, longitud, latitud, ar)
    if rad is None:
        return {"kod": None, "namn": None, "lan": None, "region": None}
    return rad


@mcp.tool(title="Slå upp valdistrikt för en koordinat", annotations=LASNING_DB)
async def geo_punkt_till_valdistrikt(
    latitud: float, longitud: float, ar: int = 2026
) -> dict[str, str | None]:
    """Punkt-i-polygon: vilket valdistrikt ligger en given WGS84-koordinat i?

    Returnerar valdistriktskod, distriktets namn, kommun och riksdags-
    valkrets — eller None-värden om koordinaten ligger utanför alla
    distrikt (utomlands eller i havet).
    """
    sql = (
        "SELECT vd.kod, vd.namn AS distrikt, "
        "       k.kod AS kommun_kod, k.namn AS kommun, "
        "       vk.namn AS riksdagsvalkrets "
        f"FROM {db.prefix()}valdistrikt_geom vg "
        f"JOIN {db.prefix()}valdistrikt vd ON vd.kod = vg.kod AND vd.ar = vg.ar "
        f"LEFT JOIN {db.prefix()}kommun k ON k.kod = vd.kommun_kod "
        f"LEFT JOIN {db.prefix()}valkrets vk ON vk.kod = vd.riksdagsvalkrets_kod "
        f"  AND vk.ar = vd.ar AND vk.typ = 'riksdag' "
        f"WHERE vg.ar = {db.ph(1)} "
        f"  AND ST_Contains(vg.geom, ST_SetSRID(ST_MakePoint({db.ph(2)}, {db.ph(3)}), 4326)) "
        "LIMIT 1"
    )
    async with db.hamta_db() as anslutning:
        rad = await anslutning.fetchrow(sql, ar, longitud, latitud)
    if rad is None:
        return {"kod": None, "distrikt": None, "kommun_kod": None,
                "kommun": None, "riksdagsvalkrets": None}
    return rad


# ============================================================================
# Ingångspunkt
# ============================================================================

APP_KARTA_URI = "ui://doa-geo/kommunkarta.html"
# Samma mall, men utan inbakad geometri — widgeten hämtar den för det
# urval som efterfrågas. Rikskartan över kommuner behåller sin inbakade
# geometri eftersom 132 kB är billigare än en extra rundtur.
APP_OMRADE_URI = "ui://doa-geo/omradeskarta.html"
APP_MIMETYPE = "text/html;profile=mcp-app"

_APP_MALL = _Path(__file__).parent / "appar" / "karta.html"

# Delar under tröskeln utelämnas — Sveriges skärgård ger 21 477 delpolygoner,
# varav de allra flesta är osynliga i en nationell översikt. Varje kommuns
# största del behålls alltid, annars försvinner de rena ö-kommunerna helt.
_APP_MIN_DELAREA_KM2 = 25.0
_APP_TOLERANS_GRADER = 0.02

_APP_GEOMETRI_SQL = f"""
WITH delar AS (
    SELECT k.kod, p.geom,
           ST_Area(p.geom::geography) / 1e6 AS km2,
           row_number() OVER (PARTITION BY k.kod
                              ORDER BY ST_Area(p.geom::geography) DESC) AS rang
    FROM mv_kommun_land k,
         LATERAL (SELECT (ST_Dump(k.geom)).geom) p
)
SELECT kod,
       ST_AsGeoJSON(ST_SimplifyPreserveTopology(ST_Collect(geom), $2), 4) AS gj
FROM delar
WHERE km2 >= $1 OR rang = 1
GROUP BY kod
ORDER BY kod
"""


async def _app_geometri() -> str:
    """Bygger widgetens geometri som kompakt JSON.

    Formen är `[{"k": kommunkod, "d": [[[lon, lat], ...], ...]}]` — ringar
    utan GeoJSON-omslag, eftersom widgeten bara ritar dem och varje sparat
    tecken räknas i en resurs som hämtas i sin helhet.
    """
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            _APP_GEOMETRI_SQL, _APP_MIN_DELAREA_KM2, _APP_TOLERANS_GRADER
        )

    kommuner = []
    for rad in rader:
        geo = json.loads(rad["gj"])
        # MultiPolygon ger [[ring, hål...], ...]; Polygon ger [ring, hål...].
        # Widgeten ritar bara yttre ringar — hål syns inte i en choroplet.
        if geo["type"] == "MultiPolygon":
            ringar = [d[0] for d in geo["coordinates"]]
        else:
            ringar = [geo["coordinates"][0]]
        kommuner.append({"k": rad["kod"], "d": ringar})
    return json.dumps(kommuner, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Kartlager på begäran — de fina indelningarna
# ---------------------------------------------------------------------------
# DeSO, RegSO och valdistrikt kan inte bakas in i widgetresursen. En enda
# kommuns DeSO i full detalj är större än hela rikskartan (Stockholm 323 kB
# mot kommunkartans 132 kB), och nationellt är de 839 kB. Men på den
# detaljnivån vill man nästan alltid se en eller ett fåtal kommuner, inte
# hela landet — så geometrin hämtas för det urval som efterfrågas.
#
# Koderna är hierarkiska: ett DeSO-, RegSO- eller valdistriktsnummer inleds
# med kommunens fyra siffror, och en kommunkod med länets två. Ett urval
# blir därför ett prefixvillkor, utan join.

OMRADESTYPER = {
    # typ: (geometritabell, namntabell eller None)
    "kommun": ("mv_kommun_land", "kommun"),
    "lan": ("mv_lan_land", "lan"),
    "deso": ("deso_geom", "deso"),
    "regso": ("regso_geom", "regso"),
    "valdistrikt": ("valdistrikt_geom", "valdistrikt"),
}

# Rikskartan förenklar med 0,02 grader över Sveriges 13,7 graders utbredning.
# Kvoten hålls konstant så att ett kommunutsnitt får samma visuella
# upplösning som riket — en konstant tolerans hade antingen suddat ut en
# stadskarta eller gjort rikskartan tiofalt tyngre än den behöver vara.
_UTSNITTSKVOT = 686.0
_MINSTA_TOLERANS = 0.00005          # ~5 m; under detta ger förenklingen inget
_STORSTA_TOLERANS = 0.02            # rikskartans värde

# Delytor under tröskeln utelämnas — skärgården ger tiotusentals
# delpolygoner som ändå inte syns. Tröskeln följer toleransen, så samma
# formel ger rikskartans beprövade 25 km² och en stadskarta 2,5 hektar.
def _minsta_delarea_km2(tolerans_grader: float) -> float:
    return (tolerans_grader * 111.0) ** 2 * 5.0


def _prefixvillkor(
    inom_kommun: list[str] | None, inom_lan: list[str] | None, koder: list[str] | None
) -> tuple[str, list]:
    """Bygger WHERE-villkoret för ett urval. Tomt urval betyder hela riket."""
    if koder:
        return "kod = ANY($1)", [koder]
    prefix = list(inom_kommun or []) + list(inom_lan or [])
    if not prefix:
        return "TRUE", []
    # `kod LIKE prefix || '%'` skrivet så att en enda parameter räcker.
    return "EXISTS (SELECT 1 FROM unnest($1::text[]) p WHERE kod LIKE p || '%')", [prefix]


async def hamta_kartgeometri(
    omradestyp: str,
    inom_kommun: list[str] | None = None,
    inom_lan: list[str] | None = None,
    koder: list[str] | None = None,
) -> dict[str, Any]:
    """Hämtar geometri för ett urval, förenklad efter utsnittets storlek."""
    if omradestyp not in OMRADESTYPER:
        raise ValueError(
            f"okänd områdestyp {omradestyp!r} — giltiga: "
            + ", ".join(sorted(OMRADESTYPER))
        )
    geomtabell, namntabell = OMRADESTYPER[omradestyp]
    villkor, parametrar = _prefixvillkor(inom_kommun, inom_lan, koder)

    async with db.hamta_db() as anslutning:
        utbredning = await anslutning.fetchrow(
            f"SELECT ST_XMax(e) - ST_XMin(e) AS bredd, ST_YMax(e) - ST_YMin(e) AS hojd "
            f"FROM (SELECT ST_Extent(geom) e FROM {geomtabell} WHERE {villkor}) x",
            *parametrar,
        )
        if utbredning is None or utbredning["bredd"] is None:
            return {
                "omradestyp": omradestyp, "tolerans": None,
                "attribution": LICENS_ATTRIBUTION, "omraden": [],
            }

        spann = max(float(utbredning["bredd"]), float(utbredning["hojd"]))
        tolerans = min(
            _STORSTA_TOLERANS, max(_MINSTA_TOLERANS, spann / _UTSNITTSKVOT)
        )
        namnled = (
            f"LEFT JOIN {namntabell} n ON n.kod = d.kod" if namntabell else ""
        )
        # DeSO har koder, inte namn: `deso.namn` upprepar bara koden. Bokstaven
        # på femte positionen bär det som faktiskt säger något — landsbygd,
        # tätort utanför centralort, eller centralort — så den översätts här
        # i stället för att kartan visar koden två gånger.
        if omradestyp == "deso":
            namnfalt = (
                "CASE substring(d.kod, 5, 1) "
                "WHEN 'A' THEN 'Landsbygd' "
                "WHEN 'B' THEN 'Tätort utanför centralort' "
                "WHEN 'C' THEN 'Centralort' END"
            )
            namnled = ""
        else:
            namnfalt = "n.namn" if namntabell else "NULL"
        rader = await anslutning.fetch(
            f"""
            WITH valda AS (
                SELECT kod, geom FROM {geomtabell} WHERE {villkor}
            ), delar AS (
                SELECT v.kod, p.geom,
                       ST_Area(p.geom::geography) / 1e6 AS km2,
                       row_number() OVER (PARTITION BY v.kod
                                          ORDER BY ST_Area(p.geom::geography) DESC) AS rang
                FROM valda v, LATERAL (SELECT (ST_Dump(v.geom)).geom) p
            ), samlade AS (
                SELECT kod,
                       ST_AsGeoJSON(
                           ST_SimplifyPreserveTopology(ST_Collect(geom), ${len(parametrar) + 1}), 5
                       ) AS gj
                FROM delar
                WHERE km2 >= ${len(parametrar) + 2} OR rang = 1
                GROUP BY kod
            )
            SELECT d.kod, {namnfalt} AS namn, d.gj
            FROM samlade d {namnled}
            ORDER BY d.kod
            """,
            *parametrar, tolerans, _minsta_delarea_km2(tolerans),
        )

    omraden = []
    for rad in rader:
        geo = json.loads(rad["gj"])
        # MultiPolygon ger [[ring, hål...], ...]; Polygon ger [ring, hål...].
        # Widgeten ritar bara yttre ringar — hål syns inte i en choroplet.
        if geo["type"] == "MultiPolygon":
            ringar = [del_[0] for del_ in geo["coordinates"]]
        else:
            ringar = [geo["coordinates"][0]]
        omraden.append({"k": rad["kod"], "n": rad["namn"], "d": ringar})

    return {
        "omradestyp": omradestyp,
        "tolerans": round(tolerans, 6),
        "attribution": LICENS_ATTRIBUTION,
        "omraden": omraden,
    }


@mcp.tool(title="Kontrollera om källorna publicerat nytt", annotations=LASNING_EXTERN)
async def geo_bevaka_kallor(
    dataset: Annotated[
        str | None,
        Field(description="Ett enskilt dataset; utelämnat kontrollerar alla"),
    ] = None,
) -> list[dict[str, Any]]:
    """Frågar källorna om de publicerat något nyare än vad vi hämtat.

    `geo_synkstatus` svarar på hur gammal vår kopia är. Det räcker inte för
    de händelsestyrda dataseten — kommunindelningen, SKR:s kommungrupper,
    NUTS — som blir inaktuella när myndigheten publicerar, inte när tiden
    går. En åldersjämförelse kan inte veta att SKR gjort en revidering.

    Kontrollen jämför källans egen signatur: ETag, Last-Modified och storlek
    ur ett HEAD-anrop. Ändras någon av dem har filen bytts ut.

    Verktyget synkar aldrig något. Det säger vad som ändrats och vilket
    verktyg som hämtar in det.

    Dataset vars synk tar URL:en som parameter eller ur .env får ingen
    signatur förrän en synk körts med en URL. De redovisas som "ingen känd
    URL" i stället för att tigande se ut som att allt är i sin ordning.
    """
    return await bevakning.kontrollera(dataset)


@mcp.tool(title="Visa hur färsk den lagrade datan är", annotations=LASNING_DB)
async def geo_synkstatus() -> list[dict[str, Any]]:
    """Ålder och färskhetsbedömning för allt suiten lagrar lokalt.

    Lagrad data är ett löfte om färskhet. Antalet rader säger ingenting om
    det — en tabell med rätt antal rader kan vara tre år gammal. Kör det
    här före en analys som bygger på lokala tabeller, och när ett svar ser
    oväntat ut.

    Varje post bär `kadens_dagar` och `kadens_grund`. Grunden säger hur
    kadensen är satt: `dokumenterad` betyder att källan anger sin takt,
    `uppmätt` att vi mätt den i datat, `händelsestyrd` att datat följer val
    eller reformer och alltså inte blir gammalt av att tiden går. En
    `antagen` kadens är en kvalificerad gissning och ska behandlas som det.

    `verktyg` säger vad som synkar om datasetet.
    """
    return await synkstatus.las_status()


@mcp.resource(
    APP_KARTA_URI,
    name="Kommunkarta",
    title="Interaktiv kommunkarta",
    description="Choroplet över Sveriges kommuner, färgad efter folkmängd.",
    mime_type=APP_MIMETYPE,
)
async def app_kommunkarta() -> str:
    """Serverar widgeten med geometrin inbakad.

    Geometrin läses ur databasen vid varje hämtning i stället för att
    förberäknas till en fil — då följer kartan automatiskt med när
    kustlinjen eller DeSO-synken byggts om.
    """
    mall = _APP_MALL.read_text(encoding="utf-8")
    return (
        mall
        .replace("__GEOMETRI__", await _app_geometri())
        .replace("__ATTRIBUTION__", json.dumps(LICENS_ATTRIBUTION))
    )


@mcp.tool(
    title="Visa folkmängd på karta",
    annotations=LASNING_DB,
    meta={"ui": {"resourceUri": APP_KARTA_URI}},
)
async def geo_karta_folkmangd(
    ar: Annotated[
        int, Field(description="Kalenderår att visa", ge=1950, le=2030)
    ] = 2024,
    indelning_ar: Annotated[
        int | None,
        Field(description="Kommunindelningens årgång; utelämnad ger senaste"),
    ] = None,
) -> Paket[Folkmangdsrad]:
    """Visar folkmängden per kommun som en interaktiv karta.

    Klienter som stöder MCP Apps renderar svaret som en choroplet där varje
    kommun färgas efter folkmängd och namnet visas vid hovring. Övriga
    klienter får samma data som ett vanligt pagineringspaket — verktyget
    fungerar alltså även utan widgetstöd.

    Geometrin följer inte med svaret utan ligger i widgetresursen; det här
    verktyget levererar bara statistiken.

    Statistiken kommer ur SCB:s historiska serie 1950-2024 i en gemensam
    kommunindelning, inte ur en löpande hämtning. Kartan visar alltså inte
    ett år som ligger efter seriens slut.
    """
    rader = await folkmangd.hamta_aret_per_kommun(ar=ar, indelning_ar=indelning_ar)
    return Paket[Folkmangdsrad](
        **paginering.paketera_forpaginerat(
            rader, total=len(rader), sida=1, per_sida=max(len(rader), 1)
        )
    )


@mcp.resource(
    APP_OMRADE_URI,
    name="Områdeskarta",
    title="Interaktiv karta över valfri indelning",
    description=(
        "Choroplet över DeSO, RegSO, valdistrikt, kommun eller län. "
        "Geometrin hämtas för det urval som visas."
    ),
    mime_type=APP_MIMETYPE,
)
async def app_omradeskarta() -> str:
    """Serverar widgeten utan geometri — den hämtas av widgeten själv.

    `__GEOMETRI__` blir `null`, vilket får widgeten att anropa
    `geo_karta_geometri` över värdbryggan när den vet vad som ska visas.
    Geometrin passerar alltså aldrig modellens kontext.
    """
    mall = _APP_MALL.read_text(encoding="utf-8")
    return (
        mall
        .replace("__GEOMETRI__", "null")
        .replace("__ATTRIBUTION__", json.dumps(LICENS_ATTRIBUTION))
    )


@mcp.tool(title="Hämta kartgeometri för ett urval", annotations=LASNING_DB)
async def geo_karta_geometri(
    omradestyp: Annotated[
        str,
        Field(description="kommun, lan, deso, regso eller valdistrikt"),
    ],
    inom_kommun: Annotated[
        list[str] | None,
        Field(description="Fyrsiffriga kommunkoder att avgränsa till"),
    ] = None,
    inom_lan: Annotated[
        list[str] | None,
        Field(description="Tvåsiffriga länskoder att avgränsa till"),
    ] = None,
    koder: Annotated[
        list[str] | None,
        Field(description="Exakta områdeskoder i stället för en avgränsning"),
    ] = None,
) -> dict[str, Any]:
    """Geometri för ett urval områden, förenklad efter utsnittets storlek.

    Kartwidgetens egen hämtare — den anropar verktyget över värdbryggan när
    den vet vad som ska ritas, så geometrin går till widgeten och inte genom
    modellens kontext. Anropa det inte för att svara på en fråga i text;
    svaret är koordinater, inte statistik.

    Utan avgränsning returneras hela riket. Det är rimligt för `kommun` och
    `lan`, men `deso` (6 160), `regso` (3 363) och `valdistrikt` (6 312)
    ska nästan alltid begränsas med `inom_kommun` eller `inom_lan`.

    Svaret är `{omradestyp, tolerans, attribution, omraden}` där varje
    område är `{k: kod, n: namn, d: [ytterringar]}`.
    """
    return await hamta_kartgeometri(
        omradestyp,
        inom_kommun=inom_kommun,
        inom_lan=inom_lan,
        koder=koder,
    )


@mcp.tool(
    title="Visa värden på karta",
    annotations=LASNING_DB,
    meta={"ui": {"resourceUri": APP_OMRADE_URI}},
)
async def geo_karta_omraden(
    omradestyp: Annotated[
        str,
        Field(description="kommun, lan, deso, regso eller valdistrikt"),
    ],
    varden: Annotated[
        dict[str, float],
        Field(description="Områdeskod till värde, t.ex. {'0180': 996000}"),
    ],
    etikett: Annotated[
        str,
        Field(description="Vad värdena visar, t.ex. 'Folkmängd 2024'"),
    ],
    inom_kommun: Annotated[
        list[str] | None,
        Field(description="Fyrsiffriga kommunkoder att avgränsa kartan till"),
    ] = None,
    inom_lan: Annotated[
        list[str] | None,
        Field(description="Tvåsiffriga länskoder att avgränsa kartan till"),
    ] = None,
) -> dict[str, Any]:
    """Ritar valfri statistik som choroplet över valfri indelning.

    Servern håller ingen statistik för de fina indelningarna och ska inte
    göra det — hämta värdena där de finns (doa-pxweb-2 för SCB:s
    RegSO-tabeller, doa-kolada för kommun och region) och skicka in dem
    här. Koderna måste vara källans egna: SCB:s RegSO-koder (`0114R001`)
    och DeSO-koder (`0114A0010`) är desamma som suitens, så ingen
    översättning behövs.

    Widgeten hämtar geometrin själv för det urval du anger. Avgränsa alltid
    med `inom_kommun` eller `inom_lan` för deso, regso och valdistrikt —
    en rikstäckande DeSO-karta har 6 160 områden och blir oläslig långt
    innan den blir stor.

    Klienter utan widgetstöd får värdena tillbaka som de är, med en notis
    om att kartan kräver MCP Apps.
    """
    if omradestyp not in OMRADESTYPER:
        raise ValueError(
            f"okänd områdestyp {omradestyp!r} — giltiga: "
            + ", ".join(sorted(OMRADESTYPER))
        )
    return {
        "omradestyp": omradestyp,
        "etikett": etikett,
        "urval": {"inom_kommun": inom_kommun, "inom_lan": inom_lan},
        "antal": len(varden),
        "varden": [{"kod": k, "varde": v} for k, v in varden.items()],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Schemat skapas idempotent vid varje uppstart. starta() wrappar
    # anropet så att servern går upp i klienten även om DB är nere.
    starta(mcp, initiera=db.initiera_schema)
