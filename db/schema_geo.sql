-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
--
-- Geografiska indelningar — schema som båda backenderna delar.
--
-- Skrivet i ANSI SQL-snitt: TEXT istället för VARCHAR, INTEGER istället för
-- INT4/INT8, INDEX:er som separata CREATE INDEX. Det fungerar oförändrat i
-- både PostgreSQL och SQLite — inga ALTER-trick eller specialtyper behövs
-- för det här schemat.
--
-- Idempotent: alla CREATE använder IF NOT EXISTS så att initieringen kan
-- köras vid varje uppstart utan att kollidera med befintliga tabeller.
--
-- Datamodellens kärna:
--
--   kommun_klassificering är en universell mappning kommun → grupp inom ett
--   namngivet system, stämplad med år. En kommun har en rad per (system, år).
--   Det gör en compositionell fråga som "DeSO-centralorter i kommuner som SKR
--   klassar som C9 OCH som Tillväxtverket klassar som landsbygd" till en ren
--   join över två rader i samma tabell.
--
--   Sub-kommunenheter (DeSO, RegSO, valdistrikt) har egen identitet men bär
--   kommun_kod för att kunna joinas via kommun mot klassificeringar.
--
--   Postnummerområden är ortogonala — de kan korsa kommungränser. Därför
--   både en "dominant kommun" på huvudtabellen och en många-till-många-
--   tabell med andelar för punkt-i-polygon-uppslag.

-- ============================================================================
-- Klassificeringssystem (metatabell)
-- ============================================================================

CREATE TABLE IF NOT EXISTS klassificeringssystem (
    system        TEXT PRIMARY KEY,
    namn          TEXT NOT NULL,
    kalla         TEXT,
    beskrivning   TEXT,
    senast_synkad TIMESTAMP
);


-- ============================================================================
-- Administrativa indelningar
-- ============================================================================

CREATE TABLE IF NOT EXISTS lan (
    kod     TEXT PRIMARY KEY,    -- två siffror, t.ex. "01"
    namn    TEXT NOT NULL,
    bokstav TEXT                 -- A, B, C, ... (gamla länsbokstaven)
);

CREATE TABLE IF NOT EXISTS region (
    kod  TEXT PRIMARY KEY,
    namn TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kommun (
    kod           TEXT PRIMARY KEY,   -- fyra siffror, t.ex. "0180"
    namn          TEXT NOT NULL,
    lan_kod       TEXT,
    region_kod    TEXT,
    giltigt_fran  INTEGER,            -- år då koden började gälla
    giltigt_till  INTEGER             -- NULL = aktiv idag
);

CREATE INDEX IF NOT EXISTS ix_kommun_lan    ON kommun (lan_kod);
CREATE INDEX IF NOT EXISTS ix_kommun_region ON kommun (region_kod);


-- ============================================================================
-- NUTS — EU:s statistiska indelning
-- ============================================================================

CREATE TABLE IF NOT EXISTS nuts (
    kommun_kod TEXT NOT NULL,
    niva       INTEGER NOT NULL,   -- 1, 2 eller 3
    kod        TEXT NOT NULL,
    namn       TEXT NOT NULL,
    PRIMARY KEY (kommun_kod, niva)
);


-- ============================================================================
-- Klassificeringar — kommun -> grupp i ett namngivet system
-- ============================================================================
--
-- system-värden i bruk:
--   "skr_kommungrupp"          SKR:s kommungruppsindelning (2023, 9 grupper)
--   "tillvaxtverket"           Tillväxtverkets kommunindelning (landsbygd/stad m.fl.)
--   "fa_region"                Tillväxtanalys FA-regioner (60 st)
--   "h_region"                 SCB:s H-region
--
-- Listan är inte stängd — fler system läggs in när en parsare publicerar
-- första raderna och `klassificeringssystem`-posten skapas.

CREATE TABLE IF NOT EXISTS kommun_klassificering (
    kommun_kod TEXT NOT NULL,
    system     TEXT NOT NULL,
    grupp_kod  TEXT NOT NULL,
    grupp_namn TEXT NOT NULL,
    ar         INTEGER NOT NULL,
    PRIMARY KEY (kommun_kod, system, ar)
);

CREATE INDEX IF NOT EXISTS ix_klass_system_ar
    ON kommun_klassificering (system, ar);

CREATE INDEX IF NOT EXISTS ix_klass_grupp
    ON kommun_klassificering (system, grupp_kod, ar);


-- ============================================================================
-- Historiska tidsserier — folkmängd per kommun, retroaktivt harmoniserad
-- ============================================================================
--
-- SCB publicerar varje år en Excel-fil med folkmängden i samtliga kommuner
-- 1950 → senaste år, harmoniserad till den nuvarande kommunindelningen.
-- Det fyller en lucka som PxWebApi inte täcker: dagens 290 kommuner bakåt
-- i tiden över tidsserier där vissa kommuner formellt skapades långt
-- senare (Knivsta 2003, Heby länsbyte 2007 osv).
--
-- `indelning_ar` flaggar vilken kommunindelning serien följer. Vid SCB-
-- uppdatering med en ny indelning får raderna ett nytt `indelning_ar`-
-- värde — gamla rader bevaras för spårbarhet.

CREATE TABLE IF NOT EXISTS kommun_folkmangd (
    kommun_kod   TEXT NOT NULL,
    ar           INTEGER NOT NULL,
    folkmangd    INTEGER NOT NULL,
    indelning_ar INTEGER NOT NULL,
    PRIMARY KEY (kommun_kod, ar, indelning_ar)
);

CREATE INDEX IF NOT EXISTS ix_folkmangd_ar
    ON kommun_folkmangd (ar, indelning_ar);


-- ============================================================================
-- Sub-kommunenheter
-- ============================================================================

CREATE TABLE IF NOT EXISTS deso (
    kod        TEXT PRIMARY KEY,    -- 9 tecken, t.ex. "0180C2110"
    namn       TEXT NOT NULL,
    kommun_kod TEXT NOT NULL,
    typ        TEXT,                -- A=glesbygd, B=mellan, C=tätort
    ar         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_deso_kommun ON deso (kommun_kod);
CREATE INDEX IF NOT EXISTS ix_deso_typ    ON deso (typ, ar);


CREATE TABLE IF NOT EXISTS regso (
    kod        TEXT PRIMARY KEY,
    namn       TEXT NOT NULL,
    kommun_kod TEXT NOT NULL,
    ar         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_regso_kommun ON regso (kommun_kod);


-- ============================================================================
-- Valgeografi (per valår — distrikt och kretsar ändras inför varje val)
-- ============================================================================
--
-- Svenska val har tre parallella valkretsindelningar — en per valtyp:
--   riksdag  29 valkretsar (oftast = län; storstadskommuner egna)
--   kommun   en eller flera valkretsar per kommun
--   region   en eller flera valkretsar per region
--
-- valkrets bär `typ`-kolumn så att samma tabell håller alla tre. Koderna
-- är inte globalt unika tvärs över typer (en "01" finns både för riksdag
-- och som första kommunvalkrets i varje kommun) — därför är PK (typ, kod, ar).
--
-- valdistrikt har tre nullable kolumner som var och en pekar på sin
-- valkrets-typ. Det undviker en länkfält-tabell eftersom man alltid joinar
-- på exakt en typ åt gången.

CREATE TABLE IF NOT EXISTS valkrets (
    typ  TEXT NOT NULL,            -- 'riksdag' | 'kommun' | 'region'
    kod  TEXT NOT NULL,
    ar   INTEGER NOT NULL,
    namn TEXT NOT NULL,
    PRIMARY KEY (typ, kod, ar)
);


CREATE TABLE IF NOT EXISTS valdistrikt (
    kod                  TEXT NOT NULL,
    ar                   INTEGER NOT NULL,
    namn                 TEXT NOT NULL,
    kommun_kod           TEXT NOT NULL,
    riksdagsvalkrets_kod TEXT,
    kommunvalkrets_kod   TEXT,
    regionvalkrets_kod   TEXT,
    PRIMARY KEY (kod, ar)
);

CREATE INDEX IF NOT EXISTS ix_valdistrikt_kommun
    ON valdistrikt (kommun_kod, ar);

CREATE INDEX IF NOT EXISTS ix_valdistrikt_riksdagsvalkrets
    ON valdistrikt (riksdagsvalkrets_kod, ar);
CREATE INDEX IF NOT EXISTS ix_valdistrikt_kommunvalkrets
    ON valdistrikt (kommunvalkrets_kod, ar);
CREATE INDEX IF NOT EXISTS ix_valdistrikt_regionvalkrets
    ON valdistrikt (regionvalkrets_kod, ar);


-- ============================================================================
-- Postnummerområden (ortogonal mot kommun — kan korsa gränser)
-- ============================================================================

CREATE TABLE IF NOT EXISTS postnummer (
    postnr              TEXT PRIMARY KEY,   -- fem siffror som sträng
    ort                 TEXT NOT NULL,
    dominant_kommun_kod TEXT,
    latitud             REAL,               -- WGS84, från Geonames
    longitud            REAL                -- WGS84, från Geonames
);

CREATE TABLE IF NOT EXISTS postnummer_kommun (
    postnr     TEXT NOT NULL,
    kommun_kod TEXT NOT NULL,
    andel      REAL,                       -- 0.0 - 1.0 (areaandel)
    PRIMARY KEY (postnr, kommun_kod)
);

CREATE INDEX IF NOT EXISTS ix_postnr_kommun
    ON postnummer_kommun (kommun_kod);

-- ============================================================================
-- Synkjournal
-- ============================================================================
-- Ungefär hälften av de lagrade tabellerna bär `senast_synkad` per rad, resten
-- inte. Utan en gemensam plats går det därför inte att svara på den enda fråga
-- som spelar roll för lagrad data: är den här färsk nog att lita på?
--
-- Journalen är en rad per körning, inte en status som skrivs över. Det gör att
-- kadensen går att mäta i efterhand i stället för att antas — och en källa som
-- slutat uppdateras syns som ett växande glapp, inte som tystnad.

CREATE TABLE IF NOT EXISTS synkkorning (
    id          BIGSERIAL PRIMARY KEY,
    dataset     TEXT NOT NULL,          -- nyckel i geodata/synkregister.py
    tidpunkt    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    antal_rader BIGINT,
    kommentar   TEXT
);
CREATE INDEX IF NOT EXISTS ix_synkkorning_dataset
    ON synkkorning (dataset, tidpunkt DESC);

-- ============================================================================
-- Mellanlager för filbaserade källor
-- ============================================================================
-- Myndigheter utan API levererar Excel- eller CSV-filer. Att hämta och tolka
-- om en fil vid varje fråga är både långsamt och onödigt tryck på källan, men
-- filerna hör inte hemma i det permanenta beståndet heller — de går att hämta
-- igen, till skillnad från SCB:s omräknade folkmängdsserie.
--
-- Därför ett mellanlager med hållbarhet. `DOA_MELLANLAGRING_DAGAR` styr hur
-- länge en hämtning behålls; `cli/rensa_mellanlager.py` tar bort det som
-- passerat. Utan rensning växer lagret tyst med varje ny årgång.

CREATE TABLE IF NOT EXISTS fil_mellanlager (
    kalla      TEXT NOT NULL,           -- myndighetsnyckel, t.ex. migrationsverket
    dataset    TEXT NOT NULL,
    blad       TEXT NOT NULL,
    hamtad     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kalla_url  TEXT,                    -- varifrån, för spårbarhet
    kolumner   JSONB NOT NULL,
    rader      JSONB NOT NULL,
    maskerade  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kalla, dataset, blad)
);
CREATE INDEX IF NOT EXISTS ix_fil_mellanlager_hamtad
    ON fil_mellanlager (hamtad);

-- Originalfiler. Öppna data-filer kan ha hundratusentals rader — en
-- kommuns leverantörsreskontra är tiotusentals per månad — och är för
-- stora för att lagras tolkade som JSONB. Filen sparas som den kom och
-- tolkas vid varje användning; samma hållbarhet och samma rensning.
CREATE TABLE IF NOT EXISTS fil_mellanlager_original (
    url           TEXT PRIMARY KEY,
    hamtad        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    innehallstyp  TEXT,
    storlek       INTEGER NOT NULL,
    innehall      BYTEA NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fil_mellanlager_original_hamtad
    ON fil_mellanlager_original (hamtad);

-- ============================================================================
-- Källsignaturer — bevakning av att källan inte publicerat något nytt
-- ============================================================================
-- `synkstatus` svarar på hur gammal VÅR kopia är. Det säger ingenting om
-- källan: nio av nitton dataset är händelsestyrda och blir inaktuella först
-- när myndigheten publicerar en ny version, inte när tiden går.
--
-- Signaturen är vad servern själv uppger om filen — ETag, Last-Modified,
-- storlek. Ändras något av dem har källan publicerat om. Det är en robustare
-- bevakning än att skrapa myndighetens webbsida efter en ny rubrik, och den
-- fungerar likadant för alla källor.

CREATE TABLE IF NOT EXISTS kallsignatur (
    dataset             TEXT PRIMARY KEY,   -- nyckel i geodata/synkregister.py
    url                 TEXT NOT NULL,
    etag                TEXT,
    last_modified       TEXT,
    storlek             BIGINT,
    forst_sedd          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    senast_kontrollerad TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    senast_andrad       TIMESTAMPTZ
);
