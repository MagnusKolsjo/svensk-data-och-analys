# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Projektkonfig — läser .env från projektroten.

Avsedd att anropas i MCP-servrarnas `__main__`-block innan
`db.initiera_schema()` körs. En MCP-klient som startas från skrivbordet
ärver inte din shell-miljö när den startar MCP-processer över stdio, så
`.env` måste läsas explicit — annars måste varje variabel dupliceras i
klientens egen `env`-konfiguration.

Filen söks i projektroten via modulens egen `__file__`, så det fungerar
oavsett vad CWD är när processen startar.

En nyckel som står flera gånger i filen får det sista värdet, i dotenv
likaväl som när bash läser filen med `source`. Ett tomt block som klistrats
in under ett ifyllt släcker alltså de ifyllda raderna utan att något
syns. `las_env` varnar därför för nycklar som står flera gånger med olika
värden. Varningen anger radnummer, aldrig värden — filen bär hemligheter.

Ingångspunkter:
    las_env(sokvag=None) -> Path | None
    dubbla_nycklar(fil) -> dict[str, list[tuple[int, bool]]]
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def _projekt_rot() -> Path:
    # konfig.py ligger i data_och_analys/infra/. Två steg upp = projektroten.
    return Path(__file__).resolve().parents[2]


def las_env(sokvag: Path | None = None) -> Path | None:
    """Läser projektets .env och returnerar filen som lästes (eller None).

    Standardplats är `<projektroten>/.env`. Sökvägen kan överstyras med
    `sokvag`. Tyst no-op om filen saknas — env-variabler kan ändå vara
    satta i parent-processens miljö (t.ex. MCP-klientens `env`-block).

    Idempotent — flera anrop skadar inte. Befintliga env-variabler
    skrivs inte över; .env fyller bara i det som saknas. Det är dotenvs
    standardbeteende och passar oss — klientens `env`-block kan
    fortfarande överstyra enskilda värden för felsökning.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv ej installerad — hoppar över .env-laddning")
        return None

    fil = sokvag or (_projekt_rot() / ".env")
    if not fil.is_file():
        logger.debug(".env saknas på %s — hoppar över", fil)
        return None

    load_dotenv(fil)
    logger.info("Läste .env från %s", fil)
    _varna_for_dubbla(fil)
    return fil


# ------------------------------------------------------------------------------
# Nycklar som står flera gånger
# ------------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _dubbla_cachade(fil: Path, mtime: float) -> dict[str, list[tuple[int, bool]]]:
    from dotenv.parser import parse_stream

    forekomster: dict[str, list[tuple[int, str]]] = {}
    with fil.open(encoding="utf-8") as f:
        for b in parse_stream(f):
            if b.key is None or b.error:
                continue
            forekomster.setdefault(b.key, []).append((b.original.line, (b.value or "").strip()))
    # Samma värde två gånger är slarv men ofarligt; bara olika värden kan
    # göra att det man skrivit inte gäller.
    return {
        nyckel: [(rad, bool(varde)) for rad, varde in lista]
        for nyckel, lista in forekomster.items()
        if len({varde for _, varde in lista}) > 1
    }


def dubbla_nycklar(fil: Path | None = None) -> dict[str, list[tuple[int, bool]]]:
    """Nycklar som står flera gånger i `.env` med olika värden.

    Ger `{nyckel: [(radnummer, har_värde), ...]}` i filens ordning; det
    sista elementet är det som gäller. Värdena returneras aldrig. Tom dict
    om filen saknas, inte går att tolka eller python-dotenv saknas.
    """
    fil = fil or (_projekt_rot() / ".env")
    try:
        return _dubbla_cachade(fil, fil.stat().st_mtime)
    except (OSError, ImportError, UnicodeDecodeError) as e:
        logger.debug("Kunde inte granska %s efter dubbla nycklar: %s", fil, e)
        return {}


def beskriv_dubbel(nyckel: str, forekomster: list[tuple[int, bool]]) -> str:
    """En rad som säger var nyckeln står och vilket värde som gäller."""
    rader = ", ".join(str(rad) for rad, _ in forekomster)
    sista_rad, sista_har_varde = forekomster[-1]
    if not sista_har_varde and any(har for _, har in forekomster[:-1]):
        return (f"{nyckel} står på rad {rader}; den tomma raden {sista_rad} "
                f"kommer sist och skriver över de ifyllda — ta bort den")
    return (f"{nyckel} står på rad {rader} med olika värden; "
            f"raden {sista_rad} kommer sist och är den som gäller")


def _varna_for_dubbla(fil: Path) -> None:
    for nyckel, forekomster in dubbla_nycklar(fil).items():
        logger.warning("%s: %s", fil.name, beskriv_dubbel(nyckel, forekomster))
