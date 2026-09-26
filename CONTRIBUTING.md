# Bidra till svensk-data-och-analys

Tack för att du vill bidra. Den här filen beskriver hur ett bidrag ska se ut
för att kunna tas in utan omtag. Den gäller lika för människor och för
AI-kodagenter som arbetar åt någon.

## Innan du börjar

- **Öppna ett ärende först** för allt som är större än en rättelse: en ny
  källa, en ny server, en ändrad arbetsgång. Då kan vi enas om vägen innan
  du lägger ner arbetet.
- **Läs `DATAKVALITET.md`** innan du utreder något som ser ut som ett fel.
  Många egendomligheter ligger i källornas egna data och är redan mätta
  och beskrivna.
- **Verifiera mot källan.** Tabell-id, variabelkoder och adresser hämtas ur
  källans metadata-API, aldrig ur minnet eller ur ett exempel på nätet. De
  ändras mellan årgångar.

## Språk

Svenska genomgående: kommentarer, docstrings, variabelnamn, loggmeddelanden,
felmeddelanden, verktygsbeskrivningar och commitmeddelanden. Undantaget är
namn som ägs av någon annan: protokollfält, API-parametrar, bibliotek.

Kommentarer förklarar **varför**, inte vad. Skriv inte interna
projektreferenser, arbetsfaser eller att-göra-listor i koden.

Texter som beskriver MCP-servrarna ska vara leverantörsoberoende. Skriv "en
MCP-klient" eller "en AI-assistent som stöder MCP", inte namnet på en viss
produkt, utom där texten handlar om just den produktens beteende.

## Kod

- Python 3.12 eller senare.
- **Nätverksanrop är asynkrona** och görs med `httpx`, inte `requests`.
- **Indata från externa API:er valideras med pydantic v2.** Undantaget är
  verktyg som släpper igenom ett externt svar orört; där ägs formen av
  källan och ska inte modelleras om.
- **PostgreSQL och SQLite är symmetriska val.** Kod som rör databasen ska
  fungera med båda, eller tydligt säga att den kräver PostGIS eller
  pgvector. Beskriv dem aldrig som primär och reserv. Detsamma gäller
  transporterna stdio och http.
- **Databasen initieras aldrig vid import.** Uppstarten ligger i
  `if __name__ == "__main__":` via `infra/mcp_transport.starta()`. En
  server ska gå att starta även när databasen är nere.
- **Hemligheter läses ur `.env`** med prefixet `DOA_` och hårdkodas aldrig.
  Nya variabler läggs in i `config.example.env` med en kommentar om vad de
  gör.
- **Explicita felmeddelanden på svenska** som säger vad som gick fel och vad
  användaren kan göra.
- **Filhuvud:** varje ny fil börjar med SPDX-rad, copyright och en docstring
  som anger syfte och ingångspunkter:

  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  # Copyright (C) 2026 <Ditt namn>
  """Vad modulen gör och varför.

  Ingångspunkter:
      funktion(argument) -> typ
  """
  ```

- **Logiska avsnitt** avgränsas med rubrikkommentarer på 78 streck.

## MCP-verktyg

Varje verktyg ska bära:

- **`annotations`** ur `data_och_analys/infra/mcp_annotationer.py`:
  `LASNING_EXTERN`, `LASNING_DB`, `SYNK` eller `SKRIVNING_DESTRUKTIV`.
- **`title`**, en kort svensk verbfras utan prefix ("Lista kommuner").
- **En returannotation** som ger ett meningsfullt `outputSchema`. `Any` ger
  inget schema alls.

Servern ska ha `instructions=` som beskriver arbetsordning, kodformat och
fällor i svarsstorlek. Det är den texten klienten läser innan den väljer
verktyg.

Tänk på svarsstorleken. Ett verktyg som kan returnera stora mängder ska kapa
och säga var det kapade, med `max_tecken` och `fran_tecken` eller med
paginering.

## Data

- **Hämta vid frågan, lagra bara det som inte går att hämta.** Statistik
  lagras inte lokalt. Det som lagras ska in i synkregistret med en angiven
  uppdateringstakt och grunden för den.
- **Brister redovisas, de döljs inte och gissas inte bort.** Värden som inte
  går att använda märks och räknas, och antalet följer med i svaret. En
  approximation mäts innan den används.
- **Kartlager ska gå att sprida fritt.** En ny geometrikälla väljs efter
  licens. Källor som förbjuder vidarespridning eller kommersiell användning
  tas inte in.

## Kontrollera innan du skickar

```bash
python cli/granska_verktyg.py
```

ska avslutas utan anmärkningar om du har ändrat i en MCP-server.

```bash
python excel-tillagg/kontrollera.py excel-tillagg
```

gäller ändringar i Excel-tillägget, och `python qgis-plugin/kontrollera.py`
gäller QGIS-pluginet.

Pröva ändringen mot den riktiga källan och beskriv i din pull request vad du
körde och vad det gav.

## Commit och pull request

- **Commitmeddelanden på svenska**, i imperativ: "Lägg till Statskontorets
  statistik", inte "Lade till" eller "Added".
- **En logisk förändring per commit.** Blanda inte en rättelse med en ny
  funktion.
- **CHANGELOG.md** får en rad under `[Unreleased]`, i rätt kategori enligt
  [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/).
- **Beskriv i pull requesten** vad som ändrats, varför, och hur det är
  prövat. Hänvisa till ärendet om det finns ett.

Repot på GitHub speglar de släppta versionerna. Sammanslagna bidrag förs in
i utvecklingen och följer med i nästa version, med dig som författare.

## AI-verktyg

Du får använda AI-verktyg när du skriver kod. Du som skickar in bidraget
ansvarar för det och ska ha läst och förstått varje rad. Ange inte ett
AI-verktyg som medförfattare, till exempel med `Co-Authored-By`. Ett verktyg
kan inte inneha upphovsrätt, och raden antyder ett anspråk som ingen har.

## Licens

Projektet är licensierat enligt GNU Affero General Public License, version 3
eller senare. Genom att skicka in ett bidrag godtar du att det licensieras
på samma villkor.
