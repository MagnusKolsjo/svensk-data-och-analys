# Excel-integration via Power Query

Lokala REST-server (FastAPI) på `http://127.0.0.1:8000` som Excel via
Power Query kan konsumera. Power Query funkar identiskt i Excel for Mac
och Excel for Windows — samma M-kod, samma resultat.

## Installation

### Starta tjänsten

Från repots rot:

```bash
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Ska tjänsten starta vid inloggning och startas om när den kraschar, lägg
kommandot i en launchd-tjänst (macOS) eller en systemd-tjänst (Linux).
Använd `bootstrap`/`bootout` med launchd, eftersom `load`/`unload` är
föråldrade och ger "Input/output error" på nyare macOS.

### Verifiera att den lever

```bash
curl http://localhost:8000/halsa
# {"status":"ok"}

open http://localhost:8000/docs
# Swagger UI med alla endpoints
```

## Power Query — exempel

I Excel, gå till **Data → Hämta data → Från annan källa → Tomt frågeformulär**
(`Get Data → From Other Sources → Blank Query`). Klicka på
**Avancerad redigerare** (`Advanced Editor`) och klistra in:

### Alla 290 kommuner med län- och regiontillhörighet

```m
let
    Källa = Json.Document(Web.Contents("http://localhost:8000/geo/kommuner")),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### Kommuner i en specifik klassificeringsgrupp

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/geo/kommuner-i-grupp",
        [Query=[system="skr_kommungrupp", grupp_kod="C9"]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### Compositionell — kommuner som matchar flera kriterier

```m
let
    Kriterier = "{""skr_kommungrupp"":""C9"",""tillvaxtverket_sl3"":""3""}",
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/geo/filtrera",
        [Query=[kriterier=Kriterier]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### Kolada-data för flera kommuner och år

KPI `N00945` (sysselsättningsgrad) för Stockholm, Göteborg och Malmö
2020–2024:

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/kolada/data",
        [Query=[
            kpi="N00945",
            kommun="0180,1480,1280",
            ar="2020,2021,2022,2023,2024"
        ]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

Svaret är plattat — en rad per (kpi, kommun, år, kön):

| kpi | kommun | ar | kon | varde | status |
|---|---|---|---|---|---|
| N00945 | 0180 | 2023 | T | 39.3 | |
| N00945 | 1480 | 2023 | T | 39.0 | |
| N00945 | 1280 | 2023 | T | 34.9 | |

Klart för pivot direkt — kommun på rad-axeln, år på kolumn-axeln,
värde i cellerna.

### Söka KPI:er på fritext

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/kolada/kpier",
        [Query=[title="arbetslöshet", per_page="50"]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### SCB-tabell via PxWebApi 2 — arbetslöshet 16-64 år per kvartal

`/pxweb-2/data` returnerar data plattat med en rad per cell — perfekt
för Power Query.

```m
let
    Urval = "{""ContentsCode"":[""*""],""Tid"":[""2024K4"",""2025K1""],""Kon"":[""1+2""]}",
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/pxweb-2/data",
        [Query=[tabell_id="TAB175", urval=Urval]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

Plattningen ger en tabell med kolumner som `Tid`, `Tid_kod`,
`ContentsCode`, `Kon`, `varde` osv — direkt pivot-bart.

### Söka tabeller hos SCB

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/pxweb-2/tabeller",
        [Query=[query="arbetslöshet", sida_storlek="20"]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### Jordbruksstatistik via PxWeb v1

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/pxweb-1/noder",
        [Query=[myndighet="jordbruksverket", sokvag="JO"]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### SCB:s tätorter i en kommun (via WFS-routern)

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/wfs/features",
        [Query=[
            type_name="stat:Tatorter_2023",
            cql_filter="kommunnamn='Mora'",
            utan_geometri="true"
        ]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

### LST EBH — förorenade områden i en kommun

```m
let
    Källa = Json.Document(Web.Contents(
        "http://localhost:8000/arcgis/features",
        [Query=[
            service_path="LST/LST_Potentiellt_fororenade_omraden_EBH_EXT",
            layer_id="0",
            where="Kommun='STOCKHOLM'",
            count="2000",
            utan_geometri="true"
        ]]
    )),
    Tabell = Table.FromRecords(Källa)
in
    Tabell
```

## Tips

- **Uppdatera data:** `Data → Uppdatera alla` (Cmd+Alt+F5 / Ctrl+Alt+F5)
- **Parametrar i Excel-celler:** Skapa en namngiven cell, läs in via
  `Excel.CurrentWorkbook(){[Name="MinKommun"]}[Content]{0}[Column1]` och
  bygg frågan dynamiskt med kommunkoden från cellen.
- **Schemalagd uppdatering** kräver Excel Online + Power BI-licens på Mac;
  på Windows funkar det lokalt om filen är öppen.
- **Stora svar:** WFS- och ArcGIS-routrarna har `utan_geometri=true`
  som default — Excel laddar inte tunga polygoner som blir oanvändbara
  i celler.

## Felsökning

| Symptom | Lösning |
|---|---|
| Power Query: "Det gick inte att ansluta" | Verifiera att tjänsten kör: `curl http://localhost:8000/halsa` |
| Tjänsten startar inte | Kolla `~/Library/Logs/doa-api.error.log` |
| Svaret är tomt | Kolla att DB är uppe: `pg_isready -p 5433` |
| Långsamt | Kolada-API:t kan vara långsamt vid stora datafrågor — använd `kpi=...` och `kommun=...` för att smala ner |

