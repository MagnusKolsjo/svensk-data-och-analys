# svensk-data-och-analys

MCP-servrar, ett tillägg för Excel och ett plugin för QGIS som hittar, hämtar
och kartlägger statistik och data från svenska myndigheter. Ett sökbart index
täcker drygt 23 000 dataserier. Kartlagren är fritt licensierade och finns för
flera geografiska indelningar, från län ner till DeSO och valdistrikt.

Sviten gör två saker. Den ger ett gemensamt gränssnitt mot myndigheternas
egna API:er, så att man slipper lära sig varje myndighets API för sig. Den
håller också lokalt det som inte går att hämta när frågan ställs:
geografiska indelningar, geometrier och ett sökindex över vad som finns.
Statistiken hämtas från källan vid varje fråga och lagras inte.

## Innehåll

| Del | Vad den gör |
|---|---|
| **MCP-servrar** | 12 servrar med drygt 120 verktyg. De fungerar med alla MCP-klienter över stdio eller HTTP. |
| **REST-API** | En lokal FastAPI-tjänst som Power Query, Excel-tillägget och QGIS-pluginet läser från |
| **Excel-tillägg** | En sidopanel för sökning och filtrering, plus funktionerna `DOA.SOK`, `DOA.HAMTA`, `DOA.KOLADA` och `DOA.RIKSBANK` |
| **QGIS-plugin** | Söker statistik och ritar den som choroplet. Importerar egna resultat i formen kommunkod + värde. |
| **Sökindex** | Hybridsökning (fulltext + semantisk) över dataserier från 16 källor. Filtren begränsar varandra, så ett val som ger noll träffar kan aldrig göras. |

MCP-servrarna ritar kartor som en interaktiv widget i klienter som stöder
tillägget MCP Apps (`io.modelcontextprotocol/ui`). Geometrin går direkt till
widgeten och belastar inte modellens kontext.

## Källor

### Statistik från myndigheter

| Källa | Innehåll | Åtkomst |
|---|---|---|
| SCB | Officiell statistik inom alla ämnesområden | PxWebApi 2 |
| Jordbruksverket | Jordbruksstatistik | PxWeb v1 |
| Folkhälsomyndigheten | Folkhälsodata | PxWeb v1 |
| Konjunkturinstitutet | Statistikdatabasen och prognosdatabasen | PxWeb v1 |
| CSN | Studiestöd | PxWeb v1 |
| Energimyndigheten | Energistatistik | PxWeb v1 |
| Skogsstyrelsen | Skogsstatistik | PxWeb v1 |
| Tillväxtanalys | Näringsliv och företagande | PxWeb v1 |
| Skolverket | Statistikdatabasen, skolenhetsregistret och planerade utbildningar | PxWeb v1 och REST |
| Kolada (RKA) | Omkring 6 000 nyckeltal för kommuner och regioner | REST |
| Socialstyrelsen | Statistikdatabasen | REST |
| Försäkringskassan | Öppna data | DCAT + CSV |
| Brå | Anmälda brott per brottstyp, region och period | Webbgränssnittet SolWebb, eftersom det saknas API |
| Trafikanalys | Transportstatistik | REST |
| Kungliga biblioteket | Biblioteksstatistik (Bibstat) | REST |
| Riksbanken | Räntor och valutakurser | REST |
| Migrationsverket | Asyl-, tillstånds- och mottagningsstatistik, delvis per kommun | Excelfiler som mellanlagras |

PxWeb-myndigheterna ligger i ett versionsregister
(`data_och_analys/klienter/myndigheter.py`). När en myndighet går över från
PxWeb v1 till v2 räcker det att ändra en rad i `.env`. Ingen kodändring behövs.

Trafikanalys läggs ned 2026-12-31 och uppgår i Tillväxtanalys.

### Geodatatjänster

| Tjänst | Innehåll | Protokoll |
|---|---|---|
| SCB | DeSO, RegSO, tätorter, småorter, befolkning per kilometerruta med mera (49 lager) | WFS |
| Socialstyrelsen | Skador och förgiftningar per län (NUTS 3) | WFS |
| SMHI | Avrinningsområden, vattendistrikt och vattenförekomster (26 lager) | WFS |
| Naturvårdsverket | Skyddad natur | WFS |
| Jordbruksverket | Blockindelning, skiftesindelning, stödområden med mera (28 lager) | WFS |
| Länsstyrelserna | Potentiellt förorenade områden (EBH), miljörisker, naturvård, kulturmiljö | ArcGIS REST |
| Lantmäteriet | Administrativ indelning | OGC API Features. Kräver avtal med Lantmäteriet. |

### Kataloger – var data finns

Katalogerna innehåller inga data. De svarar på vilka datamängder som finns,
vem som publicerar dem och var de kan hämtas. Med den informationen går det
att välja rätt hämtare bland verktygen ovan.

| Katalog | Innehåll | Protokoll |
|---|---|---|
| [Dataportal.se](https://www.dataportal.se) | Sveriges nationella portal för öppna data, byggd på EntryScape. Den beskriver datamängder från offentliga aktörer med distributioner, format och åtkomstadresser, och används bland annat för att hitta nya PxWeb-instanser. | DCAT-AP-SE via SPARQL |
| [Researchdata.se](https://researchdata.se) / SND | Forskningsdata från svenska lärosäten och Svensk nationell datatjänst. Studiebeskrivningar finns i Dublin Core. Samhällsvetenskapliga studier som VALU och SNES har dessutom variabel- och kodboksnivå i DDI. | OAI-PMH |
| [Geodataportalen](https://www.geodata.se) | Sveriges nationella geodatakatalog med metadata från ett 25-tal myndigheter och länkar till deras WFS-, WMS- och OGC API-tjänster | GeoNetwork |

### Öppna data från kommuner, regioner och myndigheter

Kommuner, regioner och myndigheter publicerar öppna data i egna kataloger.
Servern `doa-oppnadata` söker direkt i dem, beskriver datamängderna enligt
DCAT-AP-SE och läser datat. Resultaten aggregeras på serversidan, så att
frågor som "hur fördelar sig kommunernas leverantörsfakturor på
utgiftstyper första kvartalet" går att besvara utan att rådata passerar
modellens kontext.

Vilken katalog varje organisation har står i ett register som
`cli/kartlagg_datadelning.py` fyller. Kartläggningen prövar vanliga värdnamn
och sökvägar hos alla kommuner, regioner och myndigheter och identifierar
plattformen genom att anropa dess API. Den läser också dataportal.se:s
skördningar, men bara kataloger som svarade och inte var tomma kommer in i
registret. En stor del av anmälningarna till dataportal.se pekar på
adresser som slutat svara.

| Plattform | Sökning | Hämtning |
|---|---|---|
| EntryScape | Instansens egen store, avgränsad per kontext när instansen delas | Filer, rowstore |
| CKAN | `package_search` | Filer, datastore |
| Huwise | Explore API v2 | Export, poster |
| ArcGIS Hub | Webbplatsens sök-API | CSV- och GeoJSON-nedladdning, tjänstelager |
| DCAT-fil | Katalogfilen, tolkad som RDF | Det distributionerna anger |

En distributions `accessURL` kan enligt DCAT-AP-SE vara en fil, en webbsida
eller ett API. Hämtningsvägen avgörs därför ur allt distributionen bär:
datatjänst, format, `downloadURL` och adressens form. Tabeller kan komma
som CSV (också UTF-16 och Windows-1252), Excel, OpenDocument, JSON,
GeoJSON, GeoPackage, shapefil i ZIP, rowstore, CKAN datastore, WFS, OGC API
Features och ArcGIS REST. Perioder som publiceras som en fil eller en
distribution per månad slås ihop.

### Generella klienter

Klienterna nedan talar ett protokoll och är inte bundna till en viss
myndighet. Alla tar emot en adress direkt, som den står i en
DCAT-distribution. Namngivna instanser läggs till i klientens register, och
en befintlig kan pekas om i `.env`.

| Klient | Protokoll | Förregistrerade instanser |
|---|---|---|
| PxWeb | PxWeb API v1 och PxWebApi 2 | Myndigheterna ovan |
| WFS | OGC Web Feature Service 2.0, GeoJSON och GML | SCB, Socialstyrelsen, SMHI, Naturvårdsverket, Jordbruksverket |
| OGC API Features | OGC API – Features | Lantmäteriet |
| ArcGIS | ArcGIS REST, FeatureServer och MapServer | Länsstyrelserna |
| CKAN | CKAN Action API v3 och datastore | Alla i katalogregistret |
| EntryScape | EntryStore och rowstore | Alla i katalogregistret |
| ArcGIS Hub | Hubs sök- och nedladdnings-API | Alla i katalogregistret |
| DCAT-fil | DCAT-AP-SE som RDF/XML, Turtle eller JSON-LD | Alla i katalogregistret |
| Huwise | Huwise Explore API v2 (ODSQL) | hub.huwise.com, med data.opendatasoft.com som reserv — bolagets domän från tiden då det hette Opendatasoft, där dataset som ännu inte flyttats finns kvar |

## Lagrat lokalt

Sviten lagrar bara det som inte går att hämta när frågan ställs.

**Indelningar:** riket, NUTS 1–3, län och region, kommun, RegSO, DeSO,
valkrets och valdistrikt. Tre kommunklassificeringar finns också:
Tillväxtverkets FA-regioner, Tillväxtverkets kommuntyper och SKR:s
kommungrupper. Dessutom postnummer med koppling till kommun.

**Kartlager:** län, kommun, RegSO, DeSO och valdistrikt som ytor. Skolenheter
som punkter.

**Folkmängd per kommun 1950–2024.** SCB:s historiska serie, där alla år är
omräknade till dagens kommunindelning. PxWeb redovisar varje år i det årets
gränser, så en lång serie därifrån blandar befolkningsförändring med
gränsdragning. För aktuell folkmängd hämtar sviten från SCB.

**Katalogregistret.** Vilken katalog varje kommun, region och myndighet har,
på vilken plattform och med hur många datamängder. Fylls av
`cli/kartlagg_datadelning.py`.

**Mellanlager.** Filer som hämtas ur öppna data-kataloger sparas som de kom i
ett mellanlager med begränsad hållbarhet, som standard 30 dagar
(`DOA_MELLANLAGRING_DAGAR`). Den schemalagda synken tar bort det som
passerat.

Varje lagrat dataset har en angiven uppdateringstakt. Takten är dokumenterad
av källan, uppmätt i datat eller styrd av händelser som val och reformer.
`geo_synkstatus` visar hur gammal den egna kopian är. `geo_bevaka_kallor`
frågar källan om filen har bytts ut sedan senaste synk. Den schemalagda
synken håller kopiorna aktuella utifrån båda.

## Krav

- Python 3.12 eller senare
- PostgreSQL med **PostGIS** för kartlagren och **pgvector** för sökindexet

Verktygen som bara läser externa API:er (PxWeb, Kolada, WFS och så vidare)
klarar sig utan databas. SQLite räcker för de relationella indelningarna men
har inget stöd för geometrier eller vektorsökning.

Sökindexet använder
[KBLab/sentence-bert-swedish-cased](https://huggingface.co/KBLab/sentence-bert-swedish-cased)
för semantisk likhet. Modellen hämtas första gången den används.

## Kom igång

```bash
git clone https://github.com/MagnusKolsjo/svensk-data-och-analys.git
cd svensk-data-och-analys
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.env .env      # fyll i databasuppgifterna
```

Schemat skapas när `doa-geo` startar första gången. Därefter fylls de lokala
tabellerna:

1. Kör synkverktygen i `doa-geo`, som alla har prefixet `geo_synka_`.
   Tillväxtverkets kommuntyper läggs in först, eftersom de skapar
   grundtabellerna för län och kommun.
2. `cli/synka_kustlinje.py` klipper kommun- och länsytorna mot kustlinjen.
3. `cli/synka_skolenheter.py` hämtar skolenheternas koordinater.
4. `POST /sok/bygg-index` bygger sökindexet och `cli/synka_geo_fasetter.py`
   markerar vilka dataserier som går att lägga på karta. Båda körs
   inkrementellt, så en ny körning uppdaterar bara det som ändrats.
5. `cli/kartlagg_datadelning.py` fyller katalogregistret. Alla listor kommer
   ur öppna källor: kommuner och regioner ur SKR:s förteckningar, myndigheter
   ur SCB:s myndighetsregister. Tidigare namn och sammanslagningar kommer ur
   Statskontorets myndighetsinformation, och föregångarnas webbplatser ur
   Wikidata. De behövs, eftersom många kataloger är anmälda av myndigheter som
   bytt namn eller gått upp i andra — och museer behåller ofta sina
   domäner. Kartläggningen skriver också en rapport till
   `.cache/datadelning/`. En egen myndighetslista kan ges med
   `--myndigheter <FIL>.csv`.

### Schemalagd synk

```bash
.venv/bin/python cli/synka.py --installera-schema
```

installerar en daglig körning, som standard 04:45, via launchd på macOS och
cron på övriga system. Varje körning har tre steg:

- **Källor.** Händelsestyrda dataset — indelningar, klassificeringar,
  valdistrikt — blir inaktuella när källan publicerar, inte när tiden går.
  Bevakningen frågar källan om filen bytts ut, och i så fall hämtas den om.
  SKR, Tillväxtverket, SCB:s DeSO/RegSO-koppling och val.se lägger nya
  årgångar på nya adresser. De adresserna står i `.env` (`DOA_*_URL`). När
  en ny årgång publiceras byts adressen där, och nästa körning hämtar den.
- **Takt.** Skolenheter, postnummer, folkmängdsserien, sökindexet och
  katalogregistret synkas om när deras ålder passerat den angivna takten.
- **Städning.** Mellanlagret rensas från det som passerat sin hållbarhet.

Loggen hamnar i `logs/synk-<DATUM>.log`. Körningen avslutas med kod 1 om
något steg felat, men de övriga stegen körs ändå. Den rör inte dataset som
aldrig synkats: den första synken görs för hand enligt stegen ovan.

```bash
.venv/bin/python cli/synka.py --visa           # vad som skulle synkas
.venv/bin/python cli/synka.py --steg kallor    # bara ett steg
```

Tid och schemaläggare ändras i `.env` (`DOA_SYNK_SCHEMA`,
`DOA_SYNK_SCHEMALAGGARE`); kör sedan `--installera-schema` igen.

### MCP-klient

Varje server startas som en egen process. De flesta MCP-klienter tar emot en
konfiguration i den här formen:

```json
{
  "mcpServers": {
    "doa-geo": {
      "command": "/sökväg/till/svensk-data-och-analys/.venv/bin/python",
      "args": ["/sökväg/till/svensk-data-och-analys/mcp_servrar/geo_mcp.py"]
    }
  }
}
```

| Server | Fil | Innehåll |
|---|---|---|
| `doa-geo` | `geo_mcp.py` | Indelningar, klassificeringar, kartor, synk |
| `doa-pxweb-1` | `pxweb_1_mcp.py` | Myndigheter på PxWeb v1 |
| `doa-pxweb-2` | `pxweb_2_mcp.py` | Myndigheter på PxWebApi 2 |
| `doa-kolada` | `kolada_mcp.py` | Kolada |
| `doa-special` | `special_mcp.py` | Skolverket, Socialstyrelsen, Försäkringskassan, Brå, Trafikanalys, KB, Riksbanken, Migrationsverket |
| `doa-katalog` | `katalog_mcp.py` | Dataportal.se och Researchdata.se |
| `doa-oppnadata` | `oppnadata_mcp.py` | Öppna data från kommuner, regioner och myndigheter |
| `doa-geodata` | `geodataportalen_mcp.py` | Geodataportalen |
| `doa-wfs` | `wfs_mcp.py` | WFS |
| `doa-oafeat` | `ogc_api_features_mcp.py` | OGC API Features |
| `doa-arcgis` | `arcgis_features_mcp.py` | ArcGIS REST |
| `doa-huwise` | `huwise_mcp.py` | Huwise Explore API v2 |

Transport väljs med `DOA_MCP_TRANSPORT`. Standard är `stdio`. Med `http`
lyssnar servern på `DOA_MCP_HOST:DOA_MCP_PORT` och kräver en Bearer-token i
`DOA_MCP_API_KEY`. Saknas token startar servern inte.

### REST-API, Excel och QGIS

```bash
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Dokumentationen för alla endpoints ligger på `http://localhost:8000/docs`.

- **Power Query:** [EXCEL.md](EXCEL.md)
- **Excel-tillägget:** [excel-tillagg/README.md](excel-tillagg/README.md).
  Anvisningarna gäller Excel för Mac.
- **QGIS-pluginet:** [qgis-plugin/README.md](qgis-plugin/README.md)

## Datakvalitet

[DATAKVALITET.md](DATAKVALITET.md) samlar kända brister i källornas egna
data. Bristerna är mätta, inte antagna. Sviten redovisar en brist och döljer
den inte: poster som inte går att använda märks, och antalet följer med i
svaret. Hål fylls aldrig med gissade värden.

## Bidra

Bidrag är välkomna. Läs [CONTRIBUTING.md](CONTRIBUTING.md) innan du skickar
en pull request, och öppna gärna ett ärende först för större ändringar.
AI-kodagenter hittar sina anvisningar i [AGENTS.md](AGENTS.md).

## Licens

Koden är licensierad under **AGPL-3.0-or-later**.

Data omfattas av respektive källas villkor. Kartlagren är valda så att de går
att sprida vidare fritt:

| Lager | Källa | Licens |
|---|---|---|
| Kommun och län | SCB DeSO, sammanslagna per kodprefix | CC0 1.0 |
| DeSO och RegSO | SCB | CC0 1.0 |
| Kustlinje | EEA Coastline for Analysis v2.0 | CC BY 4.0 |
| Kommun och län klippta mot kust | Sammanslaget av raderna ovan | CC BY 4.0 |
| Valdistrikt | Valmyndigheten | Öppna data |
| Geokodade skolenheter | © OpenStreetMap-bidragsgivare via Nominatim | ODbL 1.0 |

Kartor som bygger på de kustklippta lagren eller på de geokodade
skolenheterna kräver källhänvisning. REST-API:et skickar därför med
källhänvisningen i sina GeoJSON-svar.
