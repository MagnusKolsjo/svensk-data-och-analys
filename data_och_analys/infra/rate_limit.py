# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Per-myndighet rate limits.

Asynkron token-bucket per myndighet. Klienter anropar
`hamta_grind(myndighet).vanta()` före varje API-anrop. Takbegränsningen
slås upp i `RATE_LIMITS`; okänd myndighet får `STANDARD_GRANS` så att
nytillkomna källor inte oavsiktligt får obegränsad takt.

Gränserna är de offentligt dokumenterade taken hos respektive myndighet.
SCB:s 30 anrop / 10 sekunder gäller per IP-adress; Jordbruksverkets
1000 / 10 sekunder är generös men respekteras ändå för att inte ge
överbelastningstoppar vid stor chunkning.

Ingångspunkter:
    hamta_grind(myndighet) -> Grind
    Grind.vanta() -> None  (asynkront)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Format: (antal_anrop, fonster_sekunder)
RATE_LIMITS: dict[str, tuple[int, float]] = {
    # SCB:s dokumenterade tak. Gäller även PxWebApi 2.
    "scb": (30, 10.0),
    "jordbruksverket": (1000, 10.0),
    "folkhalsomyndigheten": (30, 10.0),
    "konjunkturinstitutet": (10, 10.0),
    "ki_prognos": (10, 10.0),
    "csn": (30, 10.0),
    "energimyndigheten": (30, 10.0),
    "skogsstyrelsen": (30, 10.0),
    "medlingsinstitutet": (30, 10.0),
    "slu": (30, 10.0),
    # Uppmätt, inte dokumenterat: 5 anrop/s ger 429 redan efter tio anrop,
    # 1 anrop/s går rent. Det tidigare taket på 30/10s var en gissning och
    # gjorde att 41 av 48 tabeller felade i ett metadatapass.
    "tillvaxtanalys": (10, 10.0),
    "kolada": (60, 10.0),
    # Riksbanken SWEA, Försäkringskassan och KB Bibstat — egna öppna API:er
    "riksbank": (30, 10.0),
    "forsakringskassan": (30, 10.0),
    "kb_bibstat": (30, 10.0),
    # Skolverket, Trafikanalys och Socialstyrelsen publicerar inga
    # dokumenterade tak — konservativa gränser hålls för att inte ge
    # överbelastningstoppar mot tjänster utan känd kapacitet.
    # Migrationsverket har inget API utan levererar Excelfiler från sin
    # webbplats. Ingen dokumenterad gräns; en fil i sekunden är långt under
    # vad en vanlig besökare genererar.
    "migrationsverket": (10, 10.0),
    "skolverket": (30, 10.0),
    "trafa": (30, 10.0),
    "socialstyrelsen": (30, 10.0),
    # Brå skrapas via den sessionsbaserade SolWebb-appen — håll takten låg
    # eftersom varje datapunkt kostar en hel sökrunda mot deras server.
    "bra": (6, 10.0),
}

# Konservativt tak när myndigheten inte finns i katalogen
STANDARD_GRANS: tuple[int, float] = (10, 10.0)


@dataclass
class Grind:
    """Asynkron token-bucket för en myndighet.

    Trådsäker inom samma event-loop via en intern asyncio.Lock. Inte
    avsedd att delas över processer — varje MCP-server håller sin egen
    instans, vilket är rätt: gränserna är per klient mot källan, inte
    per användare av MCP-servern.
    """

    namn: str
    antal_per_fonster: int
    fonster_sekunder: float
    _tidsstamplar: list[float] = field(default_factory=list)
    _las: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def vanta(self) -> None:
        """Blockerar tills ett nytt anrop ryms inom fönstret."""
        async with self._las:
            while True:
                nu = time.monotonic()
                grans = nu - self.fonster_sekunder
                self._tidsstamplar = [t for t in self._tidsstamplar if t > grans]
                if len(self._tidsstamplar) < self.antal_per_fonster:
                    self._tidsstamplar.append(nu)
                    return
                # Sov tills äldsta tidsstämpeln rullar ut ur fönstret.
                # +0.01s marginal mot klockflakighet.
                vila = self._tidsstamplar[0] + self.fonster_sekunder - nu + 0.01
                await asyncio.sleep(max(vila, 0.01))


_grindar: dict[str, Grind] = {}


def hamta_grind(myndighet: str) -> Grind:
    """Returnerar (och cachar) en Grind för en myndighet.

    Okänd myndighet får STANDARD_GRANS. Slå upp första gången skapar
    instansen; därefter återanvänds samma objekt så token-bucketen
    faktiskt fungerar mellan anrop.
    """
    if myndighet not in _grindar:
        antal, fonster = RATE_LIMITS.get(myndighet, STANDARD_GRANS)
        _grindar[myndighet] = Grind(myndighet, antal, fonster)
    return _grindar[myndighet]
