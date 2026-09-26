# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Vad suiten lagrar lokalt, varför, och hur ofta det behöver synkas om.

Lagrad data är ett löfte om färskhet som någon måste hålla. Utan ett register
går det inte att svara på om en tabell är aktuell — man ser antalet rader och
antar att de stämmer.

Varje post bär `kadens_dagar` och `kadens_grund`. Grunden är det viktiga:
`dokumenterad` betyder att källan själv anger sin uppdateringstakt,
`uppmätt` att vi mätt den i datat, `handelsestyrd` att den inte har någon
takt utan följer val eller reformer. En gissning ska stå som `antagen` och
märkas ut, inte se ut som fakta.

Regeln för vad som får ligga lokalt: bara det som inte går att hämta när
frågan ställs. Geometri, och serier som källan inte kan leverera i den form
vi behöver. Statistik hämtas annars via respektive myndighets API.

Ingångspunkter:
    REGISTER
    Dataset
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dataset:
    tabell: str
    kalla: str
    verktyg: str
    kadens_dagar: int | None       # None = ändras bara vid reform
    kadens_grund: str              # dokumenterad | uppmätt | händelsestyrd | antagen
    varfor_lokalt: str


REGISTER: dict[str, Dataset] = {
    # --- Administrativ indelning ------------------------------------------
    # Kommun- och länsindelningen ändras vid årsskiften och mycket sällan.
    # Senaste kommunreformen var Knivstas utbrytning 2003.
    "kommun": Dataset(
        "kommun", "Tillväxtverkets kommuntypsfil",
        "geo_synka_tillvaxtverket_kommuntyper", 365, "händelsestyrd",
        "Grundtabell som allt annat joinar mot; ändras vid årsskiften."),
    "lan": Dataset(
        "lan", "Tillväxtverkets kommuntypsfil",
        "geo_synka_tillvaxtverket_kommuntyper", 365, "händelsestyrd",
        "Samma fil som kommun."),
    "region": Dataset(
        "region", "hårdkodad katalog i indelningar.py",
        "geo_synka_regioner", None, "händelsestyrd",
        "21 regioner; ändras vid namnbyten eller reform."),

    # --- Klassificeringar --------------------------------------------------
    # SKR reviderade sin kommungruppsindelning 2005, 2011 och 2017 — ungefär
    # vart sjätte år. Tillväxtverkets FA-regioner revideras i samma takt.
    "skr_kommungrupp": Dataset(
        "kommun_klassificering", "SKR", "geo_synka_skr_kommungrupp",
        None, "händelsestyrd",
        "Reviderad 2005, 2011, 2017 — cirka vart sjätte år."),
    "tillvaxtverket_fa": Dataset(
        "kommun_klassificering", "Tillväxtverket",
        "geo_synka_tillvaxtverket_fa_region", None, "händelsestyrd",
        "FA-regioner revideras i takt med pendlingsmönster, inte kalender."),
    # Samma synk fyller kommun- och läntabellerna och den här
    # klassificeringen. Två dataset ur en källa, och de skulle kunna
    # revideras var för sig — kommunindelningen vid ett årsskifte,
    # kommuntyperna när Tillväxtverket gör om sin gruppering.
    "tillvaxtverket_kommuntyper": Dataset(
        "kommun_klassificering", "Tillväxtverket",
        "geo_synka_tillvaxtverket_kommuntyper", None, "händelsestyrd",
        "SL3 och SL6 — grov och fin kommuntypsindelning, 290 rader vardera."),
    "nuts": Dataset(
        "nuts", "Eurostat NUTS", "geo_synka_nuts", None, "dokumenterad",
        "Eurostat reviderar NUTS vart tredje år: 2013, 2016, 2021, 2024."),

    # --- Statistiska indelningsområden -------------------------------------
    "deso": Dataset(
        "deso", "SCB", "geo_synka_deso_och_regso", None, "dokumenterad",
        "DeSO är fast sedan 2018 och avsedd att ligga still över tid."),
    "regso": Dataset(
        "regso", "SCB", "geo_synka_deso_och_regso", None, "dokumenterad",
        "RegSO är fast sedan 2020."),

    # --- Val ---------------------------------------------------------------
    "valdistrikt": Dataset(
        "valdistrikt", "val.se", "geo_synka_valdistrikt", None, "händelsestyrd",
        "Distriktsindelningen görs om inför varje val."),
    "valkrets": Dataset(
        "valkrets", "val.se", "geo_synka_valkretsar", None, "händelsestyrd",
        "Samma som valdistrikt."),

    # --- Serier som källan inte kan leverera on demand ---------------------
    "kommun_folkmangd": Dataset(
        "kommun_folkmangd", "SCB:s historiska publikation",
        "geo_synka_kommun_folkmangd", 365, "dokumenterad",
        "1950-2024 omräknat till EN kommunindelning. PxWeb ger varje år i "
        "det årets gränser och kan inte svara med serien. SCB publicerar "
        "en ny årgång varje år."),

    # --- Punktlager --------------------------------------------------------
    # Uppmätt på 80 aktiva enheter: en massomvalidering i maj-juni (65 % av
    # posterna) och därefter löpande ändringar på cirka 10 % per månad.
    "skolenhet_punkt": Dataset(
        "skolenhet_punkt", "Skolverkets skolenhetsregister",
        "cli/synka_skolenheter.py", 30, "uppmätt",
        "Koordinaterna finns bara i detalj-API:et, ett anrop per enhet — "
        "6 700 anrop går inte att göra vid frågetillfället. Attributen "
        "följer med i samma svar och behövs vid rendering."),

    # --- Geometri ----------------------------------------------------------
    "kustlinje_geom": Dataset(
        "kustlinje_geom", "EEA coastline for analysis v2.0",
        "cli/synka_kustlinje.py", None, "händelsestyrd",
        "EEA släpper nya versioner med flera års mellanrum."),
    "deso_geom": Dataset(
        "deso_geom", "SCB WFS", "geo_synka_deso_geom", None, "dokumenterad",
        "Följer deso-indelningen, som ligger still."),
    "regso_geom": Dataset(
        "regso_geom", "SCB WFS", "geo_synka_regso_geom", None, "dokumenterad",
        "Följer regso-indelningen."),
    "valdistrikt_geom": Dataset(
        "valdistrikt_geom", "val.se", "geo_synka_valdistrikt_geom",
        None, "händelsestyrd", "Görs om inför varje val."),

    # --- Härledda lager ----------------------------------------------------
    # Matvyerna räknas om ur deso_geom och kustlinje_geom; de har ingen egen
    # källa och blir inaktuella först när någon av de två gör det.
    # Uppmätt 2026-09-25: 18 870 lagrade mot 18 887 i källan, 17 tillkomna
    # och noll försvunna sedan synken — 0,09 % drift. Datasetets `modified`
    # speglar faktiska dataändringar och stod på 2026-09-09, alltså 16 dagar
    # gammalt. Postnummer tillkommer, försvinner nästan aldrig, och ett halvår
    # ger på den takten under en procents avvikelse.
    "postnummer": Dataset(
        "postnummer", "Geonames via Huwise", "geo_synka_postnummer",
        180, "uppmätt",
        "Punktkoordinater per postnummerort. PostNord äger polygongränserna "
        "och de finns inte i öppna data."),

    # --- Sökindex ----------------------------------------------------------
    # Uppmätt 2026-09-25 mot ett index som var 109 dagar gammalt: SCB hade
    # 69 nya och 19 borttagna tabeller (2,1 % drift), Kolada 49 nya och
    # 310 borttagna KPI:er (5,9 %). Kolada sätter takten, och 310 döda
    # träffar av 6 124 är för mycket. 60 dagar håller den värsta källan
    # under tre procent.
    # --- Katalogregistret ---------------------------------------------------
    # Vilken katalog varje kommun, region och myndighet har. Ingen takt är
    # uppmätt än: kartläggningen journalför hur många kataloger som tillkommit,
    # försvunnit och ändrats vid varje körning, och takten sätts om när det
    # finns några körningar att jämföra.
    "datakatalog": Dataset(
        "datakatalog", "organisationernas egna kataloger och dataportal.se",
        "cli/kartlagg_datadelning.py", 30, "antagen",
        "Kartläggningen prövar hundratals organisationers webbplatser och "
        "tar en kvart — den kan inte göras vid frågetillfället."),

    "sok_index": Dataset(
        "sok_index", "alla myndighetskataloger", "POST /sok/bygg-index",
        60, "uppmätt",
        "Discovery-lagret. Nya poster saknar geo-fasetter tills "
        "cli/synka_geo_fasetter.py körts."),
}
