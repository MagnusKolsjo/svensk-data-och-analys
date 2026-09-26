# AGENTS.md

Anvisningar för AI-kodagenter som arbetar i det här repot.

**Läs `CONTRIBUTING.md` innan du ändrar något.** Den innehåller reglerna för
språk, kod, MCP-verktyg, datahantering, kontroller, commit och licens. Det
som står här är det som oftast går fel.

## Projektet

svensk-data-och-analys är MCP-servrar, ett lokalt REST-API, ett
Excel-tillägg och ett QGIS-plugin för svensk officiell statistik, geodata och
öppna data. Statistik hämtas från källan vid varje fråga; lokalt lagras bara
geografiska indelningar, kartgränser, ett sökregister och ett katalogregister.
Se `README.md` för helheten.

| Katalog | Innehåll |
|---|---|
| `mcp_servrar/` | En fil per MCP-server |
| `data_och_analys/klienter/` | Klienter mot myndigheternas statistik-API:er |
| `data_och_analys/katalog_klienter/` | Katalogplattformar: EntryScape, CKAN, Huwise, ArcGIS Hub, DCAT, OGC |
| `data_och_analys/oppnadata/` | Sök och hämtning i organisationernas egna kataloger |
| `data_och_analys/geodata/` | Indelningar, geometrier, synk och bevakning |
| `data_och_analys/infra/` | Databas, transport, annotationer, mellanlager |
| `api/` | REST-API för Excel och Power Query |
| `cli/` | Synkar, kartläggning och kontroller |
| `db/` | SQL-scheman |

## Det som oftast går fel

- **Svenska** i kommentarer, namn, loggar, felmeddelanden och
  commitmeddelanden.
- **Gissa aldrig ett tabell-id, en variabelkod eller en adress.** Hämta dem
  ur källans metadata-API. Värden ur träningsdata är nästan alltid fel.
- **Initiera aldrig databasen vid import.** Allt sådant ligger i
  `if __name__ == "__main__":` via `starta()`.
- **Postgres och SQLite, stdio och http, är symmetriska val.** Skriv aldrig
  "primär" och "fallback".
- **Varje MCP-verktyg** ska ha `annotations`, `title` och en returannotation.
  Kör `python cli/granska_verktyg.py` efter ändringar; den ska avslutas utan
  anmärkningar.
- **Dölj inga brister.** Fyll inte hål i data med gissade värden och läs
  aldrig maskerade värden som nollor. Se `DATAKVALITET.md`.
- **Hemligheter** kommer ur `.env` med prefixet `DOA_`. Läs, skriv eller
  checka aldrig in `.env`.
- **Leverantörsoberoende texter.** Beskriv servrarna för "en MCP-klient",
  inte för en viss AI-produkt.
- **Ange dig inte som medförfattare.** Inga `Co-Authored-By`-rader eller
  motsvarande för AI-verktyg i commits eller pull requests.
