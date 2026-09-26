-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
--
-- Geometrier för administrativa indelningar.
--
-- Geometrierna hålls i separata tabeller från de relationella indelnings-
-- tabellerna (kommun, lan, region, deso, regso, valdistrikt, valkrets).
-- Tre skäl:
--   1. Polygoner är stora (kommungeometrier ger ~5-10 MB, DeSO ~50-100 MB)
--      — onödigt att hämta vid varje vanlig query
--   2. Geometri-livscykel skiljer sig från indelnings-livscykel (en kommun
--      kan finnas i namn länge innan dess gränser uppdateras)
--   3. PostGIS-typer behöver inte SQLite-symmetri (geometri kräver PostGIS
--      eller SpatiaLite; SQLite-användare som vill ha geometri använder
--      separata GeoPackage-filer utanför detta schema)
--
-- Alla geometrier lagras i WGS84 (EPSG:4326). MULTIPOLYGON som typ
-- eftersom flera områden (öar, exklaver) ska kunna ingå i samma enhet.
-- GIST-index på varje geom-kolumn för effektiv ST_Contains, ST_Intersects.
--
-- KRAV: PostGIS-extensionen måste vara installerad. `CREATE EXTENSION IF
-- NOT EXISTS postgis;` körs separat (manuellt eller via en initmigration).
-- Schemafilen är ett no-op om PostGIS saknas — felmeddelandet blir tydligt.

-- ============================================================================
-- Administrativa indelningar
-- ============================================================================

CREATE TABLE IF NOT EXISTS kommun_geom (
    kod           TEXT PRIMARY KEY,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS gix_kommun_geom ON kommun_geom USING GIST (geom);


CREATE TABLE IF NOT EXISTS lan_geom (
    kod           TEXT PRIMARY KEY,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS gix_lan_geom ON lan_geom USING GIST (geom);


-- Region har samma geografiska utsträckning som län i den svenska
-- standardindelningen, men hålls som egen tabell för symmetri och så
-- att framtida avvikelser går att representera.
CREATE TABLE IF NOT EXISTS region_geom (
    kod           TEXT PRIMARY KEY,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS gix_region_geom ON region_geom USING GIST (geom);


-- ============================================================================
-- Sub-kommunenheter
-- ============================================================================

CREATE TABLE IF NOT EXISTS deso_geom (
    kod           TEXT NOT NULL,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kod, ar)
);
CREATE INDEX IF NOT EXISTS gix_deso_geom ON deso_geom USING GIST (geom);


CREATE TABLE IF NOT EXISTS regso_geom (
    kod           TEXT NOT NULL,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kod, ar)
);
CREATE INDEX IF NOT EXISTS gix_regso_geom ON regso_geom USING GIST (geom);


-- ============================================================================
-- Valgeografi
-- ============================================================================

CREATE TABLE IF NOT EXISTS valdistrikt_geom (
    kod           TEXT NOT NULL,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (kod, ar)
);
CREATE INDEX IF NOT EXISTS gix_valdistrikt_geom ON valdistrikt_geom USING GIST (geom);


-- Valkrets-geometrier per typ (riksdag/kommun/region) — samma typ-konvention
-- som i `valkrets`-tabellen så att joins blir direkta.
CREATE TABLE IF NOT EXISTS valkrets_geom (
    typ           TEXT NOT NULL,
    kod           TEXT NOT NULL,
    ar            INTEGER NOT NULL,
    geom          geometry(MULTIPOLYGON, 4326) NOT NULL,
    senast_synkad TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (typ, kod, ar)
);
CREATE INDEX IF NOT EXISTS gix_valkrets_geom ON valkrets_geom USING GIST (geom);

-- ============================================================================
-- Punktlager
-- ============================================================================
-- Skolenheter är punkter, inte ytor. De hör hemma i ett punktlager och aldrig
-- i en choropleth — en skola har ingen utbredning att färglägga.
--
-- Koordinaterna finns bara i Skolverkets detalj-API, ett anrop per enhet.
-- Tabellen är därför en spegel som fylls av cli/synka_skolenheter.py och
-- läses utan nätanrop. `senast_synkad` gör passet återupptagbart: ett
-- avbrutet pass fortsätter där det slutade i stället för att börja om.
--
-- SWEREF-koordinaterna bevaras vid sidan av punkten. Källan levererar båda,
-- och den som exporterar till svenska kartunderlag vill ha originalet i
-- EPSG:3006 snarare än en tillbakatransformering.

CREATE TABLE IF NOT EXISTS skolenhet_punkt (
    kod            TEXT PRIMARY KEY,          -- Skolenhetskod
    namn           TEXT,
    kommun_kod     TEXT,                      -- SCB 4-siffrig, joinbar mot kommun
    status         TEXT,                      -- Aktiv / Vilande / Planerad
    huvudman       TEXT,
    skolformer     TEXT,                      -- kommaseparerad lista ur källan
    postnr         TEXT,                      -- ur besöksadressen
    sweref_e       DOUBLE PRECISION,
    sweref_n       DOUBLE PRECISION,
    geom           geometry(POINT, 4326),     -- NULL när läget är okänt
    -- Varifrån punkten kommer. 'kalla' är Skolverkets egen koordinat.
    -- 'postnummer' är postnummerortens centrum, använt för de drygt tusen
    -- enheter där källan har adress men ingen koordinat — nästan alla
    -- vilande. En sådan punkt duger på kommun- och riksnivå men är fel på
    -- gatunivå, och måste därför gå att skilja ut.
    lage_kalla     TEXT,
    senast_synkad  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS gix_skolenhet_punkt ON skolenhet_punkt USING GIST (geom);
CREATE INDEX IF NOT EXISTS ix_skolenhet_punkt_kommun ON skolenhet_punkt (kommun_kod);
CREATE INDEX IF NOT EXISTS ix_skolenhet_punkt_status ON skolenhet_punkt (status);

ALTER TABLE skolenhet_punkt ADD COLUMN IF NOT EXISTS postnr TEXT;
ALTER TABLE skolenhet_punkt ADD COLUMN IF NOT EXISTS lage_kalla TEXT;
-- Nominatims matchningstyp: 'school' är byggnaden, en vägtyp är gatunivå.
-- Skillnaden måste synas — en gatuträff duger för en kommunkarta men inte
-- för att peka ut en skolgård.
ALTER TABLE skolenhet_punkt ADD COLUMN IF NOT EXISTS lage_precision TEXT;
