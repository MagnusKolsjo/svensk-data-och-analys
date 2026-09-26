# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Geokodning av svenska adresser via Nominatim (OpenStreetMap).

Svenska myndighetsregister bär ofta adress men inte koordinat. Lantmäteriets
adressregister kräver avtal, och Googles villkor förbjuder att resultatet
lagras — Nominatim är den öppna vägen som går att använda i en AGPL-svit.

**Licens.** Resultatet härleds ur OpenStreetMap och är ODbL 1.0. Den som
publicerar en karta byggd på geokodade lägen måste attribuera; använd
`ATTRIBUTION`. Lagra alltid varifrån ett läge kommer, så att ODbL-material
går att skilja från källans egna koordinater.

**Villkor.** Nominatims användarpolicy tillåter högst ett anrop per sekund
från en tråd och kräver en identifierande User-Agent. `Geokodare` håller
takten själv; kringgå den inte.

**Postboxar går inte att geokoda.** "Box 11124, 10061 Stockholm" ger en
träff — på ett godtyckligt företag i Stockholm, som ser lika trovärdig ut
som en riktig. `ar_postbox()` fångar dem så att de kan redovisas som det de
är: en kvalitetsbrist i källans grundregistrering, inte ett tomt fält.

**Precisionen varierar och måste följa med.** En träff av typen `school`
är byggnaden; `residential` eller `tertiary` är gatan. Det förra duger för
att peka ut en skolgård, det senare inte.

Ingångspunkter:
    ATTRIBUTION
    Traff
    Geokodare(user_agent).slå_upp(adress) -> Traff | None
    ar_postbox(gata) -> bool
    bygg_adress(gata, postnr, ort) -> str | None
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BAS_URL = "https://nominatim.openstreetmap.org/search"
ATTRIBUTION = "Geokodning © OpenStreetMap-bidragsgivare, ODbL 1.0"

# Nominatims användarpolicy: ett anrop per sekund. Marginalen gör att en
# långsam klocka inte råkar överskrida taket.
PAUS_SEKUNDER = 1.1

_BOX = re.compile(r"^\s*(box|pl|package)\s*\.?\s*\d+", re.IGNORECASE)

# Nominatims träfftyper är engelska och många. De översätts till tre svenska
# precisionsnivåer, för det är den skillnad som avgör vad läget duger till.
#
# `ort` är den farliga: en träff på en tätort eller ett samhälle placerar
# punkten i ortens mitt, vilket är samma fälla som postnummerortens centrum.
# Den måste gå att filtrera bort.
_GATA = {
    "residential", "tertiary", "secondary", "primary", "trunk", "motorway",
    "unclassified", "living_street", "pedestrian", "service", "track",
    "road", "path", "footway", "cycleway",
}
_ORT = {
    "town", "city", "village", "hamlet", "locality", "suburb", "neighbourhood",
    "municipality", "county", "postcode", "administrative",
}

PRECISIONER = ("byggnad", "gata", "ort")


def _precision(typ: str) -> str:
    """Översätter Nominatims typ till svensk precisionsnivå.

    Allt som varken är en väg eller en ort behandlas som byggnad. Det täcker
    både `school` och verksamheter på adressen — `restaurant`, `library` —
    som geometriskt är rätt hus även när verksamheten är en annan.
    """
    t = (typ or "").lower()
    if t in _GATA:
        return "gata"
    if t in _ORT:
        return "ort"
    return "byggnad"


@dataclass(frozen=True)
class Traff:
    latitud: float
    longitud: float
    typ: str
    visningsnamn: str

    @property
    def precision(self) -> str:
        """`byggnad`, `gata` eller `ort` — avgör vad läget duger till.

        En ortsträff är hela samhällets mittpunkt och har samma fel som
        postnummerortens centrum. Den bör filtreras bort där lägena ska
        föreställa enskilda adresser.
        """
        return _precision(self.typ)


def ar_postbox(gata: str | None) -> bool:
    """Sant för en boxadress, som inte har någon plats att slå upp."""
    return bool(gata) and bool(_BOX.match(str(gata)))


def bygg_adress(
    gata: str | None, postnr: str | None = None, ort: str | None = None
) -> str | None:
    """Sätter ihop en sökbar adress. None när gatan saknas eller är en box."""
    g = str(gata or "").strip()
    if not g or ar_postbox(g):
        return None
    delar = [g, str(postnr or "").strip(), str(ort or "").strip()]
    return ", ".join(d for d in delar if d) + ", Sverige"


class Geokodare:
    """Håller anropstakten och en återanvänd HTTP-anslutning.

    Används som async-kontexthanterare så att anslutningen stängs:

        async with Geokodare("min-svit/1.0 (kontakt)") as g:
            traff = await g.sla_upp("Storgatan 1, 11122 Stockholm, Sverige")
    """

    def __init__(self, user_agent: str, land: str = "se") -> None:
        if not user_agent or "http" not in user_agent.lower():
            # Nominatim avvisar anonyma anrop. Kravet är en kontaktväg.
            logger.warning(
                "User-Agent bör innehålla en kontaktadress — Nominatim kan "
                "annars strypa eller blockera anropen."
            )
        self._user_agent = user_agent
        self._land = land
        self._klient: httpx.AsyncClient | None = None
        self._senast = 0.0

    async def __aenter__(self) -> "Geokodare":
        self._klient = httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": self._user_agent}
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._klient is not None:
            await self._klient.aclose()

    async def sla_upp(self, adress: str) -> Traff | None:
        """Slår upp en adress. None när Nominatim inte hittar något."""
        if self._klient is None:
            raise RuntimeError("Geokodare måste användas som async with")

        nu = asyncio.get_running_loop().time()
        vila = PAUS_SEKUNDER - (nu - self._senast)
        if vila > 0:
            await asyncio.sleep(vila)

        try:
            svar = await self._klient.get(
                BAS_URL,
                params={"q": adress, "format": "jsonv2", "limit": 1,
                        "countrycodes": self._land},
            )
        finally:
            self._senast = asyncio.get_running_loop().time()

        if svar.status_code != 200:
            logger.warning("Nominatim svarade %s för %r", svar.status_code, adress)
            return None
        traffar = svar.json() or []
        if not traffar:
            return None
        t = traffar[0]
        return Traff(
            latitud=float(t["lat"]),
            longitud=float(t["lon"]),
            typ=str(t.get("type") or t.get("category") or "okänd"),
            visningsnamn=str(t.get("display_name") or ""),
        )
