# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Framstegsrapportering för långkörande synkar — en söm, två avnämare.

Synkarna hämtar stora filer och skriver tiotusentals rader. Utan
rapportering är de tysta i minuter, och när klientens timeout slår till
vet varken modellen eller användaren om arbetet lyckades, misslyckades
eller hann halvvägs.

Två mekanismer kan bära den informationen, och vilken som är tillgänglig
beror på klienten:

    notifications/progress   Kärnprotokollet. Fungerar i varje klient som
                             skickar en progressToken, Claude Desktop
                             inräknad.
    Tasks-extensionen        Statusmeddelanden på en task. Kräver att
                             klienten deklarerat io.modelcontextprotocol/tasks.

Synkarna ska inte veta vilken som gäller. Därför tar de en `Rapportor` —
en enkel callback — och anropslagret kopplar in rätt adapter. En synk som
körs från CLI eller en daglig synk får `INGEN_RAPPORTOR` och märker
ingenting.

Rapportering är hjälpinformation, aldrig en felkälla: `skyddad()` sväljer
undantag från callbacken så att en trasig transport inte kan avbryta en
pågående databasskrivning.

Ingångspunkter:
    Rapportor            — typalias för callbacken
    INGEN_RAPPORTOR      — no-op, standardvärde i synkarna
    fran_context(ctx)    — väljer rätt adapter automatiskt
    aktiv_rapportor      — contextvar som taskkörningen sätter
    strypt(rapportor)    — begränsar anropsfrekvensen
    skyddad(rapportor)   — sväljer undantag från callbacken
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context

logger = logging.getLogger(__name__)

# (gjort, av, meddelande) — `av` är None när totalen inte är känd i förväg,
# vilket den ofta inte är: strömmande GeoJSON vet inte antalet features
# förrän filen är slut.
Rapportor = Callable[[int, int | None, str], Awaitable[None]]

# Minsta tid mellan två rapporter. En feature-loop kan snurra tiotusentals
# varv; utan strypning blir notiserna fler än datat de beskriver.
STANDARD_INTERVALL_SEKUNDER = 1.0


async def INGEN_RAPPORTOR(gjort: int, av: int | None, meddelande: str) -> None:
    """Standardvärde i synkarna — gör ingenting."""


# Sätts av taskkörningen. När den är satt kör verktyget i bakgrunden efter
# att dess ursprungliga anrop redan besvarats med ett task-handtag — då
# finns ingen levande request att skicka notiser på, och rapporterna ska
# i stället landa som statusmeddelanden på tasken.
aktiv_rapportor: contextvars.ContextVar[Rapportor | None] = contextvars.ContextVar(
    "doa_aktiv_rapportor", default=None
)


def fran_context(ctx: "Context | None") -> Rapportor:
    """Väljer rätt väg för framstegsrapporter — task eller notifikation.

    Ordningen är medveten. Kör verktyget inuti en task har `aktiv_rapportor`
    satts av taskkörningen, och den vinner: request-kontexten är redan
    besvarad och notiser på den skulle gå i tomma intet. I det vanliga
    synkrona fallet finns ingen aktiv task, och rapporterna går som
    `notifications/progress` på klientens progressToken.

    Returnerar `INGEN_RAPPORTOR` när varken task eller `ctx` finns, så samma
    anropskod fungerar när en synk körs fristående från CLI eller daglig synk.

    Verktygen ser aldrig skillnaden — de anropar den här funktionen och får
    en callback.
    """
    ur_task = aktiv_rapportor.get()
    if ur_task is not None:
        return ur_task

    if ctx is None:
        return INGEN_RAPPORTOR

    async def rapportera(gjort: int, av: int | None, meddelande: str) -> None:
        await ctx.report_progress(progress=gjort, total=av, message=meddelande)

    return skyddad(strypt(rapportera))


def strypt(
    rapportor: Rapportor, intervall: float = STANDARD_INTERVALL_SEKUNDER
) -> Rapportor:
    """Begränsar rapportfrekvensen till en per `intervall` sekunder.

    Första och sista rapporten släpps alltid igenom — den första för att
    arbetet ska synas direkt, den sista för att sluttillståndet inte ska
    tappas bort av en tidsgräns.
    """
    senast = 0.0
    forst = True

    async def rapportera(gjort: int, av: int | None, meddelande: str) -> None:
        nonlocal senast, forst
        nu = time.monotonic()
        slutrapport = av is not None and gjort >= av
        if forst or slutrapport or nu - senast >= intervall:
            forst = False
            senast = nu
            await rapportor(gjort, av, meddelande)

    return rapportera


def skyddad(rapportor: Rapportor) -> Rapportor:
    """Sväljer undantag från callbacken.

    En trasig eller stängd klientanslutning får inte avbryta en synk som
    står mitt i en databasskrivning — då vore rapporteringen farligare än
    tystnaden den ersatte.
    """

    async def rapportera(gjort: int, av: int | None, meddelande: str) -> None:
        try:
            await rapportor(gjort, av, meddelande)
        except Exception as fel:
            logger.debug("Framstegsrapport misslyckades: %s", fel)

    return rapportera
