# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Embeddings för semantisk sökning — KBLab svensk sentence-transformer.

Modellen `KBLab/sentence-bert-swedish-cased` (768 dim) är tränad för
semantisk likhet på svenska och valdes framför MLM-tränade alternativ
(t.ex. `bert-base-swedish-cased`) som ger sämre meningsembeddings. Modellen
överstyrs via `DOA_EMBEDDING_MODEL` i .env.

Modellen lazy-laddas vid första anrop — import av sentence-transformers och
torch är tungt och ska inte ske vid modulimport (det skulle blockera MCP-
servrars uppstart). I http-läget kan `forvarm()` anropas vid uppstart för
jämn svarstid.

Vektorerna L2-normaliseras, så cosinuslikhet blir en ren skalärprodukt och
matchar pgvector-operatorn `<=>` (cosine distance).

Ingångspunkter:
    embed_texter(texter) -> list[list[float]]
    embed_fraga(text) -> list[float]
    forvarm() -> None
    DIM
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

MODELL_NAMN = os.getenv("DOA_EMBEDDING_MODEL", "KBLab/sentence-bert-swedish-cased")
DIM = 768

_modell = None
_las = threading.Lock()


def _ladda():
    """Lazy-laddar (och cachar) sentence-transformer-modellen."""
    global _modell
    if _modell is None:
        with _las:
            if _modell is None:
                from sentence_transformers import SentenceTransformer

                logger.info("Laddar embedding-modell %s", MODELL_NAMN)
                _modell = SentenceTransformer(MODELL_NAMN)
    return _modell


def forvarm() -> None:
    """Laddar modellen i förväg (för http-uppstart med jämn svarstid)."""
    _ladda()


def embed_texter(texter: list[str]) -> list[list[float]]:
    """Embeddar en lista texter till L2-normaliserade 768-dim-vektorer."""
    if not texter:
        return []
    modell = _ladda()
    vektorer = modell.encode(
        texter, normalize_embeddings=True, show_progress_bar=False
    )
    return [v.tolist() for v in vektorer]


def embed_fraga(text: str) -> list[float]:
    """Embeddar en enskild fråga till en normaliserad vektor."""
    return embed_texter([text])[0]
