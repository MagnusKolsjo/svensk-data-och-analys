# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Speglar Skolverkets skolenheter med koordinater till ett punktlager.

Skolenheter är punkter, inte ytor — de hör hemma i ett punktlager och aldrig
i en choropleth. Koordinaterna ligger bara i detalj-API:et, ett anrop per
enhet, och mot Skolverkets tak på 30 anrop / 10 sekunder tar ett fullt pass
över ~6 700 enheter drygt en halvtimme. Därför en spegeltabell som sedan
läses utan nätanrop.

Passet är återupptagbart. Standardläget hämtar bara enheter som saknas i
tabellen, så ett avbrutet pass fortsätter där det slutade:

    python cli/synka_skolenheter.py                  # fyll på
    python cli/synka_skolenheter.py --om             # hämta om allt
    python cli/synka_skolenheter.py --status Aktiv   # bara aktiva enheter

Enheter utan koordinat skrivs ändå, med `geom` som NULL. Annars hämtas de
om vid varje körning, och källan har dem inte.

Ingångspunkter:
    synka(bara_nya, status) -> dict[str, int]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_och_analys.geodata import synkstatus
from data_och_analys.infra import db, konfig
from data_och_analys.klienter import skolverket

logger = logging.getLogger(__name__)


def _koordinater(
    enhet: dict[str, Any],
) -> tuple[float | None, float | None, float | None, float | None, str | None]:
    """Plockar WGS84 och SWEREF ur besöksadressens GeoData.

    Bara besöksadressen bär koordinater; utdelnings- och leveransadress är
    ofta en box och skulle placera skolan på postkontoret.
    """
    geo = ((enhet.get("Besoksadress") or {}).get("GeoData") or {})

    def tal(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    besok = (enhet.get("Besoksadress") or {})
    postnr = str(besok.get("Postnr") or "").replace(" ", "").strip() or None
    return (
        tal(geo.get("Koordinat_WGS84_Lat")),
        tal(geo.get("Koordinat_WGS84_Lng")),
        tal(geo.get("Koordinat_SweRef_E")),
        tal(geo.get("Koordinat_SweRef_N")),
        postnr,
    )


def _skolformer(enhet: dict[str, Any]) -> str | None:
    """Skolformerna som en läsbar lista.

    Källan ger objekt med årskursflaggor och intern id; till en kartpunkt
    räcker benämningen, och den ska gå att filtrera på som text.
    """
    poster = enhet.get("Skolformer")
    if not isinstance(poster, list):
        return poster if isinstance(poster, str) else None
    namn = [
        str(x.get("Benamning") or x.get("type") or "").strip()
        for x in poster if isinstance(x, dict)
    ]
    return ", ".join(sorted({n for n in namn if n})) or None


def _huvudman(enhet: dict[str, Any]) -> str | None:
    """Huvudmannatypen — Kommun, Enskild, Region eller Stat.

    Typen är det man filtrerar på; huvudmannens namn hör till enheten och
    finns kvar i källan för den som vill slå upp den.
    """
    h = enhet.get("Huvudman")
    if isinstance(h, dict):
        return h.get("Typ") or h.get("Namn")
    return h if isinstance(h, str) else None


_UPSERT = """
INSERT INTO {p}skolenhet_punkt
    (kod, namn, kommun_kod, status, huvudman, skolformer,
     sweref_e, sweref_n, postnr, geom, lage_kalla, senast_synkad)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $11,
        CASE WHEN $9::double precision IS NULL OR $10::double precision IS NULL
             THEN NULL
             ELSE ST_SetSRID(ST_MakePoint($10, $9), 4326)
        END,
        CASE WHEN $9::double precision IS NULL THEN NULL ELSE 'kalla' END,
        NOW())
ON CONFLICT (kod) DO UPDATE SET
    namn = EXCLUDED.namn, kommun_kod = EXCLUDED.kommun_kod,
    status = EXCLUDED.status, huvudman = EXCLUDED.huvudman,
    skolformer = EXCLUDED.skolformer, sweref_e = EXCLUDED.sweref_e,
    sweref_n = EXCLUDED.sweref_n, postnr = EXCLUDED.postnr,
    geom = EXCLUDED.geom, lage_kalla = EXCLUDED.lage_kalla,
    senast_synkad = NOW()
"""


async def synka(bara_nya: bool = True, status: str | None = None) -> dict[str, int]:
    logger.info("Hämtar skolenhetsregistret…")
    paket = await skolverket.lista_skolenheter(status=status, per_sida=10000)
    rader = paket.get("datapunkter") or []
    logger.info("%d enheter i registret", len(rader))

    kanda: set[str] = set()
    if bara_nya:
        async with db.hamta_db() as a:
            kanda = {
                r["kod"] for r in
                await a.fetch(f"SELECT kod FROM {db.prefix()}skolenhet_punkt")
            }
        logger.info("%d redan speglade — hämtar %d",
                    len(kanda), len(rader) - len(kanda))

    hamtade = med_geom = utan_geom = fel = 0
    for n, rad in enumerate(rader, start=1):
        kod = str(rad.get("Skolenhetskod") or "")
        if not kod or (bara_nya and kod in kanda):
            continue
        try:
            enhet = await skolverket.hamta_skolenhet(kod)
        except Exception as e:  # noqa: BLE001
            # En enskild enhet som felar ska inte stoppa passet. Den saknas
            # då i tabellen och tas vid nästa påfyllnad.
            logger.warning("Skolenhet %s: %s", kod, e)
            fel += 1
            continue
        if not enhet:
            fel += 1
            continue

        lat, lon, e_, n_, postnr = _koordinater(enhet)
        kommun = (enhet.get("Kommun") or {}).get("Kommunkod")
        skolformer = _skolformer(enhet)
        huvudman = _huvudman(enhet)

        async with db.hamta_db() as a:
            await a.execute(
                _UPSERT.replace("{p}", db.prefix()),
                kod, enhet.get("Namn") or enhet.get("SkolaNamn"), kommun,
                enhet.get("Status"), huvudman,
                skolformer, e_, n_, lat, lon, postnr,
            )
        hamtade += 1
        if lat is not None and lon is not None:
            med_geom += 1
        else:
            utan_geom += 1
        if hamtade % 100 == 0:
            logger.info("  %d hämtade (%d med koordinat)", hamtade, med_geom)

    await synkstatus.notera_synk(
        "skolenhet_punkt", hamtade,
        f"{med_geom} med koordinat, {utan_geom} utan, {fel} fel",
    )
    return {
        "hamtade": hamtade, "med_geom": med_geom,
        "utan_geom": utan_geom, "fel": fel,
    }



def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--om", action="store_true",
                   help="Hämta om alla enheter, inte bara de som saknas")
    p.add_argument("--status", help="Bara enheter med denna status, t.ex. Aktiv")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    konfig.las_env()
    db.initiera_schema()
    r = asyncio.run(synka(bara_nya=not args.om, status=args.status))
    print(f"\n  {r['hamtade']} enheter speglade — {r['med_geom']} med koordinat, "
          f"{r['utan_geom']} utan, {r['fel']} fel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
