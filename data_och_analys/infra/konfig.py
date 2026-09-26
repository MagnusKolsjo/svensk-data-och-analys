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

Ingångspunkter:
    las_env(sokvag=None) -> Path | None
"""

from __future__ import annotations

import logging
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
    return fil
