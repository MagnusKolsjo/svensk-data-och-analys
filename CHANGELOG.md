# Changelog

Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.1.0/),
och versionerna följer [semantisk versionshantering](https://semver.org/lang/sv/).

## [Unreleased]

## [1.0.2] - 2026-11-19

### Säkerhet

- Adresser som hämtas på uppdrag av en anropare når bara publika nät.
  Privata nät, loopback, länklokala adresser och molnleverantörers
  metadatatjänster avvisas, även efter en omdirigering. Prövningen sitter
  i anslutningen, som går till den adress som prövades, så att en
  namnserver som byter svar inte kommer förbi. Egna interna värdar kan
  tillåtas med `DOA_TILLATNA_INTERNA_VARDAR`.
- Geo-synkarna godtar en adress i anropet bara om den är https och ligger
  hos källans egen myndighet. Adressen sparas som bevakad källa, så en
  främmande adress hade annars hämtats varje natt.
- ZIP-baserade filer (zip, xlsx, ods) avvisas om innehållet packas upp över
  1 GB.
- DCAT-filer med dokumenttypsdeklaration tolkas inte, JSON-LD-kontexter
  prövas innan rdflib hämtar dem, en katalogsida får vara högst 100 MB och
  nästa sida följs bara på katalogens egen värd.
- Kartläggningen för inte in plattformar som bara svarade utan
  certifikatkontroll i katalogregistret, och tar bort sådana som förts in
  tidigare. De redovisas fortfarande i rapporten.

### Rättat

- En ny årgång som synkats med en adress i verktygsanropet backades varje
  natt till den äldre adressen i `.env`. Adressen i `.env` vid synken
  sparas nu i `kallsignatur.env_url`, och den schemalagda synken byter
  källa bara när `.env` faktiskt ändrats.

## [1.0.1] - 2026-11-18

### Tillagt

- `CONTRIBUTING.md` med regler för bidrag, och `AGENTS.md` med anvisningar
  för AI-kodagenter.
- Varning när en nyckel står flera gånger i `.env` med olika värden. Det
  sista värdet gäller, så en tom rad längre ner släcker en ifylld utan att
  något märks. Varningen anger radnummer, aldrig värden.

### Ändrat

- Den schemalagda synken skiljer på en adressvariabel som saknas, som står
  tom och som skrivs över av en senare tom rad, i stället för att alltid be
  om att adressen ska anges.

## [1.0.0] - 2026-09-26

### Tillagt

- Tolv MCP-servrar för svensk officiell statistik, geodata och öppna data,
  över stdio eller HTTP med Bearer-token.
- Statistik från ett tjugotal myndigheter via PxWeb API v1 och PxWebApi 2,
  Kolada, Skolverket, Socialstyrelsen, Försäkringskassan, Brå, Trafikanalys,
  Kungliga biblioteket, Riksbanken och Migrationsverket.
- Geografiska indelningar lagrade lokalt: riket, NUTS 1–3, län och region,
  kommun, RegSO, DeSO, valkrets och valdistrikt, samt Tillväxtverkets och
  SKR:s kommunklassificeringar och postnummer.
- Fritt licensierade kartlager för län, kommun, RegSO, DeSO och valdistrikt,
  med kommun- och länsytor klippta mot EEA:s kustlinje, och skolenheter som
  punktlager.
- Karta som interaktiv widget i MCP-klienter som stöder MCP Apps, med
  geometri för fina indelningar hämtad på begäran.
- Sök och hämtning av öppna data från kommuner, regioner och myndigheter
  direkt i deras egna kataloger — EntryScape, CKAN, Huwise, ArcGIS Hub och
  DCAT-filer — med datamängder beskrivna enligt DCAT-AP-SE och tabeller som
  beskrivs, filtreras och aggregeras på serversidan.
- Katalogregister som byggs ur öppna källor: SKR:s förteckningar, SCB:s
  myndighetsregister, Statskontorets myndighetsinformation och Wikidata.
- Discovery via dataportal.se, Researchdata.se och Geodataportalen, och
  generella klienter för WFS, OGC API Features, ArcGIS REST och Huwise.
- Semantiskt sökindex över dataserier från sexton källor, med filter som
  begränsar varandra och markering av vad som går att lägga på karta.
- Lokalt REST-API för Power Query, ett Excel-tillägg med sidopanel och
  funktionerna `DOA.SOK`, `DOA.HAMTA`, `DOA.KOLADA` och `DOA.RIKSBANK`, och
  ett QGIS-plugin som ritar statistik som choroplet.
- Synkstatus och bevakning av lagrade datamängder, och en schemalagd daglig
  synk via launchd eller cron som hämtar om det källan bytt ut eller det
  som passerat sin uppdateringstakt.
- Mellanlagring av hämtade filer med konfigurerbar hållbarhet.

### Kända begränsningar

- Lantmäteriets administrativa indelning kräver avtal och ingår inte.
- PxWeb API v1 stöds av källorna till och med 2026-12-31.
- Trafikanalys upphör 2026-12-31 och uppgår i Tillväxtanalys.
- Kända brister i källornas egna data redovisas i `DATAKVALITET.md`.
