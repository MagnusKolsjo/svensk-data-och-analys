# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Mellanlagring av data från filbaserade källor.

Myndigheter utan API levererar Excel- eller CSV-filer. Att hämta och tolka om
filen vid varje fråga är långsamt och onödigt tryck på källan — men filerna
hör inte hemma i det permanenta beståndet. De går att hämta igen, till
skillnad från SCB:s omräknade folkmängdsserie, och regeln för vad som lagras
permanent är just att det inte ska gå.

Mellanlagret har därför hållbarhet. `DOA_MELLANLAGRING_DAGAR` styr hur länge
en hämtning behålls innan den räknas som gammal; standard 30 dagar.
`rensa()` tar bort det som passerat, och bör köras efter ett jobb som hämtat
mycket — annars växer lagret tyst med varje ny årgång.

Ingångspunkter:
    hallbarhet_dagar() -> int
    las(kalla, dataset, blad) -> dict | None
    skriv(kalla, dataset, blad, kolumner, rader, ...) -> None
    las_original(url) -> tuple[bytes, str] | None
    skriv_original(url, innehall, innehallstyp) -> None
    rensa(kalla=None, aldre_an_dagar=None) -> int
    status() -> list[dict]
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from data_och_analys.infra import db

logger = logging.getLogger(__name__)

STANDARD_DAGAR = 30


def hallbarhet_dagar() -> int:
    """Hur länge en hämtning behålls. Ur `DOA_MELLANLAGRING_DAGAR`."""
    rå = os.getenv("DOA_MELLANLAGRING_DAGAR", "").strip()
    if not rå:
        return STANDARD_DAGAR
    try:
        dagar = int(rå)
    except ValueError:
        logger.warning(
            "DOA_MELLANLAGRING_DAGAR=%r går inte att tolka som heltal — "
            "använder %d dagar", rå, STANDARD_DAGAR,
        )
        return STANDARD_DAGAR
    if dagar < 1:
        logger.warning(
            "DOA_MELLANLAGRING_DAGAR=%d är mindre än en dag; mellanlagret "
            "skulle då aldrig användas. Använder %d.", dagar, STANDARD_DAGAR,
        )
        return STANDARD_DAGAR
    return dagar


async def las(kalla: str, dataset: str, blad: str) -> dict[str, Any] | None:
    """Hämtar en lagrad tabell om den finns och inte är för gammal."""
    grans = datetime.now(timezone.utc) - timedelta(days=hallbarhet_dagar())
    async with db.hamta_db() as anslutning:
        rad = await anslutning.fetchrow(
            f"SELECT kolumner, rader, maskerade, hamtad, kalla_url "
            f"FROM {db.prefix()}fil_mellanlager "
            f"WHERE kalla = $1 AND dataset = $2 AND blad = $3 AND hamtad >= $4",
            kalla, dataset, blad, grans,
        )
    if rad is None:
        return None
    return {
        "kolumner": json.loads(rad["kolumner"]) if isinstance(rad["kolumner"], str)
                    else rad["kolumner"],
        "datapunkter": json.loads(rad["rader"]) if isinstance(rad["rader"], str)
                       else rad["rader"],
        "maskerade": rad["maskerade"],
        "hamtad": rad["hamtad"].isoformat(),
        "kalla_url": rad["kalla_url"],
        "ur_mellanlager": True,
    }


async def skriv(
    kalla: str, dataset: str, blad: str,
    kolumner: list[str], rader: list[dict[str, Any]],
    maskerade: int = 0, kalla_url: str | None = None,
) -> None:
    """Lagrar en tolkad tabell. Skriver över en tidigare hämtning."""
    async with db.hamta_db() as anslutning:
        await anslutning.execute(
            f"INSERT INTO {db.prefix()}fil_mellanlager "
            f"(kalla, dataset, blad, kolumner, rader, maskerade, kalla_url, hamtad) "
            f"VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, NOW()) "
            f"ON CONFLICT (kalla, dataset, blad) DO UPDATE SET "
            f"kolumner = EXCLUDED.kolumner, rader = EXCLUDED.rader, "
            f"maskerade = EXCLUDED.maskerade, kalla_url = EXCLUDED.kalla_url, "
            f"hamtad = NOW()",
            kalla, dataset, blad,
            json.dumps(kolumner, ensure_ascii=False),
            json.dumps(rader, ensure_ascii=False, default=str),
            maskerade, kalla_url,
        )


async def las_original(url: str) -> tuple[bytes, str] | None:
    """En hämtad originalfil och dess innehållstyp, om den inte är för gammal."""
    grans = datetime.now(timezone.utc) - timedelta(days=hallbarhet_dagar())
    async with db.hamta_db() as anslutning:
        rad = await anslutning.fetchrow(
            f"SELECT innehall, innehallstyp FROM {db.prefix()}fil_mellanlager_original "
            f"WHERE url = $1 AND hamtad >= $2",
            url, grans,
        )
    return (bytes(rad["innehall"]), rad["innehallstyp"] or "") if rad else None


async def skriv_original(url: str, innehall: bytes, innehallstyp: str = "") -> None:
    """Lagrar en fil som den kom. Skriver över en tidigare hämtning."""
    async with db.hamta_db() as anslutning:
        await anslutning.execute(
            f"INSERT INTO {db.prefix()}fil_mellanlager_original "
            f"(url, innehallstyp, storlek, innehall, hamtad) VALUES ($1, $2, $3, $4, NOW()) "
            f"ON CONFLICT (url) DO UPDATE SET innehallstyp = EXCLUDED.innehallstyp, "
            f"storlek = EXCLUDED.storlek, innehall = EXCLUDED.innehall, hamtad = NOW()",
            url, innehallstyp, len(innehall), innehall,
        )


async def rensa(kalla: str | None = None, aldre_an_dagar: int | None = None) -> int:
    """Tar bort hämtningar som passerat hållbarheten. Returnerar antal.

    Originalfilerna har ingen källnyckel; de rensas när `kalla` utelämnas
    eller är `oppnadata`, som är deras avnämare.
    """
    dagar = aldre_an_dagar if aldre_an_dagar is not None else hallbarhet_dagar()
    grans = datetime.now(timezone.utc) - timedelta(days=dagar)
    villkor = "hamtad < $1"
    params: list[Any] = [grans]
    if kalla:
        villkor += " AND kalla = $2"
        params.append(kalla)
    async with db.hamta_db() as anslutning:
        antal = await anslutning.fetchval(
            f"WITH bort AS (DELETE FROM {db.prefix()}fil_mellanlager "
            f"WHERE {villkor} RETURNING 1) SELECT count(*) FROM bort",
            *params,
        ) or 0
        if kalla in (None, "oppnadata"):
            antal += await anslutning.fetchval(
                f"WITH bort AS (DELETE FROM {db.prefix()}fil_mellanlager_original "
                f"WHERE hamtad < $1 RETURNING 1) SELECT count(*) FROM bort",
                grans,
            ) or 0
    return antal


async def status() -> list[dict[str, Any]]:
    """Vad som ligger i mellanlagret, med ålder och storlek."""
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            f"SELECT kalla, dataset, blad, hamtad, maskerade, "
            f"jsonb_array_length(rader) AS antal_rader, "
            f"pg_column_size(rader) AS byte "
            f"FROM {db.prefix()}fil_mellanlager ORDER BY kalla, dataset, blad"
        )
        original = await anslutning.fetch(
            f"SELECT url, hamtad, storlek FROM {db.prefix()}fil_mellanlager_original ORDER BY url"
        )
    nu = datetime.now(timezone.utc)
    return [
        {
            "kalla": r["kalla"], "dataset": r["dataset"], "blad": r["blad"],
            "antal_rader": r["antal_rader"], "maskerade": r["maskerade"],
            "byte": r["byte"],
            "alder_dagar": (nu - r["hamtad"]).days,
            "hallbarhet_dagar": hallbarhet_dagar(),
        }
        for r in rader
    ] + [
        {
            "kalla": "oppnadata", "dataset": r["url"], "blad": "(originalfil)",
            "antal_rader": None, "maskerade": 0, "byte": r["storlek"],
            "alder_dagar": (nu - r["hamtad"]).days,
            "hallbarhet_dagar": hallbarhet_dagar(),
        }
        for r in original
    ]
