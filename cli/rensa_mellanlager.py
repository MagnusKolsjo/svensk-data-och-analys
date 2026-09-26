# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Rensar mellanlagret för filbaserade källor.

Data som hämtats ur Excel- eller CSV-filer läggs i `fil_mellanlager` för att
slippa tolka om filen vid varje fråga. Lagret har hållbarhet — standard 30
dagar, styrt av `DOA_MELLANLAGRING_DAGAR` — men inget tar bort det som
passerat om ingen kör rensningen. Utan den växer lagret tyst med varje ny
årgång.

Kör efter ett jobb som hämtat mycket, eller schemalagt:

    python cli/rensa_mellanlager.py                    # allt som passerat
    python cli/rensa_mellanlager.py --status           # visa vad som ligger
    python cli/rensa_mellanlager.py --kalla migrationsverket
    python cli/rensa_mellanlager.py --allt             # töm oavsett ålder

Ingångspunkter:
    main() -> int
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_och_analys.infra import db, konfig, mellanlager


async def _kor(args) -> int:
    if args.status:
        rader = await mellanlager.status()
        if not rader:
            print("  Mellanlagret är tomt.")
            return 0
        print(f"  Hållbarhet: {mellanlager.hallbarhet_dagar()} dagar\n")
        print(f"  {'KÄLLA':<18} {'DATASET':<28} {'BLAD':<24} {'RADER':>7} {'DAGAR':>6}")
        for r in rader:
            rader_ = r["antal_rader"] if r["antal_rader"] is not None else f"{r['byte'] // 1024} kB"
            print(f"  {r['kalla']:<18} {r['dataset'][-28:]:<28} {r['blad'][:24]:<24} "
                  f"{rader_:>7} {r['alder_dagar']:>6}")
        return 0

    antal = await mellanlager.rensa(
        kalla=args.kalla, aldre_an_dagar=0 if args.allt else None
    )
    vad = "allt" if args.allt else f"äldre än {mellanlager.hallbarhet_dagar()} dagar"
    print(f"  {antal} poster borttagna ({vad}).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--status", action="store_true", help="Visa vad som ligger i lagret")
    p.add_argument("--kalla", help="Bara den här källan")
    p.add_argument("--allt", action="store_true",
                   help="Töm oavsett ålder — nästa fråga hämtar om från källan")
    args = p.parse_args()
    konfig.las_env()
    db.initiera_schema()
    return asyncio.run(_kor(args))


if __name__ == "__main__":
    sys.exit(main())
