# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Geometri-parsare för administrativa indelningar.

Skiljd från `indelningar.py` av flera skäl:

    1. Polygoner är stora (MB istället för KB) — hör inte hemma i
       samma kodvägar som lättviktig metadata.
    2. Geometri-livscykel skiljer sig från indelnings-livscykel.
       En kommun kan finnas i namn länge innan dess gränser revideras.
    3. PostGIS-beroende — modulen kräver PostGIS-extension och passar
       inte symmetrin med SQLite på samma sätt som relationella tabeller
       gör. Schema-init lägger upp tabellerna men de fylls bara via
       Postgres-backend i nuläget.

Källor (status så här långt):

    val.se valdistrikt    GeoJSON i zip — direktlänk, EPSG:3006 (SWEREF99 TM)
    SCB DeSO/RegSO        WFS-tjänst (kommande — kräver WFS-klient)
    Kommun/Län/Region     Lantmäteriet öppna data (kommande)
    Valkretsar            härleds från valdistriktsunion eller från val.se

Geometri-import sker i två steg:
    1. Hämta källan (geojson, gpkg eller WFS-svar)
    2. INSERT via PostGIS med `ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(...),
       <källans SRID>), 4326)` så att alla geometrier landar i WGS84.

Ingångspunkter:
    synka_valdistrikt_geom(url=None, ar=2026, kalla_crs=3006)
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from typing import Iterator

import httpx
import ijson

from data_och_analys.infra import db, framsteg
from data_och_analys.katalog_klienter import ogc_api_features, wfs

logger = logging.getLogger(__name__)

# Följer med varje publicerad karta. Kommunindelningen kommer ur SCB:s DeSO
# (CC0, inget krav) och landmasken ur EEA:s kustlinje (CC-BY, attribution
# krävs). Strängen bor här därför att den beskriver geometrins ursprung, och
# konsumeras av både api/routers/karta.py och kartwidgeten i geo_mcp.py.
LICENS_ATTRIBUTION = (
    "Kommunindelning © SCB (CC0 1.0). "
    "Landmask © European Environment Agency (CC-BY 4.0)."
)


# ============================================================================
# Gemensamma hjälpfunktioner
# ============================================================================


USER_AGENT = (
    "svensk-data-och-analys/GeometrierClient (+https://github.com/)"
)
_LANG_TIMEOUT = httpx.Timeout(600.0, connect=30.0)


async def _hamta_bytes(url: str) -> bytes:
    """GET med browser-User-Agent och lång timeout för stora geo-filer."""
    async with httpx.AsyncClient(
        timeout=_LANG_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url)
    svar.raise_for_status()
    return svar.content


def _extrahera_geojson_ur_zip(zip_bytes: bytes) -> bytes:
    """Plockar ut första .geojson-filen ur en zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        namn = next((n for n in z.namelist() if n.endswith(".geojson")), None)
        if namn is None:
            raise ValueError("ingen .geojson-fil hittades i zip:en")
        return z.read(namn)


def _iter_features(geojson_bytes: bytes) -> Iterator[dict]:
    """Strömmar features ur en GeoJSON-bytes-buffert.

    ijson är minneseffektiv — för en 90+ MB geojson är full json.loads()
    onödigt tung. ijson läser feature för feature så vi kan upserta direkt.

    `use_float=True` säger till ijson att returnera koordinater som
    float istället för Decimal — annars klagar json.dumps senare.
    """
    with io.BytesIO(geojson_bytes) as buf:
        for feat in ijson.items(buf, "features.item", use_float=True):
            yield feat


# ============================================================================
# val.se — valdistrikt-geometri
# ============================================================================


async def synka_valdistrikt_geom(
    url: str | None = None,
    ar: int = 2026,
    kalla_crs: int = 3006,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Synkar valdistrikt-geometri från val.se:s riket-zip.

    Filen är en zip med en enskild .geojson — MultiPolygon-features i
    SWEREF99 TM (EPSG:3006). PostGIS reprojicerar till WGS84 (4326) vid
    INSERT. URL ändras per valår; sätt explicit eller via
    `DOA_VALDISTRIKT_GEOM_URL` i `.env`.

    Bara Postgres-backend stöds — geometri-tabellerna kräver PostGIS.
    SQLite-användare som vill ha geometri använder separat GeoPackage-fil.

    Returnerar antal skrivna distrikt.
    """
    if not db.ar_postgres():
        raise RuntimeError(
            "geometri-synk kräver Postgres-backend med PostGIS — "
            "SQLite stöder inte denna modul"
        )

    if url is None:
        url = os.getenv("DOA_VALDISTRIKT_GEOM_URL", "").strip()
    if not url:
        raise ValueError(
            "ingen URL angiven — skicka `url` eller sätt "
            "DOA_VALDISTRIKT_GEOM_URL i .env"
        )

    logger.info("Hämtar valdistrikt-geometri %d från %s", ar, url)
    await rapportera(0, None, f"Hämtar valdistriktsfilen för {ar}")
    zip_bytes = await _hamta_bytes(url)
    geojson_bytes = _extrahera_geojson_ur_zip(zip_bytes)

    # Bygg upp rader: (kod, geojson_str) — geometrin skickas som JSON-sträng
    # och PostGIS gör reprojeceringen vid INSERT.
    # Totalen är okänd här: features strömmas ur filen, så antalet är inte
    # känt förrän strömmen tagit slut.
    rader: list[tuple[str, int, str]] = []
    for feat in _iter_features(geojson_bytes):
        props = feat.get("properties", {})
        kod = (props.get("Valdistriktskod") or "").strip()
        geom = feat.get("geometry")
        if not kod or geom is None:
            continue
        rader.append((kod, ar, json.dumps(geom)))
        await rapportera(len(rader), None, f"Läste {len(rader)} valdistrikt")

    if not rader:
        logger.warning("Inga features med Valdistriktskod hittades")
        return 0

    # PostGIS gör reprojeceringen vid INSERT. ST_Multi säkerställer att
    # input som Polygon också blir MULTIPOLYGON enligt schemat.
    sql = (
        f"INSERT INTO {db.prefix()}valdistrikt_geom (kod, ar, geom, senast_synkad) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, "
        f"        ST_Multi(ST_Transform(ST_SetSRID("
        f"            ST_GeomFromGeoJSON({db.ph(3)}), {int(kalla_crs)}), 4326)), "
        f"        CURRENT_TIMESTAMP) "
        f"ON CONFLICT (kod, ar) DO UPDATE SET "
        f"geom = EXCLUDED.geom, senast_synkad = CURRENT_TIMESTAMP"
    )

    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, rader)

    logger.info(
        "Synkade %d valdistrikt-geometrier för år %d", len(rader), ar
    )
    return len(rader)


# ============================================================================
# Lantmäteriet OGC API Features — administrativa indelningar
# ============================================================================


async def _synka_oafeat_admin_geom(
    collection_id: str,
    mal_tabell: str,
    kod_falt: str,
    ar: int,
    kalla_crs: int = 3006,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Generell synk från Lantmäteriets administrativa-indelning-OAFeat
    till en lokal *_geom-tabell.

    `collection_id` är OAFeat-collectionens id (t.ex. `kommuner-2026`,
    `lan-2026`, `rike-2026`). `mal_tabell` är destinations-tabellnamnet
    (utan db.prefix()). `kod_falt` är property-fältet i features som
    håller den nyckel vi använder som primärnyckel (t.ex.
    `kommunkod`, `lanskod`).

    Strömmar features paginerat via OAFeat-klienten. PostGIS reprojicerar
    till WGS84 vid INSERT. Idempotent.
    """
    if not db.ar_postgres():
        raise RuntimeError(
            "geometri-synk kräver Postgres-backend med PostGIS"
        )

    import json

    sql = (
        f"INSERT INTO {db.prefix()}{mal_tabell} (kod, ar, geom, senast_synkad) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, "
        f"        ST_Multi(ST_Transform(ST_SetSRID("
        f"            ST_GeomFromGeoJSON({db.ph(3)}), {int(kalla_crs)}), 4326)), "
        f"        CURRENT_TIMESTAMP) "
    )
    if mal_tabell.endswith("_geom") and mal_tabell in {
        "kommun_geom", "lan_geom", "region_geom"
    }:
        # PK = kod (utan ar)
        sql += (
            f"ON CONFLICT (kod) DO UPDATE SET "
            f"ar = EXCLUDED.ar, geom = EXCLUDED.geom, "
            f"senast_synkad = CURRENT_TIMESTAMP"
        )
    else:
        sql += (
            f"ON CONFLICT (kod, ar) DO UPDATE SET "
            f"geom = EXCLUDED.geom, senast_synkad = CURRENT_TIMESTAMP"
        )

    rader: list[tuple[str, int, str]] = []
    async for feat in ogc_api_features.stroma_alla_items(
        "lantmateriet_admin", collection_id
    ):
        props = feat.get("properties", {}) or {}
        kod = str(props.get(kod_falt, "") or "").strip()
        geom = feat.get("geometry")
        if not kod or geom is None:
            continue
        rader.append((kod, ar, json.dumps(geom)))
        await rapportera(len(rader), None, f"Läste {len(rader)} objekt")

    if not rader:
        logger.warning(
            "Inga features med fält %s hittades i %s",
            kod_falt, collection_id,
        )
        return 0

    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, rader)

    logger.info(
        "Synkade %d %s från %s (år %d)",
        len(rader), mal_tabell, collection_id, ar,
    )
    return len(rader)


async def synka_kommun_geom(
    collection_id: str = "kommuner-2026", ar: int = 2026,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Synkar kommungeometrier från Lantmäteriets OAFeat.

    Kräver `LANTMATERIET_API_NYCKEL` i `.env`. Hämtar features paginerat
    från Lantmäteriets administrativa-indelning-API. SCB-kommunkoden
    finns i feature-properties under nyckeln `kommunkod`.

    Default-collectionen är `kommuner-2026` (2026 års snapshot).
    """
    return await _synka_oafeat_admin_geom(
        collection_id=collection_id,
        mal_tabell="kommun_geom",
        kod_falt="kommunkod",
        ar=ar,
        rapportera=rapportera,
    )


async def synka_lan_geom(
    collection_id: str = "lan-2026", ar: int = 2026,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Synkar länsgeometrier från Lantmäteriets OAFeat.

    Default-collectionen är `lan-2026`. SCB-länskoden finns i
    feature-properties under nyckeln `lanskod`.
    """
    return await _synka_oafeat_admin_geom(
        collection_id=collection_id,
        mal_tabell="lan_geom",
        kod_falt="lanskod",
        ar=ar,
        rapportera=rapportera,
    )


async def synka_region_geom(
    collection_id: str = "lan-2026", ar: int = 2026,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Synkar regiongeometrier från Lantmäteriets län-collection.

    Region-koden i den svenska standardindelningen är samma som länkoden,
    så vi använder Lantmäteriets län-data men sätter `region_kod = lanskod`.
    En kopia hålls i `region_geom` av symmetriskäl — framtida avvikelser
    går då att representera utan schemaändring.
    """
    return await _synka_oafeat_admin_geom(
        collection_id=collection_id,
        mal_tabell="region_geom",
        kod_falt="lanskod",
        ar=ar,
        rapportera=rapportera,
    )


async def _synka_wfs_geom(
    instans: str,
    type_name: str,
    mal_tabell: str,
    kod_falt: str,
    ar: int,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Generell synk från en WFS-tjänst till en lokal *_geom-tabell.

    Hämtar features med GeoJSON-output i WGS84 (servern reprojicerar)
    och importerar via PostGIS `ST_GeomFromGeoJSON`. Inget Python-side
    CRS-arbete krävs eftersom WFS gör reprojiceringen.

    `kod_falt` är det property-fält som blir tabellens primärnyckel
    (t.ex. `desokod`, `regsokod`).
    """
    if not db.ar_postgres():
        raise RuntimeError("geometri-synk kräver Postgres-backend med PostGIS")

    sql = (
        f"INSERT INTO {db.prefix()}{mal_tabell} (kod, ar, geom, senast_synkad) "
        f"VALUES ({db.ph(1)}, {db.ph(2)}, "
        f"        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON({db.ph(3)}), 4326)), "
        f"        CURRENT_TIMESTAMP) "
        f"ON CONFLICT (kod, ar) DO UPDATE SET "
        f"geom = EXCLUDED.geom, senast_synkad = CURRENT_TIMESTAMP"
    )

    rader: list[tuple[str, int, str]] = []
    async for feat in wfs.stroma_alla_features(
        instans=instans,
        type_name=type_name,
        sida_storlek=1000,
        srs="EPSG:4326",
    ):
        props = feat.get("properties", {}) or {}
        kod = str(props.get(kod_falt, "") or "").strip()
        geom = feat.get("geometry")
        if not kod or geom is None:
            continue
        rader.append((kod, ar, json.dumps(geom)))
        await rapportera(len(rader), None, f"Läste {len(rader)} objekt")

    if not rader:
        logger.warning("Inga features med fält %s i %s", kod_falt, type_name)
        return 0

    async with db.hamta_db() as anslutning:
        await anslutning.executemany(sql, rader)

    logger.info(
        "Synkade %d %s från WFS %s/%s (år %d)",
        len(rader), mal_tabell, instans, type_name, ar,
    )
    return len(rader)


async def synka_deso_geom_via_wfs(
    type_name: str = "stat:DeSO_2025",
    ar: int = 2025,
    instans: str = "scb_stat",
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Synkar DeSO-geometrier från SCB:s WFS-tjänst.

    Default-lager är `stat:DeSO_2025` som matchar koderna i koppling-
    deso2025-regso2025-filen. För historiska analyser finns även
    `stat:DeSO_2018`.
    """
    return await _synka_wfs_geom(
        instans=instans,
        type_name=type_name,
        mal_tabell="deso_geom",
        kod_falt="desokod",
        ar=ar,
        rapportera=rapportera,
    )


async def synka_regso_geom_via_wfs(
    type_name: str = "stat:RegSO_2025",
    ar: int = 2025,
    instans: str = "scb_stat",
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> int:
    """Synkar RegSO-geometrier från SCB:s WFS-tjänst.

    Default-lager är `stat:RegSO_2025`. Tidigare versioner: `stat:RegSO_2020`.
    """
    return await _synka_wfs_geom(
        instans=instans,
        type_name=type_name,
        mal_tabell="regso_geom",
        kod_falt="regsokod",
        ar=ar,
        rapportera=rapportera,
    )


async def bygg_valkrets_geom_fran_distrikt(ar: int = 2026) -> dict[str, int]:
    """Bygger valkrets-polygoner genom att unionera valdistrikt-polygoner.

    Tre typer skapas på samma gång:
        - riksdag (~29 polygoner per valår)
        - kommun  (~314 polygoner per valår, en eller flera per kommun)
        - region  (~62 polygoner per valår, en eller flera per region)

    Förutsätter att `valdistrikt_geom` är populerad
    (`synka_valdistrikt_geom`) och att `valdistrikt`-tabellen har koder
    för alla tre valkretstyper. PostGIS gör allt arbete via
    `ST_Multi(ST_Union(geom))`; vi triggar bara SQL:en.

    Returnerar antal byggda polygoner per typ.
    """
    if not db.ar_postgres():
        raise RuntimeError(
            "geometri-synk kräver Postgres-backend med PostGIS"
        )

    # `typ` och `kod_falt` är hardkodade konstanter — säkra att inlina
    # i SQL utan platshållare. Det krävs eftersom kolumn-/värdesplatser
    # inte kan parametriseras.
    typer = [
        ("riksdag", "riksdagsvalkrets_kod"),
        ("kommun", "kommunvalkrets_kod"),
        ("region", "regionvalkrets_kod"),
    ]

    resultat: dict[str, int] = {}
    pref = db.prefix()

    async with db.hamta_db() as anslutning:
        for typ, kod_falt in typer:
            sql = (
                f"INSERT INTO {pref}valkrets_geom "
                f"  (typ, kod, ar, geom, senast_synkad) "
                f"SELECT '{typ}', vd.{kod_falt}, vd.ar, "
                f"       ST_Multi(ST_Union(vg.geom)), CURRENT_TIMESTAMP "
                f"FROM {pref}valdistrikt_geom vg "
                f"JOIN {pref}valdistrikt vd "
                f"  ON vd.kod = vg.kod AND vd.ar = vg.ar "
                f"WHERE vd.{kod_falt} IS NOT NULL "
                f"  AND vd.ar = {db.ph(1)} "
                f"GROUP BY vd.{kod_falt}, vd.ar "
                f"ON CONFLICT (typ, kod, ar) DO UPDATE SET "
                f"  geom = EXCLUDED.geom, "
                f"  senast_synkad = CURRENT_TIMESTAMP"
            )
            await anslutning.execute(sql, ar)
            antal = await anslutning.fetchval(
                f"SELECT COUNT(*) FROM {pref}valkrets_geom "
                f"WHERE typ = {db.ph(1)} AND ar = {db.ph(2)}",
                typ, ar,
            )
            resultat[typ] = antal
            logger.info(
                "Byggde %d %s-valkretsgeometrier för år %d", antal, typ, ar
            )

    return resultat
