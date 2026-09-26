# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Historisk folkmängd per kommun — SCB:s retroaktivt harmoniserade serie.

SCB publicerar varje år en Excel-fil med folkmängden i samtliga kommuner
1950 → senaste år, harmoniserad till den nuvarande kommunindelningen.
Filen fyller en lucka som PxWebApi inte täcker: dagens 290 kommuner
bakåt över hela tidsperioden — kommuner som formellt skapades senare
(Knivsta 2003) får data tilldelad bakåt; kommuner som upphört finns
inte med.

Data lagras i `kommun_folkmangd`-tabellen med `indelning_ar` som
versionsflagga (2025 för nuvarande SCB-publikation). När SCB publicerar
en ny indelning skrivs raderna under nytt `indelning_ar` utan att den
gamla raderas — för spårbarhet.

Excelfilen har kommunnamn men inte koder. Namn mappas mot
`kommun.namn` i lokala DB:n; mismatchningar (SCB:s namnform vs
Tillväxtverkets) loggas och raden hoppas över.

Ingångspunkter:
    synka_kommun_folkmangd(url=None, sokvag=None) -> SynkResultat
    hamta_kommun_folkmangd_historik(kommun_kod, fran=None, till=None,
                                    indelning_ar=None) -> list[dict]
    hamta_aret_per_kommun(ar, indelning_ar=None) -> list[dict]
    hamta_matris(fran=None, till=None, kommun_koder=None,
                 indelning_ar=None) -> dict
    hamta_topp(ar, antal=50, vaxtperiod=None, indelning_ar=None) -> list[dict]
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import openpyxl

from data_och_analys.infra import db

logger = logging.getLogger(__name__)


# SCB:s offentliga URL för 2024 års publikation (1950-2024). Vid årlig
# uppdatering skiftar contentassets-segmentet — kan överstyras via
# argument till `synka_kommun_folkmangd` eller via DOA_SCB_FOLKMANGD_URL.
SCB_FOLKMANGD_URL = (
    "https://www.scb.se/contentassets/"
    "ef676b89b44d4bcbacce5b2d399065cd/be0101_folkmangdkom2024.xlsx"
)

# SCB:s aktuella publikation följer 2025 års kommunindelning.
# Slutåret för data — uppdateras när SCB släpper ny fil.
NUVARANDE_INDELNING_AR = 2025

USER_AGENT = "svensk-data-och-analys/SCBFolkmangdLoader (+https://github.com/)"


@dataclass
class SynkResultat:
    """Sammanfattning från `synka_kommun_folkmangd`."""

    rader_skrivna: int
    kommuner_matchade: int
    kommuner_omatchade: list[str]
    fran_ar: int
    till_ar: int
    indelning_ar: int


# ============================================================================
# Excel-läsning
# ============================================================================


async def _hamta_excel(url: str) -> bytes:
    """Laddar ner Excel-filen från SCB."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as klient:
        svar = await klient.get(url)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"SCB-folkmängdsfil ej hämtbar ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.content


def _parsa_excel(innehall: bytes) -> tuple[list[int], list[tuple[str, list[int | None]]]]:
    """Returnerar (ärgrubrik, [(kommunnamn, [folkmängd per år])]).

    Excelfilen har rubriken på rad 6 (`Kommun, 1950, 1951, …`) och
    kommundata från rad 7 tills en specialrad `Kommentar:`. Tomma
    kolumner efter sista året kan finnas — vi trimmar till första
    `None` i rubriken.
    """
    wb = openpyxl.load_workbook(io.BytesIO(innehall), read_only=True, data_only=True)
    ws = wb["Tabell"]

    # Rubrikrad 6: kol 1 = "Kommun", kol 2..N = år
    rubrik = next(ws.iter_rows(min_row=6, max_row=6, values_only=True))
    # Trimma till sista cellen som är en heltalsårtal
    ar_lista: list[int] = []
    for varde in rubrik[1:]:
        if isinstance(varde, int):
            ar_lista.append(varde)
        else:
            break

    rader: list[tuple[str, list[int | None]]] = []
    for rad in ws.iter_rows(min_row=7, max_row=ws.max_row, values_only=True):
        namn = rad[0]
        if not isinstance(namn, str):
            continue
        if namn.strip().startswith("Kommentar"):
            break
        folkmangder: list[int | None] = []
        for v in rad[1 : 1 + len(ar_lista)]:
            if isinstance(v, int):
                folkmangder.append(v)
            elif v is None:
                folkmangder.append(None)
            else:
                # Strängar med tankestreck etc — ses som okänt värde
                try:
                    folkmangder.append(int(v))
                except (TypeError, ValueError):
                    folkmangder.append(None)
        rader.append((namn.strip(), folkmangder))

    return ar_lista, rader


# ============================================================================
# Namnmappning mot lokala kommun-tabellen
# ============================================================================


# SCB:s namn vs det vi seedade från Tillväxtverket. Lista kompletteras
# vid synk om en mismatch loggas — manuella tillägg krävs bara vid
# riktiga namnförändringar (mycket sällsynta).
_NAMNALIAS: dict[str, str] = {
    # SCB-form → vår form (om de skiljer sig)
    # Exempel om det skulle behövas:
    # "Falu kommun": "Falun",
}


async def _hamta_namn_till_kod() -> dict[str, str]:
    """Slår upp `{namn: kommun_kod}` från `kommun`-tabellen."""
    sql = f"SELECT kod, namn FROM {db.prefix()}kommun"
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql)
    return {r["namn"]: r["kod"] for r in rader}


def _matcha_namn(
    scb_namn: str, namn_till_kod: dict[str, str]
) -> str | None:
    """Försöker matcha SCB:s kommunnamn mot vår kod.

    Strategi: exakt namn → alias-tabell → trimmad form (utan
    " kommun"-suffix om någon av sidorna har det).
    """
    if scb_namn in namn_till_kod:
        return namn_till_kod[scb_namn]
    aliasad = _NAMNALIAS.get(scb_namn)
    if aliasad and aliasad in namn_till_kod:
        return namn_till_kod[aliasad]
    # Trimma " kommun"-suffix på antingen sida
    test = scb_namn.removesuffix(" kommun")
    if test in namn_till_kod:
        return namn_till_kod[test]
    test_plus = f"{scb_namn} kommun"
    if test_plus in namn_till_kod:
        return namn_till_kod[test_plus]
    return None


# ============================================================================
# Synk-flöde
# ============================================================================


async def synka_kommun_folkmangd(
    url: str | None = None,
    sokvag: Path | None = None,
    indelning_ar: int = NUVARANDE_INDELNING_AR,
) -> SynkResultat:
    """Synkar SCB:s historiska folkmängdsserie till `kommun_folkmangd`.

    `url` eller `sokvag` — om båda anges vinner `sokvag` (för
    offline-tester). Standard hämtar från SCB.

    Befintliga rader för samma `(kommun_kod, ar, indelning_ar)`
    uppdateras via UPSERT. Kommuner som inte matchar mot lokala
    `kommun`-tabellen rapporteras i `kommuner_omatchade` — ingen
    rad skrivs för dem.
    """
    if sokvag is not None:
        innehall = sokvag.read_bytes()
    else:
        innehall = await _hamta_excel(url or SCB_FOLKMANGD_URL)

    ar_lista, rader = _parsa_excel(innehall)
    namn_till_kod = await _hamta_namn_till_kod()

    insert_rader: list[tuple[str, int, int, int]] = []
    matchade: set[str] = set()
    omatchade: list[str] = []

    for namn, folkmangder in rader:
        kod = _matcha_namn(namn, namn_till_kod)
        if kod is None:
            omatchade.append(namn)
            continue
        matchade.add(namn)
        for ar, f in zip(ar_lista, folkmangder):
            if f is None:
                continue
            insert_rader.append((kod, ar, f, indelning_ar))

    if insert_rader:
        sql = (
            f"INSERT INTO {db.prefix()}kommun_folkmangd "
            f"(kommun_kod, ar, folkmangd, indelning_ar) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)}, {db.ph(4)}) "
            f"ON CONFLICT (kommun_kod, ar, indelning_ar) "
            f"DO UPDATE SET folkmangd = EXCLUDED.folkmangd"
        )
        async with db.hamta_db() as anslutning:
            await anslutning.executemany(sql, insert_rader)

    return SynkResultat(
        rader_skrivna=len(insert_rader),
        kommuner_matchade=len(matchade),
        kommuner_omatchade=omatchade,
        fran_ar=min(ar_lista) if ar_lista else 0,
        till_ar=max(ar_lista) if ar_lista else 0,
        indelning_ar=indelning_ar,
    )


# ============================================================================
# Access-lager
# ============================================================================


async def _senaste_indelning_ar() -> int | None:
    sql = f"SELECT MAX(indelning_ar) FROM {db.prefix()}kommun_folkmangd"
    async with db.hamta_db() as anslutning:
        return await anslutning.fetchval(sql)


async def hamta_kommun_folkmangd_historik(
    kommun_kod: str,
    fran: int | None = None,
    till: int | None = None,
    indelning_ar: int | None = None,
) -> list[dict[str, Any]]:
    """Folkmängd per år för en kommun.

    Saknat `indelning_ar` slår mot senaste tillgängliga indelning —
    historiska analyser anger explicit indelningsår.
    """
    ind = indelning_ar if indelning_ar is not None else await _senaste_indelning_ar()
    if ind is None:
        return []
    villkor = [f"kommun_kod = {db.ph(1)}", f"indelning_ar = {db.ph(2)}"]
    parametrar: list[Any] = [kommun_kod, ind]
    if fran is not None:
        villkor.append(f"ar >= {db.ph(len(parametrar) + 1)}")
        parametrar.append(fran)
    if till is not None:
        villkor.append(f"ar <= {db.ph(len(parametrar) + 1)}")
        parametrar.append(till)
    sql = (
        f"SELECT kommun_kod, ar, folkmangd, indelning_ar "
        f"FROM {db.prefix()}kommun_folkmangd "
        f"WHERE {' AND '.join(villkor)} ORDER BY ar"
    )
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, *parametrar)
    return [dict(r) for r in rader]


async def hamta_aret_per_kommun(
    ar: int, indelning_ar: int | None = None
) -> list[dict[str, Any]]:
    """Folkmängd för alla kommuner ett givet år."""
    ind = indelning_ar if indelning_ar is not None else await _senaste_indelning_ar()
    if ind is None:
        return []
    sql = (
        f"SELECT f.kommun_kod, k.namn AS kommun_namn, f.ar, f.folkmangd, "
        f"f.indelning_ar "
        f"FROM {db.prefix()}kommun_folkmangd f "
        f"JOIN {db.prefix()}kommun k ON k.kod = f.kommun_kod "
        f"WHERE f.ar = {db.ph(1)} AND f.indelning_ar = {db.ph(2)} "
        f"ORDER BY k.namn"
    )
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, ar, ind)
    return [dict(r) for r in rader]


async def hamta_matris(
    fran: int | None = None,
    till: int | None = None,
    kommun_koder: list[str] | None = None,
    indelning_ar: int | None = None,
) -> dict[str, Any]:
    """Hela folkmängdsmatrisen i kompakt form — en enda MCP-svarscell.

    Returstrukturen är optimerad för bredbild-analys (rangordningar,
    persistens, percentil-mönster) där hela datasetet behövs samtidigt.
    En rad-per-cell-form sprängde MCP-kanalens 1 MB-cap; matris-formen
    ger ~150 KB för 290 kommuner × 75 år och laddas direkt till
    pandas/numpy.

    Format:

        {
          "indelning_ar": 2025,
          "fran_ar":      1950,
          "till_ar":      2024,
          "ar":           [1950, 1951, ..., 2024],
          "kommuner":     [{"kod": "0114", "namn": "Upplands Väsby"}, ...],
          "matris":       [[12744, 12812, ...], [...], ...]
        }

    `matris[i][j]` = folkmängden för `kommuner[i]` år `ar[j]`. `None`
    om värdet saknas för en specifik (kommun, år).

    Pandas-laddning:

        df = pd.DataFrame(
            res["matris"],
            index=[k["kod"] for k in res["kommuner"]],
            columns=res["ar"],
        )

    `fran` och `till` är inklusiva årsintervall. `kommun_koder`
    begränsar urvalet till en delmängd kommuner.
    """
    ind = indelning_ar if indelning_ar is not None else await _senaste_indelning_ar()
    if ind is None:
        return {
            "indelning_ar": None, "fran_ar": None, "till_ar": None,
            "ar": [], "kommuner": [], "matris": [],
        }

    villkor = [f"f.indelning_ar = {db.ph(1)}"]
    parametrar: list[Any] = [ind]
    if fran is not None:
        villkor.append(f"f.ar >= {db.ph(len(parametrar) + 1)}")
        parametrar.append(fran)
    if till is not None:
        villkor.append(f"f.ar <= {db.ph(len(parametrar) + 1)}")
        parametrar.append(till)
    if kommun_koder:
        # Postgres tar ANY($N::text[]); SQLite tar IN-villkor.
        # `db.ph` ger oss ett portabelt placeholder-namn. Vi bygger
        # en IN-lista av enskilda placeholders för att hålla det
        # rakt över bägge backends.
        placeholder_lista = ", ".join(
            db.ph(len(parametrar) + 1 + i) for i in range(len(kommun_koder))
        )
        villkor.append(f"f.kommun_kod IN ({placeholder_lista})")
        parametrar.extend(kommun_koder)

    sql = (
        f"SELECT f.kommun_kod, k.namn AS kommun_namn, f.ar, f.folkmangd "
        f"FROM {db.prefix()}kommun_folkmangd f "
        f"JOIN {db.prefix()}kommun k ON k.kod = f.kommun_kod "
        f"WHERE {' AND '.join(villkor)} "
        f"ORDER BY f.kommun_kod, f.ar"
    )
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, *parametrar)

    if not rader:
        return {
            "indelning_ar": ind, "fran_ar": fran, "till_ar": till,
            "ar": [], "kommuner": [], "matris": [],
        }

    # Bygg år-axeln (sortable union av befintliga år)
    ar_set = sorted({r["ar"] for r in rader})
    ar_idx = {a: i for i, a in enumerate(ar_set)}

    # Bygg kommun-axeln (sortable union av kod, namn)
    kommun_namn: dict[str, str] = {}
    for r in rader:
        kommun_namn.setdefault(r["kommun_kod"], r["kommun_namn"])
    kommun_koder_sorted = sorted(kommun_namn)
    kommun_idx = {k: i for i, k in enumerate(kommun_koder_sorted)}

    # Initiera matris med None
    matris: list[list[int | None]] = [
        [None] * len(ar_set) for _ in kommun_koder_sorted
    ]
    for r in rader:
        i = kommun_idx[r["kommun_kod"]]
        j = ar_idx[r["ar"]]
        matris[i][j] = r["folkmangd"]

    return {
        "indelning_ar": ind,
        "fran_ar": ar_set[0],
        "till_ar": ar_set[-1],
        "ar": ar_set,
        "kommuner": [
            {"kod": k, "namn": kommun_namn[k]} for k in kommun_koder_sorted
        ],
        "matris": matris,
    }


async def hamta_topp(
    ar: int,
    antal: int = 50,
    indelning_ar: int | None = None,
) -> list[dict[str, Any]]:
    """Topp N kommuner efter folkmängd ett givet år (server-side ORDER BY).

    Slipper skicka hela bredsidan över MCP-kanalen när bara
    rangordningen behövs (Fråga 12: historisk topp).
    """
    ind = indelning_ar if indelning_ar is not None else await _senaste_indelning_ar()
    if ind is None:
        return []
    sql = (
        f"SELECT f.kommun_kod, k.namn AS kommun_namn, f.ar, f.folkmangd, "
        f"f.indelning_ar, "
        f"RANK() OVER (ORDER BY f.folkmangd DESC) AS rang "
        f"FROM {db.prefix()}kommun_folkmangd f "
        f"JOIN {db.prefix()}kommun k ON k.kod = f.kommun_kod "
        f"WHERE f.ar = {db.ph(1)} AND f.indelning_ar = {db.ph(2)} "
        f"ORDER BY f.folkmangd DESC "
        f"LIMIT {db.ph(3)}"
    )
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, ar, ind, antal)
    return [dict(r) for r in rader]
