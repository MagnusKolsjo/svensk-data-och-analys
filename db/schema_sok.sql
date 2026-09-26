-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
-- Sök-index för federerad/semantisk sökning över datakällorna.
-- Kräver PostgreSQL med pgvector. En rad per sökbart objekt (KPI, tabell,
-- serie, produkt, databas) med både FTS-vektor och embedding för hybridsök.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS sok_index (
    id          SERIAL PRIMARY KEY,
    kalla       TEXT NOT NULL,
    typ         TEXT NOT NULL,
    kalla_id    TEXT NOT NULL,
    namn        TEXT,
    beskrivning TEXT,
    hamta_api   TEXT,
    extern_url  TEXT,
    sok_text    TEXT NOT NULL,
    fts         tsvector GENERATED ALWAYS AS (to_tsvector('swedish', coalesce(sok_text, ''))) STORED,
    embedding   vector(768),
    fasetter    JSONB,
    uppdaterad  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kalla, kalla_id)
);
-- Hårda filterfasetter per objekt (källspecifika strukturerade attribut, t.ex.
-- skolform och huvudman för skolenheter). JSONB håller indexet generiskt —
-- nya fasetter kräver ingen schemaändring. ALTER för redan skapade tabeller.
ALTER TABLE sok_index ADD COLUMN IF NOT EXISTS fasetter JSONB;
CREATE INDEX IF NOT EXISTS sok_index_fts ON sok_index USING GIN (fts);
CREATE INDEX IF NOT EXISTS sok_index_vec ON sok_index USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS sok_index_fasetter ON sok_index USING GIN (fasetter);
