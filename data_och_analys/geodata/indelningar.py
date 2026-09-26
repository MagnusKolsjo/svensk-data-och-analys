# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Funktionella indelningar — SKR, Tillväxtverket, FA-regioner.

Modulen är delad i två lager:

1. Access-lager — frågor mot lokal DB. Två perspektiv stöds:
   - "redovisa fördelat på X" (lista_grupper, oversatt, lista_kommuner_i_grupp)
   - "vilka enheter uppfyller kriterier" (filtrera_kommuner)

2. Parsare — hämtar källans senaste publikation och skriver till tabellen
   `kommun_klassificering`. Som referens ingår en SKR-parser; varje nytt
   system (Tillväxtverket, FA-regioner, ...) får en analog parser.

Klassificeringar versioneras med `ar` i tabellen. Saknat årtal i en
access-fråga tolkas som "senaste tillgängliga år för det systemet" —
historiska analyser anger explicit år.

Ingångspunkter:

    Access:
        lista_system()
        lista_grupper(system, ar=None)
        lista_kommuner_i_grupp(system, grupp_kod, ar=None)
        filtrera_kommuner(kriterier, ar=None)
        oversatt(kommun_koder, system, ar=None)

    Parsare:
        synka_skr_kommungrupp(rader=None, ar=..., url=None)
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

import httpx

from data_och_analys.infra import db, framsteg
from data_och_analys.katalog_klienter import huwise

logger = logging.getLogger(__name__)


# ============================================================================
# Access-lager
# ============================================================================


async def _senaste_ar(system: str) -> int | None:
    """Returnerar det senaste år för vilket systemet har data, eller None."""
    sql = (
        f"SELECT MAX(ar) FROM {db.prefix()}kommun_klassificering "
        f"WHERE system = {db.ph(1)}"
    )
    async with db.hamta_db() as anslutning:
        return await anslutning.fetchval(sql, system)


async def lista_system() -> list[dict]:
    """Listar alla registrerade klassificeringssystem.

    Returnerar metadata-rader från `klassificeringssystem`, kompletterat
    med senaste år som finns i `kommun_klassificering` för respektive
    system.
    """
    sql = f"SELECT system, namn, kalla, beskrivning, senast_synkad FROM {db.prefix()}klassificeringssystem ORDER BY system"
    async with db.hamta_db() as anslutning:
        system_rader = await anslutning.fetch(sql)

    resultat = []
    for rad in system_rader:
        rad["senaste_ar"] = await _senaste_ar(rad["system"])
        resultat.append(rad)
    return resultat


async def lista_grupper(system: str, ar: int | None = None) -> list[dict]:
    """Returnerar alla unika grupper i ett system för ett givet år.

    Saknat `ar` tolkas som senaste tillgängliga år. Returnerar tom lista
    om systemet inte finns eller saknar data.
    """
    ar = ar if ar is not None else await _senaste_ar(system)
    if ar is None:
        return []
    sql = (
        f"SELECT DISTINCT grupp_kod, grupp_namn "
        f"FROM {db.prefix()}kommun_klassificering "
        f"WHERE system = {db.ph(1)} AND ar = {db.ph(2)} "
        f"ORDER BY grupp_kod"
    )
    async with db.hamta_db() as anslutning:
        return await anslutning.fetch(sql, system, ar)


async def lista_kommuner_i_grupp(
    system: str, grupp_kod: str, ar: int | None = None
) -> list[dict]:
    """Returnerar kommuner som hör till en specifik grupp i ett system.

    LEFT JOIN mot `kommun` så att klassificeringarna kan visas även när
    `kommun`-tabellen ännu inte är seedad — namnet är då `None`. Det
    håller verktyget användbart från första klassificeringssynken,
    innan SCB-kommunlistan är inläst.
    """
    ar = ar if ar is not None else await _senaste_ar(system)
    if ar is None:
        return []
    sql = (
        f"SELECT kk.kommun_kod AS kod, k.namn "
        f"FROM {db.prefix()}kommun_klassificering kk "
        f"LEFT JOIN {db.prefix()}kommun k ON k.kod = kk.kommun_kod "
        f"WHERE kk.system = {db.ph(1)} "
        f"  AND kk.grupp_kod = {db.ph(2)} "
        f"  AND kk.ar = {db.ph(3)} "
        f"ORDER BY kk.kommun_kod"
    )
    async with db.hamta_db() as anslutning:
        return await anslutning.fetch(sql, system, grupp_kod, ar)


async def lista_lan() -> list[dict]:
    """Returnerar alla län."""
    sql = f"SELECT kod, namn, bokstav FROM {db.prefix()}lan ORDER BY kod"
    async with db.hamta_db() as anslutning:
        return await anslutning.fetch(sql)


async def lista_regioner() -> list[dict]:
    """Returnerar alla regioner."""
    sql = f"SELECT kod, namn FROM {db.prefix()}region ORDER BY kod"
    async with db.hamta_db() as anslutning:
        return await anslutning.fetch(sql)


async def lista_kommuner(
    lan_kod: str | None = None, region_kod: str | None = None
) -> list[dict]:
    """Listar kommuner, optionellt filtrerat på län eller region."""
    villkor: list[str] = []
    parametrar: list[object] = []
    if lan_kod is not None:
        villkor.append(f"lan_kod = {db.ph(len(parametrar) + 1)}")
        parametrar.append(lan_kod)
    if region_kod is not None:
        villkor.append(f"region_kod = {db.ph(len(parametrar) + 1)}")
        parametrar.append(region_kod)
    where = (" WHERE " + " AND ".join(villkor)) if villkor else ""
    sql = (
        f"SELECT kod, namn, lan_kod, region_kod "
        f"FROM {db.prefix()}kommun{where} ORDER BY kod"
    )
    async with db.hamta_db() as anslutning:
        return await anslutning.fetch(sql, *parametrar)


async def kommun_info(kommun_kod: str) -> dict | None:
    """Returnerar basinfo för en kommun + alla aktuella klassificeringar.

    "Aktuell" betyder här senaste år som finns per system — inte
    nödvändigtvis samma år över systemen.
    """
    grund_sql = (
        f"SELECT kod, namn, lan_kod, region_kod "
        f"FROM {db.prefix()}kommun WHERE kod = {db.ph(1)}"
    )
    klass_sql = (
        f"SELECT kk.system, kk.grupp_kod, kk.grupp_namn, kk.ar "
        f"FROM {db.prefix()}kommun_klassificering kk "
        f"WHERE kk.kommun_kod = {db.ph(1)} "
        f"  AND kk.ar = ("
        f"    SELECT MAX(ar) FROM {db.prefix()}kommun_klassificering "
        f"    WHERE kommun_kod = kk.kommun_kod AND system = kk.system"
        f"  )"
        f"ORDER BY kk.system"
    )
    async with db.hamta_db() as anslutning:
        grund = await anslutning.fetchrow(grund_sql, kommun_kod)
        klass = await anslutning.fetch(klass_sql, kommun_kod)
    if grund is None and not klass:
        return None
    if grund is None:
        # Klassificerad men inte ännu seedad i kommun-tabellen
        grund = {"kod": kommun_kod, "namn": None, "lan_kod": None, "region_kod": None}
    grund["klassificeringar"] = klass
    return grund


async def lagg_till_kommuner(
    rader: Iterable[tuple[str, str, str | None]]
) -> int:
    """Bootstrap-helper för `kommun`-tabellen.

    `rader` är (kod, namn, lan_kod). Idempotent — upsertas via
    ON CONFLICT (kod). Tänkt för att kunna seeda enstaka kommuner
    inför demos eller tester innan en full SCB-synk är på plats.
    """
    rader = list(rader)
    if not rader:
        return 0
    sql = (
        f"INSERT INTO {db.prefix()}kommun (kod, namn, lan_kod) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}) "
        f"ON CONFLICT (kod) DO UPDATE SET "
        f"namn = EXCLUDED.namn, lan_kod = EXCLUDED.lan_kod"
    )
    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, [tuple(r) for r in rader])
    return len(rader)


async def synka_klassificering_rader(
    system: str,
    system_namn: str,
    kalla: str,
    beskrivning: str,
    rader: Iterable[tuple[str, str, str]],
    ar: int,
) -> int:
    """Generisk synk av en klassificering från redan inlästa rader.

    `rader` är (kommun_kod, grupp_kod, grupp_namn). Funktionen registrerar
    `system` i klassificeringssystem-tabellen, upsertar raderna och
    uppdaterar `senast_synkad`. Idempotent.

    Avsedd för system som inte har en specifik parser (t.ex. Tillväxtverket,
    FA-region) — anroparen läser källfilen och normaliserar utanför. För
    SKR finns en specialiserad `synka_skr_kommungrupp` med inbyggd
    grupp-koddatabas och kolumn-autodetektion.
    """
    rader = [
        (kk.zfill(4), system, gk, gn, ar)
        for kk, gk, gn in rader
    ]
    if not rader:
        return 0

    async with db.hamta_db() as anslutning:
        meta_sql = (
            f"INSERT INTO {db.prefix()}klassificeringssystem "
            f"(system, namn, kalla, beskrivning, senast_synkad) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}, "
            f"CURRENT_TIMESTAMP) "
        )
        if db.ar_postgres():
            meta_sql += (
                "ON CONFLICT (system) DO UPDATE SET "
                "namn = EXCLUDED.namn, kalla = EXCLUDED.kalla, "
                "beskrivning = EXCLUDED.beskrivning, "
                "senast_synkad = CURRENT_TIMESTAMP"
            )
        else:
            meta_sql = meta_sql.replace("INSERT INTO", "INSERT OR REPLACE INTO")
        await anslutning.execute(
            meta_sql, system, system_namn, kalla, beskrivning
        )

        upsert_sql = (
            f"INSERT INTO {db.prefix()}kommun_klassificering "
            f"(kommun_kod, system, grupp_kod, grupp_namn, ar) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}, {db.ph(5)}) "
            f"ON CONFLICT (kommun_kod, system, ar) DO UPDATE SET "
            f"grupp_kod = EXCLUDED.grupp_kod, "
            f"grupp_namn = EXCLUDED.grupp_namn"
        )
        await anslutning.executemany(upsert_sql, rader)

    logger.info(
        "Klassificering %s synkad: %d rader för år %d", system, len(rader), ar
    )
    return len(rader)


async def filtrera_kommuner(
    kriterier: dict[str, str | list[str]], ar: int | None = None
) -> list[str]:
    """Returnerar kommunkoder som uppfyller alla kriterier samtidigt.

    `kriterier` mappar `system` till `grupp_kod` (sträng) eller en lista
    av grupp_kod:er (kommun som matchar någon av dem). Tomt `kriterier`-
    objekt returnerar tom lista — fråga utan kriterier ska vara explicit
    om man vill ha alla kommuner.

    Implementationen joinas via en CTE per kriterium så att SQL:en är
    läsbar oavsett antal system.
    """
    if not kriterier:
        return []

    bitar: list[str] = []
    parametrar: list[object] = []
    nasta_ph = 1

    for i, (system, varde) in enumerate(kriterier.items()):
        ar_for_system = ar if ar is not None else await _senaste_ar(system)
        if ar_for_system is None:
            # Ett system utan data → ingen kommun kan uppfylla kombinationen.
            return []

        varden = [varde] if isinstance(varde, str) else list(varde)

        # Platshållarordningen följer parameter-listans ordning — viktigt
        # för SQLite där `?` är positionellt. (Postgres tolkar $N explicit
        # men vi håller dem synkroniserade ändå.)
        ph_system = db.ph(nasta_ph)
        ph_ar = db.ph(nasta_ph + 1)
        platshallare = ", ".join(
            db.ph(nasta_ph + 2 + j) for j in range(len(varden))
        )

        bitar.append(
            f"k{i} AS ("
            f"  SELECT kommun_kod FROM {db.prefix()}kommun_klassificering"
            f"  WHERE system = {ph_system}"
            f"    AND ar = {ph_ar}"
            f"    AND grupp_kod IN ({platshallare})"
            f")"
        )
        parametrar.append(system)
        parametrar.append(ar_for_system)
        parametrar.extend(varden)
        nasta_ph += 2 + len(varden)

    cte_block = "WITH " + ", ".join(bitar)
    join_block = " ".join(
        f"INNER JOIN k{i} ON k{i}.kommun_kod = k0.kommun_kod"
        for i in range(1, len(kriterier))
    )
    sql = (
        f"{cte_block} "
        f"SELECT DISTINCT k0.kommun_kod FROM k0 "
        f"{join_block} "
        f"ORDER BY k0.kommun_kod"
    )

    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, *parametrar)
    return [r["kommun_kod"] for r in rader]


async def oversatt(
    kommun_koder: list[str], system: str, ar: int | None = None
) -> dict[str, dict]:
    """Mappar kommunkoder till deras grupp i ett givet system.

    Returnerar `{kommun_kod: {grupp_kod, grupp_namn}}`. Kommuner som
    saknar klassificering i systemet utelämnas ur svaret.
    """
    if not kommun_koder:
        return {}
    ar = ar if ar is not None else await _senaste_ar(system)
    if ar is None:
        return {}

    platshallare = ", ".join(db.ph(i + 3) for i in range(len(kommun_koder)))
    sql = (
        f"SELECT kommun_kod, grupp_kod, grupp_namn "
        f"FROM {db.prefix()}kommun_klassificering "
        f"WHERE system = {db.ph(1)} AND ar = {db.ph(2)} "
        f"  AND kommun_kod IN ({platshallare})"
    )
    parametrar = [system, ar, *kommun_koder]
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, *parametrar)
    return {
        r["kommun_kod"]: {"grupp_kod": r["grupp_kod"], "grupp_namn": r["grupp_namn"]}
        for r in rader
    }


# ============================================================================
# Parsarlager — källspecifika Excel-parsare
# ============================================================================
#
# Varje parsare följer samma kontrakt:
#   1. Antingen tar den `rader` direkt (för test och offline-import) eller
#      hämtar en publikation från källan via httpx.
#   2. Normaliserar varje rad till (kommun_kod, grupp_kod, grupp_namn).
#   3. Anropar `synka_klassificering_rader` för upsert mot DB.
#
# Källspecifika parsare slipper kolumnnamn-heuristik — de vet exakt vilket
# blad i exakt vilket filformat de hanterar. Generiska klassificeringar
# utan dedicerad parser tas in via `synka_klassificering_rader` direkt.


SKR_SYSTEM = "skr_kommungrupp"
TV_FA_SYSTEM = "tillvaxtverket_fa_region"
TV_SL3_SYSTEM = "tillvaxtverket_sl3"
TV_SL6_SYSTEM = "tillvaxtverket_sl6"


async def _hamta_xlsx(url: str) -> bytes:
    """GET av en xlsx-fil med browser-User-Agent."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 svensk-data-och-analys"
        )
    }
    async with httpx.AsyncClient(timeout=60.0, headers=headers) as klient:
        svar = await klient.get(url)
    svar.raise_for_status()
    return svar.content


async def synka_skr_kommungrupp(
    rader: Iterable[tuple[str, str, str]] | None = None,
    ar: int = 2023,
    url: str | None = None,
) -> int:
    """Synkar SKR:s kommungruppsindelning till `kommun_klassificering`.

    Tre källalternativ, i fallande prioritet:
        1. `rader` direkt (iterable av (kommun_kod, grupp_kod, grupp_namn))
        2. `url` — SKR:s publicerade Excel-fil
        3. Env-variabeln `DOA_SKR_KOMMUNGRUPP_URL`

    Excel-strukturen: blad "Bilaga1 Lista alla kommuner" har sin rubrik på
    rad 2 (rad 1 är ett SKR-meddelande). Kolumnerna `Gruppkod`,
    `Kommunkod` och `Kommungrupp 2023` används.
    """
    if rader is None:
        kalla = url or os.getenv("DOA_SKR_KOMMUNGRUPP_URL", "").strip()
        if not kalla:
            raise ValueError(
                "ingen källa angiven — skicka `rader`, `url` eller sätt "
                "DOA_SKR_KOMMUNGRUPP_URL i .env"
            )
        logger.info("Hämtar SKR-kommungrupp från %s", kalla)
        import io
        import pandas as pd

        innehall = await _hamta_xlsx(kalla)
        df = pd.read_excel(
            io.BytesIO(innehall),
            sheet_name="Bilaga1 Lista alla kommuner",
            header=1,
            dtype=str,
        )
        rader = [
            (str(r["Kommunkod"]).strip().zfill(4),
             str(r["Gruppkod"]).strip(),
             str(r["Kommungrupp 2023"]).strip())
            for _, r in df.iterrows()
            if pd.notna(r.get("Kommunkod"))
        ]

    return await synka_klassificering_rader(
        system=SKR_SYSTEM,
        system_namn="SKR:s kommungruppsindelning",
        kalla="skr.se",
        beskrivning=(
            "Nio-grupp-indelning av kommuner efter befolkning, "
            "tätortsstruktur och pendlingsmönster."
        ),
        rader=rader,
        ar=ar,
    )


async def synka_tillvaxtverket_fa_region(
    rader: Iterable[tuple[str, str, str]] | None = None,
    ar: int = 2025,
    url: str | None = None,
    seeda_kommuner: bool = True,
) -> int:
    """Synkar Tillväxtverkets FA-regioner till `kommun_klassificering`.

    FA-regioner är funktionella analysregioner — kommuner som hänger
    samman ekonomiskt via pendling, oavsett administrativ gräns. FA25
    är 2025 års indelning, 60 regioner som täcker alla 290 kommuner.

    Excel-strukturen: ett enda blad "FA25" med rena kolumner
    `FA25Nr`, `FA25Namn`, `KommunNamn`, `KommunKod`.

    Med `seeda_kommuner=True` upsertas också kommunnamn i `kommun`-
    tabellen från filens `KommunNamn`-kolumn — en gratis-bieffekt
    eftersom källan ändå listar alla 290.
    """
    kommun_seed: list[tuple[str, str, str | None]] = []
    if rader is None:
        kalla = url or os.getenv("DOA_TV_FA_REGION_URL", "").strip()
        if not kalla:
            raise ValueError(
                "ingen källa angiven — skicka `rader`, `url` eller sätt "
                "DOA_TV_FA_REGION_URL i .env"
            )
        logger.info("Hämtar FA-regioner från %s", kalla)
        import io
        import pandas as pd

        innehall = await _hamta_xlsx(kalla)
        df = pd.read_excel(io.BytesIO(innehall), sheet_name="FA25", dtype=str)
        rader = []
        for _, r in df.iterrows():
            kk = str(r["KommunKod"]).strip().zfill(4)
            # Tillväxtverket bytte i augusti 2026 från tal (1) till text med
            # inledande nolla (01) i samma årgång. Utan normalisering byter
            # varje region kod mellan två synkar av samma indelning.
            fa_nr = str(r["FA25Nr"]).strip().lstrip("0") or "0"
            rader.append((kk, fa_nr, str(r["FA25Namn"]).strip()))
            if seeda_kommuner:
                kommun_seed.append((kk, str(r["KommunNamn"]).strip(), None))

    if seeda_kommuner and kommun_seed:
        await lagg_till_kommuner(kommun_seed)

    return await synka_klassificering_rader(
        system=TV_FA_SYSTEM,
        system_namn="Tillväxtverkets FA-regioner",
        kalla="tillvaxtverket.se",
        beskrivning=(
            "Funktionella analysregioner — kommuner som hänger samman "
            "ekonomiskt via pendling. FA25 är 2025 års 60-region-indelning."
        ),
        rader=rader,
        ar=ar,
    )


async def synka_tillvaxtverket_kommuntyper(
    niva: str,
    rader: Iterable[tuple[str, str, str]] | None = None,
    ar: int = 2026,
    url: str | None = None,
    seeda_kommuner: bool = True,
    seeda_lan: bool = True,
) -> int:
    """Synkar Tillväxtverkets kommuntyper (stad/landsbygd).

    Två nivåer publiceras i samma fil:
        SL3 — grov indelning, 3 grupper (Storstad / Blandad / Landsbygd)
        SL6 — fin indelning, 6 grupper

    Varje nivå lagras som ett eget system: `tillvaxtverket_sl3` och
    `tillvaxtverket_sl6`. Anropa funktionen en gång per nivå du vill ha.

    Excel-strukturen: blad `KommuntyperSL2026` med rubrikraden på rad 2.
    Relevanta kolumner per nivå:
        SL3: kod-kolumn "Kommun-typer 2026 kod (SL3KN_2026)",
             namn-kolumn "Kommuntyper 2026  namn, (SL3_2026)"
        SL6: kod-kolumn "6 kommun-typer, kod, (SL6KN_2026_kod)",
             namn-kolumn "6 Kommuntyper, namn,  2026 (SL6KN_2026_namn)"

    Med `seeda_kommuner=True` och `seeda_lan=True` (default) fylls
    `kommun`- och `lan`-tabellerna samtidigt från filens kolumner —
    bekvämt eftersom filen ändå listar alla 290 kommuner med län.
    """
    niva = niva.upper()
    if niva == "SL3":
        system_id = TV_SL3_SYSTEM
        system_namn = "Tillväxtverkets kommuntyper SL3 (3-grupper)"
        kol_kod = "Kommun-typer 2026 kod (SL3KN_2026)"
        kol_namn = "Kommuntyper 2026  namn, (SL3_2026)"
        beskrivning = "Grov stad/land-indelning i 3 grupper."
    elif niva == "SL6":
        system_id = TV_SL6_SYSTEM
        system_namn = "Tillväxtverkets kommuntyper SL6 (6-grupper)"
        kol_kod = "6 kommun-typer, kod, (SL6KN_2026_kod)"
        kol_namn = "6 Kommuntyper, namn,  2026 (SL6KN_2026_namn)"
        beskrivning = "Fin stad/land-indelning i 6 grupper."
    else:
        raise ValueError(f"niva måste vara 'SL3' eller 'SL6', fick {niva!r}")

    kommun_seed: list[tuple[str, str, str | None]] = []
    lan_seed: dict[str, str] = {}
    if rader is None:
        kalla = url or os.getenv("DOA_TV_KOMMUNTYPER_URL", "").strip()
        if not kalla:
            raise ValueError(
                "ingen källa angiven — skicka `rader`, `url` eller sätt "
                "DOA_TV_KOMMUNTYPER_URL i .env"
            )
        logger.info("Hämtar Tillväxtverket-kommuntyper (%s) från %s", niva, kalla)
        import io
        import pandas as pd

        innehall = await _hamta_xlsx(kalla)
        df = pd.read_excel(
            io.BytesIO(innehall),
            sheet_name="KommuntyperSL2026",
            header=1,
            dtype=str,
        )
        rader = []
        for _, r in df.iterrows():
            if pd.isna(r.get("Kommunkod")):
                continue
            kk = str(r["Kommunkod"]).strip().zfill(4)
            grupp_kod = str(r[kol_kod]).strip()
            grupp_namn = str(r[kol_namn]).strip()
            rader.append((kk, grupp_kod, grupp_namn))
            if seeda_kommuner:
                lan_kod = str(r["Län kod"]).strip().zfill(2)
                kommun_seed.append((kk, str(r["Kommunnamn"]).strip(), lan_kod))
            if seeda_lan:
                lan_kod = str(r["Län kod"]).strip().zfill(2)
                lan_seed[lan_kod] = str(r["Län namn"]).strip()

    if seeda_lan and lan_seed:
        await _lagg_till_lan(lan_seed.items())
    if seeda_kommuner and kommun_seed:
        await lagg_till_kommuner(kommun_seed)

    return await synka_klassificering_rader(
        system=system_id,
        system_namn=system_namn,
        kalla="tillvaxtverket.se",
        beskrivning=beskrivning,
        rader=rader,
        ar=ar,
    )


async def _lagg_till_lan(rader: Iterable[tuple[str, str]]) -> int:
    """Internt: upserta län från (kod, namn) och fyller på länsbokstav.

    Bokstaven slås upp i `_LANSBOKSTAVER` — fast i lag sedan
    registreringsbeteckningsreformen 1971 (med justeringen vid Skåne-
    sammanslagningen 1997 där L försvann och M behölls för hela Skåne,
    och vid Västra Götalands-sammanslagningen 1998 där O behölls för
    hela det nya länet).
    """
    rader = list(rader)
    if not rader:
        return 0
    rader_med_bokstav = [
        (kod, namn, _LANSBOKSTAVER.get(kod)) for kod, namn in rader
    ]
    sql = (
        f"INSERT INTO {db.prefix()}lan (kod, namn, bokstav) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}) "
        f"ON CONFLICT (kod) DO UPDATE SET "
        f"  namn = EXCLUDED.namn, "
        f"  bokstav = COALESCE(EXCLUDED.bokstav, {db.prefix()}lan.bokstav)"
    )
    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, rader_med_bokstav)
    return len(rader)


# ============================================================================
# Hardkodade indelningar — små, stabila, offentligt välkända
# ============================================================================
#
# Hardkodning är försvarbar för data som är liten (under 100 rader), ändras
# sällan (vart 3:e–10:e år) och vars officiella benämning är offentlig.
# Alternativet — att skrapa SCB:s sökresultat eller dra in EU:s SDMX-API —
# blir överarbete för 70 rader text. När en uppdatering kommer redigeras
# katalogen och en re-synk körs. DB:n versionerar via `ar` så historiska
# data bevaras.


# Länsbokstaven — fastställd i lag via registreringsbeteckningssystemet
# (Trafikverket). Bokstaven används också som vedertagen muntlig kortform.
# Skåne (M) och Västra Götaland (O) bär enkla bokstäver efter
# sammanslagningarna 1997 respektive 1998; Stockholms (AB), Västerbottens
# (AC) och Norrbottens (BD) län bär dubbla bokstäver av historiska skäl.
_LANSBOKSTAVER: dict[str, str] = {
    "01": "AB",  # Stockholms län
    "03": "C",   # Uppsala län
    "04": "D",   # Södermanlands län
    "05": "E",   # Östergötlands län
    "06": "F",   # Jönköpings län
    "07": "G",   # Kronobergs län
    "08": "H",   # Kalmar län
    "09": "I",   # Gotlands län
    "10": "K",   # Blekinge län
    "12": "M",   # Skåne län
    "13": "N",   # Hallands län
    "14": "O",   # Västra Götalands län
    "17": "S",   # Värmlands län
    "18": "T",   # Örebro län
    "19": "U",   # Västmanlands län
    "20": "W",   # Dalarnas län
    "21": "X",   # Gävleborgs län
    "22": "Y",   # Västernorrlands län
    "23": "Z",   # Jämtlands län
    "24": "AC",  # Västerbottens län
    "25": "BD",  # Norrbottens län
}


# 21 regioner. Stabil sedan regionerna formellt ersatte landstingen 2019.
# Region-kod är samma som länkod — `kommun.region_kod` sätts från `lan_kod`
# i `synka_regioner`. Region Gotland är samtidigt kommun (specialfall).
_REGIONER: list[tuple[str, str]] = [
    ("01", "Region Stockholm"),
    ("03", "Region Uppsala"),
    ("04", "Region Sörmland"),
    ("05", "Region Östergötland"),
    ("06", "Region Jönköpings län"),
    ("07", "Region Kronoberg"),
    ("08", "Region Kalmar län"),
    ("09", "Region Gotland"),
    ("10", "Region Blekinge"),
    ("12", "Region Skåne"),
    ("13", "Region Halland"),
    ("14", "Västra Götalandsregionen"),
    ("17", "Region Värmland"),
    ("18", "Region Örebro län"),
    ("19", "Region Västmanland"),
    ("20", "Region Dalarna"),
    ("21", "Region Gävleborg"),
    ("22", "Region Västernorrland"),
    ("23", "Region Jämtland Härjedalen"),
    ("24", "Region Västerbotten"),
    ("25", "Region Norrbotten"),
]


# 29 valkretsar för riksdagsval. Stabila sedan reformen inför 2018 års val.
# Kommun-mappning sker via `valdistrikt`-tabellen (per valår).
_VALKRETSAR_RIKSDAG: list[tuple[str, str]] = [
    ("01", "Stockholms kommun"),
    ("02", "Stockholms län"),
    ("03", "Uppsala län"),
    ("04", "Södermanlands län"),
    ("05", "Östergötlands län"),
    ("06", "Jönköpings län"),
    ("07", "Kronobergs län"),
    ("08", "Kalmar län"),
    ("09", "Gotlands län"),
    ("10", "Blekinge län"),
    ("11", "Malmö kommun"),
    ("12", "Skåne läns västra"),
    ("13", "Skåne läns södra"),
    ("14", "Skåne läns norra och östra"),
    ("15", "Hallands län"),
    ("16", "Göteborgs kommun"),
    ("17", "Västra Götalands läns norra"),
    ("18", "Västra Götalands läns västra"),
    ("19", "Västra Götalands läns södra"),
    ("20", "Västra Götalands läns östra"),
    ("21", "Värmlands län"),
    ("22", "Örebro län"),
    ("23", "Västmanlands län"),
    ("24", "Dalarnas län"),
    ("25", "Gävleborgs län"),
    ("26", "Västernorrlands län"),
    ("27", "Jämtlands län"),
    ("28", "Västerbottens län"),
    ("29", "Norrbottens län"),
]


# NUTS — Eurostats statistiska indelning. Aktuell version: NUTS 2021.
# SE1 Östra, SE2 Södra, SE3 Norra.
_NUTS_1: list[tuple[str, str]] = [
    ("SE1", "Östra Sverige"),
    ("SE2", "Södra Sverige"),
    ("SE3", "Norra Sverige"),
]

# (kod, namn, NUTS1-parent)
_NUTS_2: list[tuple[str, str, str]] = [
    ("SE11", "Stockholm", "SE1"),
    ("SE12", "Östra Mellansverige", "SE1"),
    ("SE21", "Småland med öarna", "SE2"),
    ("SE22", "Sydsverige", "SE2"),
    ("SE23", "Västsverige", "SE2"),
    ("SE31", "Norra Mellansverige", "SE3"),
    ("SE32", "Mellersta Norrland", "SE3"),
    ("SE33", "Övre Norrland", "SE3"),
]

# länkod -> (NUTS3-kod, NUTS3-namn, NUTS2-parent). NUTS3 motsvarar län 1-till-1.
_NUTS_3_PER_LAN: dict[str, tuple[str, str, str]] = {
    "01": ("SE110", "Stockholms län", "SE11"),
    "03": ("SE121", "Uppsala län", "SE12"),
    "04": ("SE122", "Södermanlands län", "SE12"),
    "05": ("SE123", "Östergötlands län", "SE12"),
    "06": ("SE211", "Jönköpings län", "SE21"),
    "07": ("SE212", "Kronobergs län", "SE21"),
    "08": ("SE213", "Kalmar län", "SE21"),
    "09": ("SE214", "Gotlands län", "SE21"),
    "10": ("SE221", "Blekinge län", "SE22"),
    "12": ("SE224", "Skåne län", "SE22"),
    "13": ("SE231", "Hallands län", "SE23"),
    "14": ("SE232", "Västra Götalands län", "SE23"),
    "17": ("SE311", "Värmlands län", "SE31"),
    "18": ("SE124", "Örebro län", "SE12"),
    "19": ("SE125", "Västmanlands län", "SE12"),
    "20": ("SE312", "Dalarnas län", "SE31"),
    "21": ("SE313", "Gävleborgs län", "SE31"),
    "22": ("SE321", "Västernorrlands län", "SE32"),
    "23": ("SE322", "Jämtlands län", "SE32"),
    "24": ("SE331", "Västerbottens län", "SE33"),
    "25": ("SE332", "Norrbottens län", "SE33"),
}


async def synka_regioner() -> int:
    """Sätter de 21 regionerna i `region`-tabellen och fyller i
    `kommun.region_kod` för alla kommuner som saknar det.

    Hardkodad data — listan är stabil sedan 2019. Idempotent. Returnerar
    antalet regioner.
    """
    sql = (
        f"INSERT INTO {db.prefix()}region (kod, namn) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}) "
        f"ON CONFLICT (kod) DO UPDATE SET namn = EXCLUDED.namn"
    )
    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, _REGIONER)
        # Region-kod = län-kod för standardregionerna; fyll i där det saknas
        await anslutning.execute(
            f"UPDATE {db.prefix()}kommun SET region_kod = lan_kod "
            f"WHERE region_kod IS NULL OR region_kod = ''"
        )
    return len(_REGIONER)


async def synka_valkretsar(ar: int = 2022) -> int:
    """Sätter de 29 riksdagsvalkretsarna för ett givet valår.

    Hardkodad data — valkretsarna har varit oförändrade sedan 2018 års
    valreform. Idempotent på (typ='riksdag', kod, ar).

    Kommun- och regionvalkretsar synkas inte här utan i `synka_valdistrikt`
    som extraherar dem från val.se-filen (där de finns per valår).
    """
    sql = (
        f"INSERT INTO {db.prefix()}valkrets (typ, kod, ar, namn) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}) "
        f"ON CONFLICT (typ, kod, ar) DO UPDATE SET namn = EXCLUDED.namn"
    )
    rader = [("riksdag", kod, ar, namn) for kod, namn in _VALKRETSAR_RIKSDAG]
    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, rader)
    return len(rader)


async def synka_nuts() -> int:
    """Genererar NUTS-tabellen från hardkodad katalog joinad mot kommunens
    `lan_kod`. Tre rader per kommun (nivå 1, 2, 3) = ~870 rader.

    Kräver att `kommun`-tabellen är seedad med `lan_kod` — vilket
    Tillväxtverkets kommuntyp-parser gör automatiskt.
    """
    async with db.hamta_db() as anslutning:
        kommuner = await anslutning.fetch(
            f"SELECT kod, lan_kod FROM {db.prefix()}kommun "
            f"WHERE lan_kod IS NOT NULL"
        )

    nuts2_lookup = {kod: (namn, parent) for kod, namn, parent in _NUTS_2}
    nuts1_lookup = dict(_NUTS_1)

    rader: list[tuple[str, int, str, str]] = []
    for k in kommuner:
        lan = k["lan_kod"]
        if lan not in _NUTS_3_PER_LAN:
            logger.warning("Saknar NUTS3-mappning för länkod %s", lan)
            continue
        n3_kod, n3_namn, n2_parent = _NUTS_3_PER_LAN[lan]
        n2_namn, n1_parent = nuts2_lookup[n2_parent]
        n1_namn = nuts1_lookup[n1_parent]
        rader.append((k["kod"], 1, n1_parent, n1_namn))
        rader.append((k["kod"], 2, n2_parent, n2_namn))
        rader.append((k["kod"], 3, n3_kod, n3_namn))

    if not rader:
        return 0

    sql = (
        f"INSERT INTO {db.prefix()}nuts (kommun_kod, niva, kod, namn) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}) "
        f"ON CONFLICT (kommun_kod, niva) DO UPDATE SET "
        f"kod = EXCLUDED.kod, namn = EXCLUDED.namn"
    )
    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, rader)
    return len(rader)


# ============================================================================
# DeSO + RegSO — parser för SCB:s kopplings-xlsx
# ============================================================================


async def synka_deso_och_regso(
    url: str | None = None,
    ar: int = 2025,
) -> dict[str, int]:
    """Synkar DeSO och RegSO från SCB:s kombinerade xlsx-fil.

    SCB publicerar en fil som mappar varje DeSO-område till RegSO och
    kommun, med namn på formen `koppling-deso<år>-regso<år>_<datum>.xlsx`.
    Adressen ändras när SCB publicerar nya filer — sätt den explicit eller
    via `DOA_DESO_REGSO_URL`.

    Filstruktur:
        - Blad: Blad1
        - Rad 0–2: titel, varningstext, blank
        - Rad 3: rubrikrad (Kommun, Kommunnamn, DeSO_2025, RegSO_2025, RegSOkod_2025)
        - Rad 4+: data

    DeSO-typen utvinns ur kodens femte tecken: A landsbygd, B tätort utanför
    centralort, C centralort. DeSO har koder, inte namn — så är indelningen
    konstruerad — och `namn` sätts till koden för att uppfylla NOT NULL i
    schemat.

    Returnerar `{"antal_deso": ..., "antal_regso": ...}`.
    """
    if url is None:
        url = os.getenv("DOA_DESO_REGSO_URL", "").strip()
    if not url:
        raise ValueError(
            "ingen URL angiven — skicka `url` eller sätt "
            "DOA_DESO_REGSO_URL i .env"
        )

    logger.info("Hämtar DeSO/RegSO från %s", url)
    import io
    import pandas as pd

    innehall = await _hamta_xlsx(url)
    df = pd.read_excel(io.BytesIO(innehall), header=3, dtype=str)
    df = df.dropna(subset=["DeSO_2025", "RegSOkod_2025"])

    deso_rader: list[tuple[str, str, str, str | None, int]] = []
    for _, r in df.iterrows():
        kod = str(r["DeSO_2025"]).strip()
        kommun_kod = str(r["Kommun"]).strip().zfill(4)
        typ = kod[4] if len(kod) >= 5 else None
        deso_rader.append((kod, kod, kommun_kod, typ, ar))

    # RegSO dedupe på kod
    regso_uniq: dict[str, tuple[str, str]] = {}
    for _, r in df.iterrows():
        kod = str(r["RegSOkod_2025"]).strip()
        namn = str(r["RegSO_2025"]).strip()
        kommun_kod = str(r["Kommun"]).strip().zfill(4)
        if kod not in regso_uniq:
            regso_uniq[kod] = (namn, kommun_kod)
    regso_rader = [(k, n, kk, ar) for k, (n, kk) in regso_uniq.items()]

    async with db.hamta_db() as anslutning:
        await anslutning.executemany(
            f"INSERT INTO {db.prefix()}deso (kod, namn, kommun_kod, typ, ar) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}, {db.ph(5)}) "
            f"ON CONFLICT (kod) DO UPDATE SET "
            f"namn = EXCLUDED.namn, kommun_kod = EXCLUDED.kommun_kod, "
            f"typ = EXCLUDED.typ, ar = EXCLUDED.ar",
            deso_rader,
        )
        await anslutning.executemany(
            f"INSERT INTO {db.prefix()}regso (kod, namn, kommun_kod, ar) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}) "
            f"ON CONFLICT (kod) DO UPDATE SET "
            f"namn = EXCLUDED.namn, kommun_kod = EXCLUDED.kommun_kod, "
            f"ar = EXCLUDED.ar",
            regso_rader,
        )

    logger.info("Synkade %d DeSO + %d RegSO", len(deso_rader), len(regso_rader))
    return {"antal_deso": len(deso_rader), "antal_regso": len(regso_rader)}


# ============================================================================
# Valdistrikt — parser för val.se:s xlsx
# ============================================================================


# DeSO har koder, inte namn — till skillnad från RegSO. Bokstaven på femte
# positionen bär informationen, och den är verifierad mot geometrin:
# A-områden har medianytan 194 km², B 7,5 och C 0,9. Stockholm består av
# 569 C utan ett enda A; Kiruna har fyra A.
DESO_TYPER: dict[str, str] = {
    "A": "Landsbygd",
    "B": "Tätort utanför centralort",
    "C": "Centralort",
}


def deso_typtext(kod: str) -> str | None:
    """Bokstavens betydelse ur en DeSO-kod, t.ex. 0180C1390 -> Centralort."""
    if not kod or len(kod) < 5:
        return None
    return DESO_TYPER.get(kod[4].upper())


def _kod_eller_none(varde: Any) -> str | None:
    """Tom cell -> None, aldrig strängen "nan".

    val.se:s fil läses med pandas, som gör tomma celler till NaN. `str()`
    på ett NaN ger "nan", och den strängen är sann — så `kod or None` släpper
    igenom den. Gotlands fyrtio valdistrikt har inget regionval, eftersom
    kommunen också är region, och fick därför "nan" som regionvalkretskod.
    Unionen av dem bildade sedan en 63:e regionvalkrets som inte finns.
    """
    if varde is None:
        return None
    text = str(varde).strip()
    if not text or text.lower() in ("nan", "none", "<na>"):
        return None
    return text


async def synka_valdistrikt(
    url: str | None = None,
    ar: int = 2026,
) -> dict[str, int]:
    """Synkar valdistrikt och alla tre valkretstyper från val.se.

    Valmyndigheten publicerar en xlsx-fil per valår med ca 6 000
    valdistrikt och varje distrikts mappning mot kommun, län,
    riksdagsvalkrets samt kommun- och regionvalkrets. En enda fil ger
    underlag för all valgeografi.

    URL ändras per valår — passa in explicit eller sätt
    `DOA_VALDISTRIKT_URL` i `.env`.

    Funktionen:
        1. Extraherar unika kommunvalkretsar och regionvalkretsar ur filen
           och upsertar dem i `valkrets` med rätt `typ`.
        2. Upsertar alla valdistrikt i `valdistrikt` med koder för alla
           tre valkretstyper.

    Returnerar antal skrivna rader per kategori.
    """
    if url is None:
        url = os.getenv("DOA_VALDISTRIKT_URL", "").strip()
    if not url:
        raise ValueError(
            "ingen URL angiven — skicka `url` eller sätt "
            "DOA_VALDISTRIKT_URL i .env"
        )

    logger.info("Hämtar valdistrikt %d från %s", ar, url)
    import io
    import pandas as pd

    innehall = await _hamta_xlsx(url)
    df = pd.read_excel(io.BytesIO(innehall), dtype=str)
    df = df.dropna(subset=["Valdistriktskod", "Kommunkod"])

    # Dedupe kommun- och regionvalkretsar
    kommunvalkretsar: dict[str, str] = {}
    regionvalkretsar: dict[str, str] = {}
    valdistrikt_rader: list[tuple[str, int, str, str, str, str, str]] = []

    for _, r in df.iterrows():
        vd_kod = str(r["Valdistriktskod"]).strip()
        vd_namn = str(r["Valdistrikt"]).strip()
        kommun_kod = str(r["Kommunkod"]).strip().zfill(4)

        riksdagsvk = str(r["Riksdagsvalkretskod"]).strip().zfill(2)
        kommunvk_kod = _kod_eller_none(r["Kommunvalkretskod"])
        kommunvk_namn = str(r["Kommunvalkrets"]).strip()
        regionvk_kod = _kod_eller_none(r["Regionvalkretskod"])
        regionvk_namn = str(r["Regionvalkrets"]).strip()

        if kommunvk_kod:
            kommunvalkretsar[kommunvk_kod] = kommunvk_namn
        if regionvk_kod:
            regionvalkretsar[regionvk_kod] = regionvk_namn

        valdistrikt_rader.append((
            vd_kod, ar, vd_namn, kommun_kod,
            riksdagsvk, kommunvk_kod, regionvk_kod,
        ))

    if not valdistrikt_rader:
        logger.warning("Inga valdistriktsrader hittades")
        return {"antal_valdistrikt": 0, "antal_kommunvalkretsar": 0,
                "antal_regionvalkretsar": 0}

    vk_upsert = (
        f"INSERT INTO {db.prefix()}valkrets (typ, kod, ar, namn) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}) "
        f"ON CONFLICT (typ, kod, ar) DO UPDATE SET namn = EXCLUDED.namn"
    )
    vd_upsert = (
        f"INSERT INTO {db.prefix()}valdistrikt "
        f"(kod, ar, namn, kommun_kod, riksdagsvalkrets_kod, "
        f" kommunvalkrets_kod, regionvalkrets_kod) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}, "
        f"        {db.ph(5)}, {db.ph(6)}, {db.ph(7)}) "
        f"ON CONFLICT (kod, ar) DO UPDATE SET "
        f"namn = EXCLUDED.namn, kommun_kod = EXCLUDED.kommun_kod, "
        f"riksdagsvalkrets_kod = EXCLUDED.riksdagsvalkrets_kod, "
        f"kommunvalkrets_kod = EXCLUDED.kommunvalkrets_kod, "
        f"regionvalkrets_kod = EXCLUDED.regionvalkrets_kod"
    )

    async with db.hamta_db() as anslutning:
        await anslutning.executemany(
            vk_upsert,
            [("kommun", k, ar, n) for k, n in kommunvalkretsar.items()],
        )
        await anslutning.executemany(
            vk_upsert,
            [("region", k, ar, n) for k, n in regionvalkretsar.items()],
        )
        await anslutning.executemany(vd_upsert, valdistrikt_rader)

    logger.info(
        "Valdistrikt-synk %d: %d distrikt, %d kommunvk, %d regionvk",
        ar, len(valdistrikt_rader),
        len(kommunvalkretsar), len(regionvalkretsar),
    )
    return {
        "antal_valdistrikt": len(valdistrikt_rader),
        "antal_kommunvalkretsar": len(kommunvalkretsar),
        "antal_regionvalkretsar": len(regionvalkretsar),
    }


# ============================================================================
# Postnummer — parser mot Geonames på Huwise
# ============================================================================


async def synka_postnummer_via_huwise(
    instans: str = "huwise",
    dataset_id: str = "geonames-postal-code",
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> dict[str, int]:
    """Synkar svenska postnummer från Geonames-datasetet på Huwise.

    Standardkällan är hub.huwise.com (instans `huwise`). Instansen `global`
    är samma katalog på data.opendatasoft.com, domänen från tiden då
    bolaget hette Opendatasoft; den är reserven om datasetet skulle saknas
    på hub.huwise.com.

    Datasetet innehåller punktkoordinater per postnummer-ort, inte
    polygongränser. Polygongränser för svenska postnummerområden ägs av
    PostNord och finns inte i Geonames.

    Källfältets `admin_code2` är SCB:s 4-siffriga kommunkod när den finns.
    Vissa rader (flygplatspostnummer, speciella koder) saknar den, och
    för dem blir `dominant_kommun_kod` NULL och ingen rad skrivs till
    `postnummer_kommun`.

    Returnerar `{"antal_postnummer": ..., "antal_postnummer_kommun": ...}`.
    """
    import csv
    import io

    logger.info("Hämtar svenska postnummer från huwise %s/%s", instans, dataset_id)
    await rapportera(0, None, "Hämtar postnummerexporten")
    raw = await huwise.exportera(
        instans, dataset_id, format="csv", where='country_code="SE"',
    )
    # Huwise CSV-export använder ';' som avgränsare
    lasare = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";")

    postnummer_rader: dict[str, tuple[str, str | None, float | None, float | None]] = {}
    pk_rader: list[tuple[str, str, float]] = []

    for r in lasare:
        rå_kod = (r.get("postal_code") or "").strip()
        # Normalisera: ta bort mellanrum, se till att 5 siffror
        kod = "".join(c for c in rå_kod if c.isdigit())
        if len(kod) != 5:
            continue
        ort = (r.get("place_name") or "").strip()
        kommun_kod = (r.get("admin_code2") or "").strip()
        kommun_kod = kommun_kod.zfill(4) if kommun_kod else None

        try:
            lat = float(r["latitude"]) if r.get("latitude") else None
            lng = float(r["longitude"]) if r.get("longitude") else None
        except (ValueError, TypeError):
            lat = lng = None

        # Geonames-datasetet kan ha flera rader per postnummer (t.ex. olika
        # orter inom samma postnummerområde). Dedupe — första vinner för
        # huvudtabellen, men varje (postnr, kommun) länk räknas en gång.
        if kod not in postnummer_rader:
            postnummer_rader[kod] = (ort, kommun_kod, lat, lng)
        if kommun_kod:
            pk_rader.append((kod, kommun_kod, 1.0))
        await rapportera(len(pk_rader), None, f"Läste {len(pk_rader)} postnummer")

    # Dedupe (postnr, kommun) — datasetet kan upprepa
    pk_unika: dict[tuple[str, str], float] = {}
    for postnr, kommun_kod, andel in pk_rader:
        pk_unika[(postnr, kommun_kod)] = andel

    if not postnummer_rader:
        logger.warning("Inga svenska postnummer hittades")
        return {"antal_postnummer": 0, "antal_postnummer_kommun": 0}

    pn_rows = [
        (postnr, ort, kommun_kod, lat, lng)
        for postnr, (ort, kommun_kod, lat, lng) in postnummer_rader.items()
    ]
    pk_rows = [(p, k, a) for (p, k), a in pk_unika.items()]

    async with db.hamta_db() as anslutning:
        await anslutning.executemany(
            f"INSERT INTO {db.prefix()}postnummer "
            f"(postnr, ort, dominant_kommun_kod, latitud, longitud) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}, {db.ph(5)}) "
            f"ON CONFLICT (postnr) DO UPDATE SET "
            f"ort = EXCLUDED.ort, "
            f"dominant_kommun_kod = EXCLUDED.dominant_kommun_kod, "
            f"latitud = EXCLUDED.latitud, "
            f"longitud = EXCLUDED.longitud",
            pn_rows,
        )
        await anslutning.executemany(
            f"INSERT INTO {db.prefix()}postnummer_kommun "
            f"(postnr, kommun_kod, andel) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}) "
            f"ON CONFLICT (postnr, kommun_kod) DO UPDATE SET "
            f"andel = EXCLUDED.andel",
            pk_rows,
        )

    logger.info(
        "Postnummer synkade: %d postnummer, %d postnummer-kommun-länkar",
        len(pn_rows), len(pk_rows),
    )
    return {
        "antal_postnummer": len(pn_rows),
        "antal_postnummer_kommun": len(pk_rows),
    }
