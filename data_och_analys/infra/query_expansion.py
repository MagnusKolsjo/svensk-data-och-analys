# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""LLM-baserad frågeutvidgning via OpenAI-kompatibelt API.

Utvidgar en sökfråga till relaterade termer och synonymer innan sökningen
körs — t.ex. "styrränta" → "policy rate", "reporänta"; "jobb" →
"sysselsättning", "förvärvsarbete". Det adresserar att myndigheter använder
inkonsekvent terminologi och att svenska vardagsord sällan matchar de
officiella variabelnamnen (eller engelska beskrivningar, som hos Riksbanken).

Plattformsoberoende: fungerar mot vilket OpenAI-kompatibelt chat-API som
helst (OpenAI, Ollama, LM Studio, vLLM). Konfiguration via .env:

    DOA_QUERY_EXPANSION_ENABLED   true för att aktivera (annars no-op)
    DOA_QUERY_EXPANSION_URL       bas-URL, t.ex. http://localhost:11434/v1
    DOA_QUERY_EXPANSION_MODEL     modellnamn
    DOA_QUERY_EXPANSION_PROMPT_FILE  promptfil med {query}-platshållare
    DOA_QUERY_EXPANSION_API_KEY   valfri nyckel (Bearer)

Fail-open: är funktionen avstängd, promptfilen saknas eller LLM:en inte
svarar returneras `[fraga]` oförändrat. Sökningen ska aldrig falla på att
utvidgningen inte är tillgänglig.

Ingångspunkter:
    aktiverad() -> bool
    expandera(fraga) -> list[str]
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def aktiverad() -> bool:
    """True om frågeutvidgning är påslagen i .env."""
    return os.getenv("DOA_QUERY_EXPANSION_ENABLED", "false").strip().lower() == "true"


def _projekt_rot() -> Path:
    return Path(__file__).resolve().parents[2]


def _las_prompt(fraga: str) -> str | None:
    """Läser promptfilen och fyller i {query}; None om filen saknas."""
    rel = os.getenv(
        "DOA_QUERY_EXPANSION_PROMPT_FILE", "prompts/expansion_prompt.txt"
    )
    fil = Path(rel)
    if not fil.is_absolute():
        fil = _projekt_rot() / rel
    if not fil.is_file():
        logger.warning("Promptfil saknas: %s — hoppar över utvidgning", fil)
        return None
    return fil.read_text(encoding="utf-8").replace("{query}", fraga)


def _slasamman(fraga: str, termer: list[str]) -> list[str]:
    """Lägger originalfrågan först och deduplicerar skiftlägesokänsligt."""
    resultat: list[str] = []
    sedda: set[str] = set()
    for t in [fraga, *termer]:
        t = t.strip()
        nyckel = t.casefold()
        if t and nyckel not in sedda:
            sedda.add(nyckel)
            resultat.append(t)
    return resultat


async def expandera(fraga: str) -> list[str]:
    """Returnerar `[fraga]` plus utvidgade termer (eller bara `[fraga]`).

    Fail-open: avstängd funktion, saknad promptfil eller ett LLM-fel ger
    `[fraga]` oförändrat.
    """
    if not aktiverad():
        return [fraga]

    prompt = _las_prompt(fraga)
    if prompt is None:
        return [fraga]

    bas_url = os.getenv("DOA_QUERY_EXPANSION_URL", "http://localhost:11434/v1")
    modell = os.getenv("DOA_QUERY_EXPANSION_MODEL", "llama3")
    api_nyckel = os.getenv("DOA_QUERY_EXPANSION_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if api_nyckel:
        headers["Authorization"] = f"Bearer {api_nyckel}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as klient:
            svar = await klient.post(
                f"{bas_url.rstrip('/')}/chat/completions",
                json={
                    "model": modell,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
            )
        svar.raise_for_status()
        innehall = svar.json()["choices"][0]["message"]["content"]
    except Exception as fel:  # noqa: BLE001
        logger.warning(
            "Frågeutvidgning misslyckades: %s — använder originalfrågan", fel
        )
        return [fraga]

    termer = [rad.strip("-•* \t") for rad in innehall.splitlines() if rad.strip()]
    return _slasamman(fraga, termer)
