# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Undersöker vilka poster i sökindexet som är geografiskt indelade.

Utan det här passet vet sökningen inte vilka träffar som går att lägga på
karta, och kartarbetet blir trial and error: välj en träff, klicka visa,
få veta först då att tabellen saknar regiondimension.

Passet är avsiktligt inkrementellt. Standardläget undersöker bara poster
som saknar geo-block, vilket gör det till påfyllnadsverktyget när nya
dataserier dykt upp i indexet:

    python cli/synka_geo_fasetter.py                 # alla källor, bara nya
    python cli/synka_geo_fasetter.py --kalla scb     # en källa
    python cli/synka_geo_fasetter.py --kalla scb --om   # gör om från början

Det första passet är långt — det är rate-limiten hos myndigheterna som
sätter takten, inte vår kod. Kör en källa i taget i bakgrunden när det är
många; resultatet skrivs löpande och ett avbrutet pass tappar inget som
hunnit skrivas.

Ingångspunkter:
    synka(kallor, bara_nya) -> dict[str, dict[str, int]]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_och_analys.infra import db, konfig
from data_och_analys.sok import geo_fasetter
from data_och_analys.sok.upplosare import INGEN_GEOGRAFI, UPPLOSARE, upplos

logger = logging.getLogger(__name__)

KANDA_KALLOR = sorted(set(UPPLOSARE) | INGEN_GEOGRAFI)


async def _rapportera(gjort: int, av: int | None, meddelande: str) -> None:
    logger.info("  %s", meddelande)


async def synka_kalla(kalla: str, bara_nya: bool = True) -> dict[str, int]:
    """Undersöker en källa och skriver resultatet. Returnerar en räkning."""
    async with db.hamta_db() as anslutning:
        poster = await geo_fasetter.poster(anslutning, kalla, bara_nya=bara_nya)

    if not poster:
        logger.info("%s: inget att undersöka", kalla)
        return {"undersokta": 0, "geografiska": 0, "utan_geografi": 0}

    logger.info("%s: %d poster att undersöka", kalla, len(poster))
    resultat = await upplos(kalla, poster, _rapportera)

    med = utan = 0
    async with db.hamta_db() as anslutning:
        for post_id, geo in resultat.items():
            await geo_fasetter.skriv_geo(anslutning, post_id, geo)
            if geo:
                med += 1
            else:
                utan += 1

    logger.info(
        "%s: %d geografiska, %d utan geografi (%d orörda)",
        kalla, med, utan, len(poster) - len(resultat),
    )
    return {"undersokta": len(resultat), "geografiska": med, "utan_geografi": utan}


async def synka(
    kallor: list[str] | None = None, bara_nya: bool = True
) -> dict[str, dict[str, int]]:
    valda = kallor or KANDA_KALLOR
    okanda = [k for k in valda if k not in KANDA_KALLOR]
    if okanda:
        raise SystemExit(
            f"okänd källa: {', '.join(okanda)} — kända: {', '.join(KANDA_KALLOR)}"
        )
    summa: dict[str, dict[str, int]] = {}
    for kalla in valda:
        try:
            summa[kalla] = await synka_kalla(kalla, bara_nya=bara_nya)
        except Exception as fel:  # noqa: BLE001
            # En källa som ligger nere ska inte stoppa de övriga. Passet är
            # långt och inkrementellt — det som skrivits står kvar, och nästa
            # körning tar bara det som saknas.
            logger.error("%s: passet avbröts (%s)", kalla, fel)
            summa[kalla] = {"fel": str(fel)}
    return summa


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--kalla", action="append", dest="kallor",
                   help=f"Källa att undersöka; upprepa för flera. Kända: "
                        f"{', '.join(KANDA_KALLOR)}")
    p.add_argument("--om", action="store_true",
                   help="Gör om från början i stället för att bara ta nya")
    p.add_argument("--lista", action="store_true",
                   help="Visa vilka källor som har en upplösare och sluta")
    args = p.parse_args()

    if args.lista:
        for k in KANDA_KALLOR:
            print(f"  {k:<22} {'aldrig geografisk' if k in INGEN_GEOGRAFI else 'upplösare'}")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    konfig.las_env()
    summa = asyncio.run(synka(args.kallor, bara_nya=not args.om))

    print()
    for kalla, r in summa.items():
        if "fel" in r:
            print(f"  {kalla:<22} FEL: {r['fel'][:60]}")
        else:
            print(f"  {kalla:<22} {r['geografiska']:>6} geografiska "
                  f"av {r['undersokta']} undersökta")
    return 0


if __name__ == "__main__":
    sys.exit(main())
