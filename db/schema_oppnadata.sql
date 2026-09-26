-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
--
-- Register över var kommuner, regioner och myndigheter delar öppna data.
--
-- Sökning efter öppna data går direkt till varje organisations egen
-- katalog, inte via dataportal.se. Dataportalen skördar det som anmälts,
-- och en stor del av anmälningarna pekar på adresser som slutat svara.
-- Registret håller i stället det som faktiskt svarade när det
-- kontrollerades: plattform, adress och — för delade instanser — vilken
-- kontext som är organisationens.
--
-- Fylls av cli/kartlagg_datadelning.py. En rad som inte bekräftas vid en
-- ny kartläggning raderas inte; `kontrollerad` visar hur gammal uppgiften
-- är. Ett tillfälligt nätfel ska inte kunna tömma registret.

-- ============================================================================
-- Datakataloger
-- ============================================================================

CREATE TABLE IF NOT EXISTS datakatalog (
    org_nyckel    TEXT NOT NULL,           -- kommun:0180, region:AB, myndighet:<orgnr eller namn>
    kategori      TEXT NOT NULL,           -- kommun, region, myndighet
    kod           TEXT,                    -- kommunkod, länsbokstav eller organisationsnummer
    namn          TEXT NOT NULL,
    lan_kod       TEXT,                    -- tvåsiffrig länskod för kommuner och regioner
    plattform     TEXT NOT NULL,           -- entryscape, ckan, huwise, arcgis_hub, dcat_fil
    bas_url       TEXT NOT NULL,           -- plattformens rot, t.ex. https://catalog.sodertalje.se
    kontext       TEXT NOT NULL DEFAULT '', -- EntryScape-kontext när instansen delas av många utgivare
    katalog_url   TEXT,                    -- DCAT-källan, när en sådan är känd
    antal_dataset INTEGER,
    belagg        TEXT,                    -- hur plattformen fastställdes
    kontrollerad  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_nyckel, plattform, bas_url, kontext)
);

CREATE INDEX IF NOT EXISTS ix_datakatalog_urval
    ON datakatalog (kategori, lan_kod);
