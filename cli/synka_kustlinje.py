# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Hämtar EEA:s kustlinje och bygger de landklippta geometrivyerna.

Kör en gång vid installation, och om igen när DeSO-geometrin synkats om.
Kustlinjen är från 2015 och revideras inte, så nedladdningen hoppas över
om filerna redan ligger i cachen.

Kedjan är:

    EEA shapefile  ->  kustlinje_geom     (landmask, CC-BY 4.0)
    mv_kommun_geom  ∩  kustlinje_geom  ->  mv_kommun_land
    mv_kommun_land  unionerat per länskod ->  mv_lan_land

`mv_kommun_geom` byggs i sin tur av SCB:s DeSO (CC0). Resultatet ärver
båda licenserna: attribution till EEA krävs, i övrigt fritt — inklusive
kommersiell användning och vidaredistribution, vilket är hela skälet att
källan är EEA och inte Eurostat GISCO.

Snittet tar drygt en minut. Det är därför det ligger här och inte i
`initiera_schema` — en MCP-server som blockerar en minut vid uppstart
rapporteras som trasig av klienten.

Körs så här:

    .venv/bin/python cli/synka_kustlinje.py

Ingångspunkter:
    synka() -> int
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJEKT_ROT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJEKT_ROT))

from data_och_analys.infra import db, framsteg, konfig  # noqa: E402
from data_och_analys.katalog_klienter import eea_sdi  # noqa: E402

CACHE = PROJEKT_ROT / ".cache" / "eea"

# Rejält tilltagen ram kring Sverige. Kustlinjefilen täcker hela Europa;
# att klippa mot bbox före databasen håller tabellen på 44 000 polygoner
# i stället för 71 500, utan att riskera att kapa någon svensk ö.
SVERIGE_BBOX = (10.0, 54.5, 25.5, 69.5)

# Facit att stämma av mot. Sveriges yta inklusive inlandsvatten är
# ~447 000 km²; avviker resultatet grovt har något gått fel i snittet.
FORVANTAD_YTA_KM2 = 447_000
TOLERANS = 0.05


async def _rapportera(gjort: int, av: int | None, meddelande: str) -> None:
    print(f"  {meddelande}")


async def synka() -> int:
    konfig.las_env()
    if not db.ar_postgres():
        print("FEL: kräver Postgres med PostGIS")
        return 1

    print("1/4  Hämtar EEA:s kustlinje")
    shp = await eea_sdi.hamta_kustlinje(
        CACHE, rapportera=framsteg.strypt(_rapportera)
    )

    print("2/4  Läser och klipper till Sveriges bbox")
    import geopandas as gpd
    import shapely.geometry as sgeom

    europa = gpd.read_file(shp)
    ram = gpd.GeoDataFrame(
        geometry=[sgeom.box(*SVERIGE_BBOX)], crs=4326
    ).to_crs(europa.crs)
    sverige = gpd.clip(europa, ram).to_crs(4326)
    sverige = sverige[~sverige.geometry.is_empty & sverige.geometry.notna()]
    print(f"     {len(sverige):,} polygoner")

    print("3/4  Skriver kustlinje_geom")
    async with db.hamta_db() as anslutning:
        await anslutning.execute("DROP TABLE IF EXISTS kustlinje_geom CASCADE")
        await anslutning.execute(
            """CREATE TABLE kustlinje_geom (
                   id    serial PRIMARY KEY,
                   kalla text NOT NULL,
                   geom  geometry(MULTIPOLYGON, 4326) NOT NULL
               )"""
        )
        await anslutning.executemany(
            "INSERT INTO kustlinje_geom (kalla, geom) VALUES "
            "($1, ST_Multi(ST_GeomFromWKB(decode($2, 'hex'), 4326)))",
            [(eea_sdi.KUSTLINJE.attribution, g.wkb_hex)
             for g in sverige.geometry],
        )
        await anslutning.execute(
            "CREATE INDEX kustlinje_geom_gix ON kustlinje_geom USING GIST (geom)"
        )
        await anslutning.execute("ANALYZE kustlinje_geom")

        print("4/4  Bygger landklippta vyer (tar en dryg minut)")
        t0 = time.monotonic()
        sql = (PROJEKT_ROT / "db" / "karta_geometri.sql").read_text()
        # Vyerna är CREATE ... IF NOT EXISTS; släpp dem först så att en
        # omsynk faktiskt bygger om dem mot den nya kustlinjen.
        await anslutning.execute(
            "DROP MATERIALIZED VIEW IF EXISTS mv_lan_land CASCADE;"
            "DROP MATERIALIZED VIEW IF EXISTS mv_kommun_land CASCADE"
        )
        await anslutning.execute(sql)
        print(f"     klart på {time.monotonic() - t0:.0f} s")

        rad = await anslutning.fetchrow(
            "SELECT count(*) AS n, sum(ST_Area(geom::geography)) / 1e6 AS km2 "
            "FROM mv_kommun_land"
        )

    antal, yta = rad["n"], float(rad["km2"] or 0)
    avvikelse = abs(yta / FORVANTAD_YTA_KM2 - 1)
    print(f"\nmv_kommun_land: {antal} kommuner, {yta:,.0f} km²")
    print(f"  facit ~{FORVANTAD_YTA_KM2:,} km² — avvikelse {avvikelse * 100:.1f} %")

    if antal != 290 or avvikelse > TOLERANS:
        print("\nFEL: resultatet ser inte rimligt ut. Kontrollera att "
              "mv_kommun_geom är fylld och att DeSO-synken körts.")
        return 1
    print("\nKlart. Attribution som måste följa med publicerade kartor:")
    print(f"  {eea_sdi.KUSTLINJE.attribution}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(synka()))
