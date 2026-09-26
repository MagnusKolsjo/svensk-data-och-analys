# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Kontrollerar om källorna publicerat något nyare än vad vi hämtat.

`cli/synka_geo_fasetter.py` och `geo_synkstatus` svarar på hur gammal vår
kopia är. Det räcker inte för de nio händelsestyrda dataseten — de blir
inaktuella när myndigheten publicerar, inte när tiden går.

Den här kontrollen frågar källan i stället: ETag, Last-Modified och storlek
ur ett HEAD-anrop. Ändras någon av dem har filen bytts ut.

Den synkar aldrig något. Den säger vad som ändrats och vilket verktyg som
hämtar in det — beslutet är ditt.

    python cli/bevaka_kallor.py
    python cli/bevaka_kallor.py --dataset kommun_folkmangd

Ingångspunkter:
    main() -> int
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_och_analys.geodata import bevakning
from data_och_analys.infra import db, konfig


async def _kor(dataset: str | None) -> int:
    rader = await bevakning.kontrollera(dataset)
    if not rader:
        print("  Inga bevakade källor. Kör en synk med en URL först.")
        return 0

    andrade = [r for r in rader if r["lage"] == "ändrad hos källan"]
    for r in rader:
        markor = "!" if r["lage"] == "ändrad hos källan" else " "
        print(f" {markor} {r['dataset']:<26} {r['lage']}")
        if r["lage"] != "oförändrad":
            print(f"     {r['rad']}")
    print()
    print(f"  {len(andrade)} av {len(rader)} källor har publicerat om.")
    # Utgångskod 1 när något ändrats, så att en schemaläggare kan larma.
    return 1 if andrade else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dataset", help="Bara det här datasetet")
    args = p.parse_args()
    konfig.las_env()
    db.initiera_schema()
    return asyncio.run(_kor(args.dataset))


if __name__ == "__main__":
    sys.exit(main())
