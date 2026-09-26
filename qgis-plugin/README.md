# QGIS-plugin — Svensk data och analys

Söker svensk officiell statistik och lägger upp den som **choropleth-lager**
i QGIS — kommun, län, DeSO, RegSO, valdistrikt. Visar källstatistik (t.ex.
Kolada-nyckeltal) eller resultat av egna analyser på karta.

Backend är den lokala FastAPI-tjänsten på `http://localhost:8000` (samma som
Excel-panelen). QGIS pratar ren HTTP mot localhost — inget cert behövs.

## Installation

1. Se till att doa-api-tjänsten körs:
   ```bash
   curl http://localhost:8000/karta/omraden
   ```
   (Ska lista kommun, lan, deso, regso, valdistrikt med antal.)

2. **Installera (rekommenderat — funkar i både QGIS 3 och 4):** zippa mappen
   och installera via GUI, så slipper du gissa profil-sökväg.
   ```bash
   cd qgis-plugin
   zip -r svensk_data_analys.zip svensk_data_analys
   ```
   I QGIS: **Plugins → Hantera och installera plugin → Installera från ZIP** →
   välj `svensk_data_analys.zip`.

   *Alternativt* kopiera mappen direkt till profilens plugin-katalog. Hitta den
   via **Inställningar → Användarprofiler → Öppna aktiv profilmapp** och lägg
   `svensk_data_analys/` under `python/plugins/`.

3. Aktivera **Svensk data och analys** under **Installerade**. En knapp dyker
   upp i verktygsfältet/Webb-menyn → öppnar panelen till höger.

> QGIS 4 / Qt6 stöds (`supportsQt6=True`, scopade enum:er, modernt
> renderer-API). Plugin:en fungerar även i QGIS 3.16+.

## Användning

**Visa källstatistik:**
1. Sök (t.ex. "ohälsotal", "arbetslöshet") → markera ett Kolada-nyckeltal.
2. Välj **År** och klicka **Visa vald statistik på karta** → ett färglagt
   kommun-lager läggs till (kvantil-klassning, Viridis).

**Områdeslager (bara geometri):** välj områdestyp och **Lägg till
områdeslager** — t.ex. valdistrikt eller DeSO som bakgrund.

**Importera analysresultat:** har du en analys (till exempel en Excel-export)
med en kod-kolumn (kommunkod) och en värde-kolumn → välj områdestyp och
**Importera analysresultat (CSV)**. Pluginet matchar kolumnerna automatiskt
(annars de två första) och ritar choropleth.

## Steg 1 / kommande

- Dimensionella källor (SCB, PxWeb v1) har region/år/-val. Välj dem i
  Excel-panelen, exportera och importera som analysresultat — eller (kommande)
  ett dimensionssteg direkt i pluginet.
- Direktanslutning till PostGIS (DeSO/RegSO/valdistrikt + mv_kommun_geom/
  mv_lan_geom) är ett alternativ till HTTP för avancerad analys i QGIS.

## Alternativ: direkt PostGIS / WFS

QGIS kan också ansluta direkt till:
- **PostGIS** — geom-tabellerna och de materialiserade vyerna
  `mv_kommun_geom`, `mv_lan_geom` (lägg till en PostGIS-anslutning).
- **WFS / OGC API Features** — källornas egna tjänster (SCB geoserver,
  Lantmäteriet OAFeat m.fl.) via doa-katalog/geodata.
