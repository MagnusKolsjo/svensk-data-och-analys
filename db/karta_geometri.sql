-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
-- Härledd kommun- och länsgeometri för kartlagret.
--
-- Kommun- och länspolygoner saknas i geom-tabellerna, men DeSO-geometrin
-- finns och DeSO-koden bär kommunkoden (4 första tecknen) respektive länskoden
-- (2 första). Vi unionerar därför upp DeSO → kommun/län i materialiserade vyer.
-- Körs INTE av initiera_schema (skulle blockera MCP-uppstart i ~30 s) — byggs
-- separat via cli eller karta-routerns refresh-endpoint.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_kommun_geom AS
SELECT substring(d.kod, 1, 4) AS kod,
       k.namn,
       ST_Union(d.geom) AS geom
FROM deso_geom d
LEFT JOIN kommun k ON k.kod = substring(d.kod, 1, 4)
GROUP BY substring(d.kod, 1, 4), k.namn;
CREATE UNIQUE INDEX IF NOT EXISTS mv_kommun_geom_kod ON mv_kommun_geom (kod);
CREATE INDEX IF NOT EXISTS mv_kommun_geom_gix ON mv_kommun_geom USING GIST (geom);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_lan_geom AS
SELECT substring(d.kod, 1, 2) AS kod,
       l.namn,
       ST_Union(d.geom) AS geom
FROM deso_geom d
LEFT JOIN lan l ON l.kod = substring(d.kod, 1, 2)
GROUP BY substring(d.kod, 1, 2), l.namn;
CREATE UNIQUE INDEX IF NOT EXISTS mv_lan_geom_kod ON mv_lan_geom (kod);
CREATE INDEX IF NOT EXISTS mv_lan_geom_gix ON mv_lan_geom USING GIST (geom);

-- ---------------------------------------------------------------------------
-- Landklippta geometrier — kommun och län utan vattenområden
-- ---------------------------------------------------------------------------
-- DeSO-ytorna följer den administrativa gränsen, som för kustkommuner löper
-- långt ut i havet: unionen blir 532 000 km² mot Sveriges faktiska 447 000,
-- och Gotland fem gånger för stort. Snittet mot en landmask rättar det.
--
-- Landmasken är EEA:s kustlinje (CC-BY 4.0), inte Eurostat GISCO
-- (© EuroGeographics). GISCO förbjuder kommersiell användning och dess
-- nyttjanderätt är inte överlåtbar, vilket gör kartutdata ofritt för
-- mottagaren och krockar med AGPL:s syfte. EEA är dessutom finare —
-- 1:100 000 mot 1:1 miljon — och träffar SCB:s officiella landarealer
-- bättre: Ekerö +0,3 % mot GISCO:s +78 %.
--
-- Fylls av cli/synka_kustlinje.py. Byggs INTE av initiera_schema — snittet
-- tar drygt en minut och skulle blockera MCP-uppstart.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_kommun_land AS
SELECT k.kod, k.namn,
       ST_Multi(ST_CollectionExtract(ST_Intersection(k.geom, kl.geom), 3)) AS geom
FROM mv_kommun_geom k
CROSS JOIN LATERAL (
    SELECT ST_Union(c.geom) AS geom
    FROM kustlinje_geom c
    WHERE ST_Intersects(c.geom, k.geom)
) kl
WHERE kl.geom IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS mv_kommun_land_kod ON mv_kommun_land (kod);
CREATE INDEX IF NOT EXISTS mv_kommun_land_gix ON mv_kommun_land USING GIST (geom);

-- Länen unioneras ur kommunvyn, aldrig ur en egen källa — då kan en länsgräns
-- och summan av dess kommuner inte glida isär.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_lan_land AS
SELECT substring(k.kod, 1, 2) AS kod, l.namn,
       ST_Multi(ST_UnaryUnion(ST_Collect(k.geom))) AS geom
FROM mv_kommun_land k
LEFT JOIN lan l ON l.kod = substring(k.kod, 1, 2)
GROUP BY substring(k.kod, 1, 2), l.namn;
CREATE UNIQUE INDEX IF NOT EXISTS mv_lan_land_kod ON mv_lan_land (kod);
CREATE INDEX IF NOT EXISTS mv_lan_land_gix ON mv_lan_land USING GIST (geom);

-- Vitlistade namn i api/routers/karta.py. Casten behövs för att
-- CREATE OR REPLACE VIEW ska kunna peka om dem senare.
CREATE OR REPLACE VIEW kommun_land_v AS
SELECT kod, namn, geom::geometry(MultiPolygon, 4326) AS geom FROM mv_kommun_land;
CREATE OR REPLACE VIEW lan_land_v AS
SELECT kod, namn, geom::geometry(MultiPolygon, 4326) AS geom FROM mv_lan_land;
