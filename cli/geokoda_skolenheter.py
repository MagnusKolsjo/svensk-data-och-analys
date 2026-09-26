# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Geokodar skolenheter som saknar koordinat hos Skolverket.

Drygt tusen enheter har besöksadress men ingen koordinat i källan — nästan
alla vilande. Adressen finns, så läget går att slå upp.

**Postnummerortens centrum duger inte.** Uppmätt mot de enheter som har en
riktig koordinat är medianfelet 1,1 km, men 90:e percentilen 8,1 km och
värsta fallet 17,9 km. Ett postnummerområde i glesbygd är flera mil, och en
punkt mitt i det hade lästs som skolans läge.

Nominatim slår i stället upp gatuadressen. Stickprov: elva träffar av tolv.

**Postboxar hoppas över.** "Box 11124, 10061 Stockholm" gav en träff på ett
företag i Stockholm — en box har ingen plats, och svaret blir ett godtyckligt
läge som ser lika trovärdigt ut som ett riktigt.

Uppslagningen görs av `klienter/nominatim.py`, som håller anropstakten,
fångar postboxar och bedömer träffens precision. Licens och villkor står
där. Passet tar drygt tjugo minuter och är återupptagbart.

Ingångspunkter:
    geokoda(bara_nya) -> dict[str, int]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_och_analys.geodata import synkstatus
from data_och_analys.infra import db, konfig
from data_och_analys.klienter import nominatim, skolverket

logger = logging.getLogger(__name__)

# Identiteten Nominatim ser. Kontaktvägen är ett krav i deras policy.
USER_AGENT = (
    "svensk-data-och-analys/Skolenhetsgeokodning "
    "(AGPL-3.0; https://github.com/)"
)


def _adress(enhet: dict) -> str | None:
    """Besöksadressen som sökbar sträng, eller None för box eller tomt.

    Bara besöksadressen duger. Utdelningsadressen är ofta just den box som
    inte går att placera.
    """
    b = (enhet or {}).get("Besoksadress") or {}
    return nominatim.bygg_adress(b.get("Adress"), b.get("Postnr"), b.get("Ort"))


async def geokoda(bara_nya: bool = True) -> dict[str, int]:
    # Prövade men misslyckade poster ska inte prövas om vid varje körning —
    # adressen är densamma och svaret blir detsamma. `--om` tar om dem.
    villkor = (
        "geom IS NULL AND coalesce(lage_kalla, '') NOT IN "
        "('saknar_gatuadress', 'endast_ortsniva', 'ej_hittad')"
    )
    if not bara_nya:
        villkor = "lage_kalla IS DISTINCT FROM 'kalla'"

    async with db.hamta_db() as anslutning:
        rader = [
            dict(r) for r in await anslutning.fetch(
                f"SELECT kod, namn FROM {db.prefix()}skolenhet_punkt "
                f"WHERE {villkor} ORDER BY kod"
            )
        ]
    logger.info("%d enheter att geokoda", len(rader))

    traff = box = missad = fel = ort = 0
    async with nominatim.Geokodare(USER_AGENT) as geokodare:
        for n, rad in enumerate(rader, start=1):
            try:
                enhet = await skolverket.hamta_skolenhet(rad["kod"])
            except Exception as e:  # noqa: BLE001
                logger.warning("%s: %s", rad["kod"], e)
                fel += 1
                continue

            adress = _adress(enhet or {})
            if adress is None:
                # Att bara räkna dem i en logg gör bristen osynlig nästa gång.
                # Märkningen gör populationen frågbar: en verksamhet med
                # enbart boxadress, trots att gatuadress krävs, är en
                # kvalitetsbrist i grundregistreringen — ett mätvärde om
                # registret, inte ett tomt fält hos oss.
                async with db.hamta_db() as anslutning:
                    await anslutning.execute(
                        f"UPDATE {db.prefix()}skolenhet_punkt "
                        f"SET lage_kalla = 'saknar_gatuadress' WHERE kod = $1",
                        rad["kod"],
                    )
                box += 1
                continue

            try:
                funnen = await geokodare.sla_upp(adress)
            except Exception as e:  # noqa: BLE001
                logger.warning("Nominatim %s: %s", rad["kod"], e)
                fel += 1
                continue

            if funnen is None:
                # Märk även missarna. Utan det går "prövad utan träff" inte
                # att skilja från "ännu inte prövad", och då går det inte att
                # svara på vad det är för adresser som inte hittas.
                async with db.hamta_db() as anslutning:
                    await anslutning.execute(
                        f"UPDATE {db.prefix()}skolenhet_punkt "
                        f"SET lage_kalla = 'ej_hittad' WHERE kod = $1",
                        rad["kod"],
                    )
                missad += 1
                continue

            if funnen.precision == "ort":
                # En ortsträff är samhällets mittpunkt, inte adressen. Felet
                # är av samma storlek som postnummerortens centrum, som
                # förkastades efter mätning. Hellre oplacerad än felplacerad.
                async with db.hamta_db() as anslutning:
                    await anslutning.execute(
                        f"UPDATE {db.prefix()}skolenhet_punkt "
                        f"SET lage_kalla = 'endast_ortsniva' WHERE kod = $1",
                        rad["kod"],
                    )
                ort += 1
                continue

            async with db.hamta_db() as anslutning:
                await anslutning.execute(
                    f"UPDATE {db.prefix()}skolenhet_punkt SET "
                    f"geom = ST_SetSRID(ST_MakePoint($2, $3), 4326), "
                    f"lage_kalla = 'nominatim', lage_precision = $4 "
                    f"WHERE kod = $1",
                    rad["kod"], funnen.longitud, funnen.latitud,
                    funnen.precision,
                )
            traff += 1
            if n % 50 == 0:
                logger.info("  %d/%d — %d träffar", n, len(rader), traff)

    await synkstatus.notera_synk(
        "skolenhet_geokodning", traff,
        f"{missad} utan träff, {box} postboxar, {ort} bara ortsnivå, {fel} fel",
    )
    return {"traff": traff, "missad": missad, "box": box,
            "ort": ort, "fel": fel}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--om", action="store_true",
                   help="Geokoda om även det som redan geokodats")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    konfig.las_env()
    r = asyncio.run(geokoda(bara_nya=not args.om))
    print(f"\n  {r['traff']} geokodade, {r['missad']} utan träff, "
          f"{r['box']} utan gatuadress, {r['ort']} bara ortsnivå, "
          f"{r['fel']} fel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
