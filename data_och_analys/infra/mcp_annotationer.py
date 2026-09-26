# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Verktygsannotationer för doa-suiten — fyra klasser, inte 124 varianter.

MCP:s fyra hintar (readOnlyHint, destructiveHint, idempotentHint,
openWorldHint) är hintar till klienten, inte garantier: klienten
använder dem för att avgöra vad som får köras utan att fråga användaren.
Utan dem är ett verktyg som skriver till databasen omöjligt att skilja
från ett som bara läser.

Varje verktyg i suiten faller i exakt en av fyra klasser:

    LASNING_EXTERN  Hämtar från ett myndighets-API över nätet. Öppen
                    värld — SCB kan publicera ny data när som helst, så
                    ett upprepat anrop kan ge ett annat svar.
    LASNING_DB      Läser ur suitens egen Postgres/SQLite. Sluten värld —
                    innehållet ändras bara av våra egna synkar.
    SYNK            Skriver till databasen. Idempotent upsert på naturlig
                    nyckel: samma anrop två gånger ger samma sluttillstånd.
                    Icke-destruktivt — befintliga rader uppdateras, aldrig
                    raderas.
    SKRIVNING_DESTRUKTIV
                    Reserverad. Inget verktyg är destruktivt i dag;
                    klassen finns så att den som inför ett raderande
                    verktyg tvingas välja den medvetet i stället för att
                    återanvända SYNK.

`openWorldHint` är den distinktion som betyder något i praktiken: den
skiljer ett anrop vars svar kan ändras utanför vår kontroll från ett som
bara ändras när vi själva synkar.

Modulen håller också suitens cachningshintar, av samma skäl: de ska vara
lika i alla elva servrar och beskrivas på ett ställe.

Ingångspunkter:
    LASNING_EXTERN, LASNING_DB, SYNK, SKRIVNING_DESTRUKTIV
    CACHE_HINTAR
"""

from __future__ import annotations

from mcp.server.caching import CacheHint
from mcp.types import ToolAnnotations

LASNING_EXTERN = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

LASNING_DB = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

# Synkarna upsertar mot naturlig nyckel. Att köra om en synk är säkert,
# och det ska synas för klienten — annars behandlas de som destruktiva.
SYNK = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

SKRIVNING_DESTRUKTIV = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


# ==============================================================================
# Cachningshintar — protokollrevision 2026-07-28
# ==============================================================================
#
# Utan hintar svarar servern ttlMs=0, alltså "omedelbart inaktuell", och
# klienten hämtar om verktygslistan vid varje behov. Suitens 124
# verktygsdefinitioner ändras bara när koden deployas, så en timme är
# gott om marginal — en deploy kräver ändå omstart av klienten.
#
# `public` är korrekt här: listorna innehåller inga användarspecifika
# data och varierar inte med anroparens behörighet. Om ett verktyg
# någon gång börjar filtreras per användare måste hinten bli `private`.

CACHE_HINTAR = {
    "tools/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "prompts/list": CacheHint(ttl_ms=3_600_000, scope="public"),
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
}
