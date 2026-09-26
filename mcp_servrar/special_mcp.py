# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""MCP-server för myndigheter med var sitt mindre statistik-API.

Flera myndigheter, samlade i en server eftersom ingen ensam motiverar en
egen process:

    Skolverket       skolenheter och utbildningsinfo (kommunkod)
    Trafikanalys     transportstatistik (fordon, kollektivtrafik, skador)
    Socialstyrelsen  hälso- och socialtjänststatistik (länskod)
    Brå              anmälda brott per kommun/region och år (SolWebb-skrap)
    Försäkringskassan socialförsäkringsstatistik (öppna data, JSON)
    KB Bibstat       Sveriges officiella biblioteksstatistik (RDF Data Cube)
    Riksbanken       räntor och valutakurser (SWEA-API)
    Migrationsverket asyl, tillstånd och mottagande (Excelfiler, inget API)

Transport och autentisering hanteras av data_och_analys.infra.mcp_transport.


Verktyg:
    skolverket_lista_skolenheter
    skolverket_hamta_skolenhet
    skolverket_sok_planerade_utbildningar
    skolverket_hamta_skolenhet_statistik
    trafa_lista_produkter
    trafa_hamta_struktur
    trafa_hamta_data
    socialstyrelsen_lista_databaser
    socialstyrelsen_hamta_dimensioner
    socialstyrelsen_hamta_dimensionsvarden
    socialstyrelsen_hamta_resultat
    bra_lista_brottstyper
    bra_lista_regioner
    bra_lista_perioder
    bra_hamta_statistik
    fk_lista_dataset
    fk_hamta_dataset
    bibstat_hamta_observationer
    riksbanken_lista_serier
    riksbanken_hamta_serie
    riksbanken_hamta_observationer
    riksbanken_hamta_korsvalutakurs
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

from data_och_analys.infra import paginering
from data_och_analys.infra.paginering import Paket
from data_och_analys.infra.mcp_transport import starta
from data_och_analys.klienter import (
    bra,
    migrationsverket,
    forsakringskassan,
    kb_bibstat,
    riksbanken,
    skolverket,
    socialstyrelsen,
    trafa,
)

from data_och_analys.infra.mcp_annotationer import (  # noqa: E402
    CACHE_HINTAR,
    LASNING_DB,
    LASNING_EXTERN,
    SYNK,
)

logger = logging.getLogger(__name__)


mcp = MCPServer(
    "special",
    instructions=(
        "MCP-server för myndigheter utan gemensamt API-mönster. Sju "
        "prefix, sju olika källor: skolverket_ (Swagger/REST), trafa_ "
        "(Trafikanalys odokumenterade REST), socialstyrelsen_ (SDB), bra_ "
        "(Brottsförebyggande rådet), fk_ (Försäkringskassans DCAT + CSV), "
        "bibstat_ (KB:s biblioteksstatistik), riksbanken_ (räntor och "
        "valutakurser), samt migrationsverket_ (asyl, tillstånd och "
        "mottagande). "
        "MIGRATIONSVERKET HAR INGET API: statistiken är Excelfiler. "
        "Arbetsordningen är migrationsverket_lista_dataset -> "
        "_lista_blad -> _hamta_tabell. Hoppa inte över bladlistan; bladen "
        "heter olika i varje fil och det första är oftast beskrivande text. "
        "Två punkter i källan betyder maskerat litet tal, inte noll — de "
        "returneras som null och räknas i `maskerade`. Summera dem aldrig "
        "som nollor. För kommunnedbrytning, använd migrationsverket_per_kommun "
        "som sätter på SCB:s kommunkod och filtrerar bort länstotaler. "
        "VÄLJ PREFIX FÖRST: verktygen delar ingen gemensam arbetsordning. "
        "Varje myndighetsgrupp har sin egen kedja, oftast lista → "
        "hämta-struktur → hämta-data. "
        "SVARSSTORLEK: sökverktygen returnerar "
        "{total, sida, per_sida, antal_sidor, datapunkter}. Läs "
        "`antal_sidor` och hämta vidare med `sida=2` — svaret säger inte "
        "av sig självt att det finns mer. "
        "TIDSBEGRÄNSNING: Trafikanalys upphör 2026-12-31 och uppgår i "
        "Tillväxtanalys 2027-01-01 — bas-URL kan ändras då."
    ),
    cache_hints=CACHE_HINTAR,
)


# ============================================================================
# Skolverket
# ============================================================================


@mcp.tool(title="Lista skolenheter", annotations=LASNING_EXTERN)
async def skolverket_lista_skolenheter(
    fritext: str | None = None,
    status: str | None = None,
    kommunkod: Annotated[
        str | None,
        Field(
            description="Fyrsiffrig kommunkod med inledande nolla",
            pattern=r"^\d{4}$",
        ),
    ] = None,
    sida: int = 1,
    per_sida: int = 200,
) -> dict[str, Any]:
    """Listar skolenheter ur Skolverkets skolenhetsregister.

    `fritext` matchar mot skolenhetsnamnet, `status` filtrerar på
    `"Aktiv"`, `"Vilande"` eller `"Planerad"`, och `kommunkod` är SCB:s
    4-siffriga kommunkod (`"0180"` Stockholm) — samma koder som geo-stacken,
    så resultatet kan grupperas per kommun, län eller FA-region.

    Svaret är ett pagineringspaket `{total, sida, per_sida, antal_sidor,
    datapunkter}` med `{Skolenhetskod, Kommunkod, PeOrgNr,
    Skolenhetsnamn, Status}` per enhet.
    """
    return await skolverket.lista_skolenheter(
        fritext=fritext, status=status, kommunkod=kommunkod,
        sida=sida, per_sida=per_sida,
    )


@mcp.tool(title="Hämta en skolenhet", annotations=LASNING_EXTERN)
async def skolverket_hamta_skolenhet(skolenhetskod: str) -> dict[str, Any] | None:
    """Hämtar fullständig information om en skolenhet.

    Inkluderar rektorsnamn, kontaktuppgifter, adress och geokoordinater
    (SWEREF 99 och WGS84). `skolenhetskod` kommer från
    `skolverket_lista_skolenheter`.
    """
    return await skolverket.hamta_skolenhet(skolenhetskod)


@mcp.tool(title="Sök planerade utbildningar", annotations=LASNING_EXTERN)
async def skolverket_sok_planerade_utbildningar(
    kommunkod: Annotated[
        str | None,
        Field(
            description="Fyrsiffrig kommunkod med inledande nolla",
            pattern=r"^\d{4}$",
        ),
    ] = None,
    sida: int = 1,
    per_sida: int = 20,
) -> dict[str, Any]:
    """Söker skolenheter i Utbildningsinfo (planned educations).

    Rikare beskrivning än registret: skolformer, årskurser, huvudmannatyp
    och länkar till statistik. `kommunkod` filtrerar på
    `geographicalAreaCode`. Paginerar på serversidan — `server_totalt`
    och `server_antal_sidor` i svaret speglar hela träffmängden.

    Varje datapunkt har bl.a. `code`, `name`, `geographicalAreaCode`,
    `typeOfSchooling` och `principalOrganizerType`. Använd `code` med
    `skolverket_hamta_skolenhet_statistik` för betygs- och behörighetsdata.
    """
    return await skolverket.sok_planerade_utbildningar(
        kommunkod=kommunkod, sida=sida, per_sida=per_sida
    )


@mcp.tool(title="Hämta statistik för en skolenhet", annotations=LASNING_EXTERN)
async def skolverket_hamta_skolenhet_statistik(
    skolenhetskod: str, lasaar: str | None = None
) -> dict[str, Any]:
    """Hämtar konsoliderad statistik per skolform för en skolenhet.

    Följer Utbildningsinfo:s `statistics`-länk och sedan vidare till
    `gr-statistics`/`fsk-statistics`/`gy-statistics`. Varje mätvärde
    levereras som en lista över de senaste 5 läsåren med
    `{value, valueType, timePeriod}` per år.

    `lasaar` filtrerar till ett specifikt läsår (t.ex. `"2023/24"`).
    Saknas filtret returneras alla år.

    **Begränsning i källan:** API:et håller ett rullande 5-årsfönster
    (typiskt 2020/21–2024/25 för betyg, 2021/22–2025/26 för
    personal/elever). Äldre läsår (t.ex. 2013/14) finns INTE här —
    de kräver Skolverkets arkivfiler (Excel/CSV) och separat ingest.

    Returnerar `{skolenhetskod, skolformer, tillgangliga_lasaar,
    lasaarfilter, statistik: {skolform: {<matvarden>}}}`.
    """
    return await skolverket.hamta_skolenhet_statistik(skolenhetskod, lasaar=lasaar)


# ============================================================================
# Trafikanalys (Trafa)
# ============================================================================


@mcp.tool(title="Lista Trafikanalys produkter", annotations=LASNING_EXTERN)
async def trafa_lista_produkter() -> list[dict[str, Any]]:
    """Listar Trafikanalys statistikprodukter.

    Returnerar `{namn, label, beskrivning, aktiv_fran}` per produkt.
    `namn` (t.ex. `"t10016"` Personbilar) är produktdelen i `query` till
    `trafa_hamta_struktur` och `trafa_hamta_data`.
    """
    return await trafa.lista_produkter()


@mcp.tool(title="Hämta produktens variabelstruktur", annotations=LASNING_EXTERN)
async def trafa_hamta_struktur(query: str | None = None) -> dict[str, Any]:
    """Returnerar Trafas strukturkatalog — utforska variabler och mått.

    Utan `query`: alla produkter. Med en produkt i `query` (t.ex.
    `"t10016"`): produktens variabler och mått. Trafas query-DSL är
    odokumenterad och varierar per produkt, så använd det här verktyget
    för att se giltiga variabel- och måttnamn innan du bygger en
    dataförfrågan — gissa inte namnen.
    """
    return await trafa.hamta_struktur(query)


@mcp.tool(title="Hämta transportstatistik", annotations=LASNING_EXTERN)
async def trafa_hamta_data(query: str) -> dict[str, Any]:
    """Hämtar en datatabell för en Trafa-query.

    `query` är Trafas pipe-separerade DSL:
    `<produkt>|<variabel>:<värde>|<mått>`, t.ex. `"t10016|ar:2022"`
    (Personbilar år 2022). Bygg den utifrån `trafa_hamta_struktur`.

    Returnerar `{namn, kolumner, datapunkter, fel}` där `kolumner` är
    kolumndefinitionerna (med enhet och datatyp) och `datapunkter` är de
    plattade raderna. `fel` speglar API:ets felfält om query:n tolkas fel.
    """
    return await trafa.hamta_data(query)


# ============================================================================
# Socialstyrelsen (SDB)
# ============================================================================


@mcp.tool(title="Lista statistikdatabaser", annotations=LASNING_EXTERN)
async def socialstyrelsen_lista_databaser() -> list[dict[str, Any]]:
    """Listar Socialstyrelsens statistikdatabaser.

    Returnerar `{namn, text}` per databas (t.ex.
    `"diagnoserislutenvard"`, `"dodsorsaker_manad"`, `"amning"`). `namn`
    matas till övriga socialstyrelsen-verktyg.
    """
    return await socialstyrelsen.lista_databaser()


@mcp.tool(title="Hämta databasens dimensioner", annotations=LASNING_EXTERN)
async def socialstyrelsen_hamta_dimensioner(databas: str) -> list[dict[str, Any]]:
    """Listar en databas dimensioner (region, ålder, kön, mått, …).

    Dimensionerna varierar per databas. Returnerar `{namn, text, info}`
    där `info` är en HTML-beskrivning. Använd `namn` som nyckel i `urval`
    till `socialstyrelsen_hamta_resultat`.
    """
    return await socialstyrelsen.hamta_dimensioner(databas)


@mcp.tool(title="Hämta en dimensions värden", annotations=LASNING_EXTERN)
async def socialstyrelsen_hamta_dimensionsvarden(
    databas: Annotated[
        str, Field(description="Databas-ID ur socialstyrelsen_lista_databaser")
    ],
    dimension: Annotated[
        str, Field(description="Dimensions-ID ur socialstyrelsen_hamta_dimensioner")
    ],
    sida: Annotated[int, Field(description="1-baserat sidnummer", ge=1)] = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> Paket[dict[str, Any]]:
    """Listar giltiga värden för en dimension med `{id, kod, text}`.

    `id` är det numeriska id:t som används i resultat-urvalet, `kod` är
    källans kod (t.ex. länskod `"01"`) och `text` är benämningen. Region
    `id=0` är Riket.
    """
    rader = await socialstyrelsen.hamta_dimensionsvarden(databas, dimension)
    return Paket[dict[str, Any]](
        **paginering.paginera(rader, sida=sida, per_sida=per_sida)
    )


@mcp.tool(title="Hämta statistikresultat", annotations=LASNING_EXTERN)
async def socialstyrelsen_hamta_resultat(
    databas: str,
    urval: dict[str, Any],
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> dict[str, Any]:
    """Hämtar dataresultat för ett urval.

    `urval` mappar dimensionsnamn till valt id eller en lista av id:n,
    t.ex. `{"region": 0, "ar": [2022, 2023]}`. Dimensioner som utelämnas
    returneras i sin helhet. Slå upp giltiga id:n med
    `socialstyrelsen_hamta_dimensionsvarden`.

    Svaret är paginerat `{total, sida, per_sida, antal_sidor,
    datapunkter}` med en rad per kombination — typiskt `{regionId,
    alderId, konId, mattId, ar, varde}` plus databasspecifika id-fält.
    Stora urval (alla diagnoser × åldrar × kön) sträcker sig över flera
    sidor; hämta nästa med `sida=2`.
    """
    return await socialstyrelsen.hamta_resultat(
        databas, urval, sida=sida, per_sida=per_sida
    )


# ============================================================================
# Brottsförebyggande rådet (Brå)
# ============================================================================


@mcp.tool(title="Lista brottstyper", annotations=LASNING_EXTERN)
async def bra_lista_brottstyper(menyid: int = bra.MENYID_KOMMUN_AR) -> dict[str, Any]:
    """Listar valbara brottstyper i Brå:s statistik över anmälda brott.

    Brottstyperna är hierarkiska — `niva` anger indraget (0 = toppnivå som
    "Totalt antal brott", högre = mer specifik som "Misshandel inkl. grov").
    Använd `kod` i `bra_hamta_statistik`. Koderna parsas live ur Brå:s
    urvalssida, så de speglar alltid aktuell indelning.
    """
    return await bra.lista_brottstyper(menyid=menyid)


@mcp.tool(title="Lista regioner i brottsstatistiken", annotations=LASNING_EXTERN)
async def bra_lista_regioner(menyid: int = bra.MENYID_KOMMUN_AR) -> dict[str, Any]:
    """Listar valbara regioner (kommuner) i Brå:s anmälda-brott-statistik.

    `kod` är Brå:s interna regionkod — inte SCB:s kommunkod. Matcha på
    `namn` för att koppla mot geo-stackens kommuner. Använd `kod` i
    `bra_hamta_statistik`.
    """
    return await bra.lista_regioner(menyid=menyid)


@mcp.tool(title="Lista tillgängliga perioder", annotations=LASNING_EXTERN)
async def bra_lista_perioder(menyid: int = bra.MENYID_KOMMUN_AR) -> list[dict[str, Any]]:
    """Listar valbara perioder (år) i Brå:s anmälda-brott-statistik.

    Returnerar `{kod, ar, etikett}` med senaste året först. Använd `kod` i
    `bra_hamta_statistik`.
    """
    return await bra.lista_perioder(menyid=menyid)


@mcp.tool(title="Hämta brottsstatistik", annotations=LASNING_EXTERN)
async def bra_hamta_statistik(
    brottstyp_koder: str | list[str],
    region_koder: str | list[str],
    period_koder: str | list[str],
    per_100k: bool = False,
    menyid: int = bra.MENYID_KOMMUN_AR,
    sida: int = 1,
    per_sida: int = 500,
) -> dict[str, Any]:
    """Hämtar antal anmälda brott för brottstyp × region × period.

    Varje argument är en kod eller lista av koder från `bra_lista_brottstyper`,
    `bra_lista_regioner` och `bra_lista_perioder`. Resultatet är en rad per
    kombination med `{brottstyp_kod, region_kod, period_kod, enhet,
    varde_text, varde}`. `per_100k=True` ger antal per 100 000 invånare.

    Datan skrapas ur Brå:s sessionsbaserade SolWebb-tjänst med en
    HTTP-runda per kombination, så håll uttagen rimliga (en handfull
    brottstyper × kommuner × år åt gången). Mycket stora uttag avvisas med
    ett fel som ber dig dela upp dem — kör då flera mindre anrop.

    Saknat värde returneras som `varde_text="..."` med `varde=null`.
    """
    return await bra.hamta_statistik(
        brottstyp_koder=brottstyp_koder,
        region_koder=region_koder,
        period_koder=period_koder,
        per_100k=per_100k,
        menyid=menyid,
        sida=sida,
        per_sida=per_sida,
    )


# ============================================================================
# Försäkringskassan
# ============================================================================


@mcp.tool(title="Lista Försäkringskassans dataset", annotations=LASNING_EXTERN)
async def fk_lista_dataset(
    fritext: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Listar Försäkringskassans öppna JSON-dataset (via dataportal.se).

    `fritext` filtrerar på titel (t.ex. "sjukpenning", "föräldra",
    "bostadsbidrag"). Returnerar `[{titel, url}]` där `url` matas till
    `fk_hamta_dataset`. FK:s API-rot har ingen katalog, så listan kommer
    från dataportal.se.
    """
    return await forsakringskassan.lista_dataset(fritext=fritext, limit=limit)


@mcp.tool(title="Hämta ett dataset", annotations=LASNING_EXTERN)
async def fk_hamta_dataset(
    sokvag_eller_url: str,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> dict[str, Any]:
    """Hämtar ett Försäkringskassan-dataset som platta rader.

    `sokvag_eller_url` är en URL från `fk_lista_dataset` eller en relativ
    sökväg (t.ex. `"sjp-avslag-efter180/SJPAVSLAGEfter180LAN"`). Varje rad
    har dimensionsfälten (år, kön, län, intervall …) plus ett fält per mått.
    Sekretessröjda mått blir `null` med en `<matt>_rojd`-flagga.

    Paginerat — stora dataset (alla län × år × intervall) sträcker sig över
    flera sidor.
    """
    return await forsakringskassan.hamta_dataset(
        sokvag_eller_url, sida=sida, per_sida=per_sida
    )


# ============================================================================
# KB Bibstat — biblioteksstatistik
# ============================================================================


@mcp.tool(title="Hämta biblioteksstatistik", annotations=LASNING_EXTERN)
async def bibstat_hamta_observationer(
    from_date: str | None = None,
    max_poster: int = 2000,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> dict[str, Any]:
    """Hämtar KB:s biblioteksstatistik (Bibstat) som platta observationer.

    Sveriges officiella biblioteksstatistik per bibliotek och undersökningsår.
    `from_date` (YYYY-MM-DD) begränsar till poster ändrade efter datumet.
    `max_poster` tak mot enorma uttag — datat är stort (varje bibliotek × år
    × variabel).

    Varje rad har `bibliotek`, `bibliotek_id`, `sampleYear`, `targetGroup`
    (Folkbibliotek, Forskningsbibliotek …) plus enkätens variabelfält.
    `_avbrott` sätts om taket slog innan datat var slut.
    """
    return await kb_bibstat.hamta_observationer(
        from_date=from_date, max_poster=max_poster, sida=sida, per_sida=per_sida
    )


# ============================================================================
# Riksbanken — räntor och valutakurser (SWEA)
# ============================================================================


@mcp.tool(title="Lista Riksbankens serier", annotations=LASNING_EXTERN)
async def riksbanken_lista_serier(
    grupp_id: int | None = None,
    inkludera_stangda: bool = True,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> dict[str, Any]:
    """Listar Riksbankens SWEA-serier (räntor, valutakurser m.m.).

    `grupp_id` filtrerar på serietyp; `inkludera_stangda=False` döljer
    nedlagda serier. Varje serie har `seriesId` (t.ex. `SECBREPOEFF`
    styrränta, `SEKEURPMI` EUR-kurs), `shortDescription`,
    `observationMinDate`/`observationMaxDate`. Använd `seriesId` i
    `riksbanken_hamta_observationer`.
    """
    return await riksbanken.lista_serier(
        grupp_id=grupp_id, inkludera_stangda=inkludera_stangda,
        sida=sida, per_sida=per_sida,
    )


@mcp.tool(title="Hämta seriemetadata", annotations=LASNING_EXTERN)
async def riksbanken_hamta_serie(serie_id: str) -> dict[str, Any]:
    """Returnerar metadata för en SWEA-serie (datumintervall, beskrivning)."""
    return await riksbanken.hamta_serie(serie_id)


@mcp.tool(title="Hämta serieobservationer", annotations=LASNING_EXTERN)
async def riksbanken_hamta_observationer(
    serie_id: str,
    fran: str | None = None,
    till: str | None = None,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> dict[str, Any]:
    """Hämtar observationer för en SWEA-serie inom ett datumintervall.

    `serie_id` från `riksbanken_lista_serier`. `fran`/`till` är YYYY-MM-DD;
    utelämnas de används seriens hela historik (kan bli många rader för
    dagliga växelkursserier sedan 1993 — paginerat). Varje datapunkt är
    `{date, value}`.
    """
    return await riksbanken.hamta_observationer(
        serie_id, fran=fran, till=till, sida=sida, per_sida=per_sida
    )


@mcp.tool(title="Hämta korsvalutakurs", annotations=LASNING_EXTERN)
async def riksbanken_hamta_korsvalutakurs(
    serie_id_1: str,
    serie_id_2: str,
    fran: str,
    till: str,
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = paginering.STANDARD_PER_SIDA,
) -> dict[str, Any]:
    """Hämtar korsvalutakurs mellan två valutaserier.

    Räknar fram kursen mellan två valutor ur deras SEK-serier — t.ex.
    `SEKEURPMI` och `SEKUSDPMI` ger EUR/USD. `fran`/`till` (YYYY-MM-DD) är
    obligatoriska. Varje datapunkt är `{date, value}`.
    """
    return await riksbanken.hamta_korsvalutakurs(
        serie_id_1, serie_id_2, fran, till, sida=sida, per_sida=per_sida
    )


# ============================================================================
# Migrationsverket
# ============================================================================

# Bladen är breda snarare än långa: en kolumn per månad ger ~740 tecken per
# rad, så suitens vanliga 500 rader per sida blir 370 000 tecken — långt över
# klientens tak. Antalet rader säger inget om svarets storlek när formen ser
# ut så här.
_MV_PER_SIDA = 100


@mcp.tool(title="Lista Migrationsverkets dataset", annotations=LASNING_EXTERN)
async def migrationsverket_lista_dataset() -> list[dict[str, Any]]:
    """Datasetten på Migrationsverkets öppna data-sida.

    Verket har inget API — statistiken ligger som Excelfiler. Nyckeln i
    svaret är den som övriga verktyg tar. Länkarna bär en tidsstämpel som
    byts vid varje uppdatering, så de läses om varje gång och ska inte
    sparas.

    Sex serier uppdateras löpande (asylansökningar, avgjorda asylärenden,
    beviljade uppehållstillstånd, inskrivna i mottagningssystemet,
    kommunmottagna, arbetstillstånd). Övriga är historiska arkiv, flera
    tillbaka till 1980-talet.
    """
    return await migrationsverket.lista_dataset()


@mcp.tool(title="Lista blad i ett Migrationsverket-dataset",
          annotations=LASNING_EXTERN)
async def migrationsverket_lista_blad(
    dataset: Annotated[
        str, Field(description="Nyckel ur migrationsverket_lista_dataset")
    ],
) -> list[str]:
    """Bladen i en arbetsbok. Ett blad är en tabell.

    Kör alltid det här före `migrationsverket_hamta_tabell` — bladen heter
    olika i varje fil, och det första bladet är oftast "Information" med
    beskrivande text snarare än data.
    """
    return await migrationsverket.lista_blad(dataset)


@mcp.tool(title="Hämta ett blad ur Migrationsverkets statistik",
          annotations=LASNING_EXTERN)
async def migrationsverket_hamta_tabell(
    dataset: Annotated[
        str, Field(description="Nyckel ur migrationsverket_lista_dataset")
    ],
    blad: Annotated[str, Field(description="Bladnamn ur migrationsverket_lista_blad")],
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = _MV_PER_SIDA,
) -> dict[str, Any]:
    """Läser ett blad som rader.

    `maskerade` i svaret räknar celler där Migrationsverket dolt ett litet
    tal av sekretesskäl. De returneras som `null`, inte noll — summeras de
    som nollor blir totalerna för låga och kommuner ser tomma ut.

    Tolkade tabeller mellanlagras enligt DOA_MELLANLAGRING_DAGAR så att
    filen inte behöver tolkas om vid varje fråga. `ur_mellanlager` säger
    vilket det blev.

    Svaret är paginerat. Kommunbladen är breda — en kolumn per månad — och
    ett helt blad överskrider klientens svarsgräns.
    """
    tabell = await migrationsverket.hamta_tabell(dataset, blad)
    rader = tabell.pop("datapunkter")
    return {**tabell, **paginering.paginera(rader, sida=sida, per_sida=per_sida)}


@mcp.tool(title="Hämta Migrationsverkets statistik per kommun",
          annotations=LASNING_EXTERN)
async def migrationsverket_per_kommun(
    dataset: Annotated[
        str, Field(description="Nyckel ur migrationsverket_lista_dataset")
    ],
    blad: Annotated[
        str, Field(description="Ett blad med kommunkolumn, t.ex. 'Län, kommun och månad'")
    ],
    sida: int = 1,
    per_sida: Annotated[
        int, Field(description="Rader per sida", ge=1, le=500)
    ] = _MV_PER_SIDA,
) -> dict[str, Any]:
    """Samma som hamta_tabell, men med SCB:s kommunkod påsatt.

    Bladen bär kommunnamn, inte koder. Översättningen jämnar ut skrivsätt —
    Migrationsverket skriver "Upplands-Väsby" där SCB skriver "Upplands
    Väsby" — och länsvisa totalrader filtreras bort så att de inte
    dubbelräknas.

    Med `kommun_kod` går datat att joina mot doa-geo: bryta ned på län,
    FA-region eller kommungrupp, och lägga på karta med geo_karta_omraden.

    Namn som inte kunde matchas står i `omatchade`. En kommun som saknas i
    källans fil syns inte där — den är utelämnad av Migrationsverket, inte
    tappad av oss.

    Svaret är paginerat; 289 kommuner med en kolumn per månad ryms inte i
    ett svar.
    """
    tabell = await migrationsverket.hamta_per_kommun(dataset, blad)
    rader = tabell.pop("datapunkter")
    return {**tabell, **paginering.paginera(rader, sida=sida, per_sida=per_sida)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    starta(mcp)
