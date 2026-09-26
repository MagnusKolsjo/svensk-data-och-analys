# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Gemensam paginerings- och svarsstorlekshantering för doa-suiten.

Claude Desktop kapar ett verktygssvar vid ungefär 150 000 tecken. Den
verkliga marginalen är hälften av det: SDK:t skickar ett listsvar både
som textblock och som `structuredContent`, så varje rad räknas två
gånger. Frågor om kommunal data eller longitudinella tidsserier
returnerar naturligt hundratals eller tusentals rader — 312 enheter ×
11 år × 3 kön tippar över taket även om varje enskild rad är liten.

Den här modulen kodifierar ett gemensamt kontrakt för alla MCP-verktyg
i doa-suiten som kan returnera variabelt stora svar:

    Paket {
      total:        int    — totala antalet rader efter filtrering
      sida:         int    — 1-baserad sidnummer
      per_sida:     int    — antalet rader per sida
      antal_sidor:  int    — ceildiv(total, per_sida)
      datapunkter:  list[T] — radens platta form
    }

Standardstorlek 500 rader per sida × ~150 bytes ≈ 75 KB, som med
dubbleringen blir ~150 KB — precis vid taket. Sänk `per_sida` hellre än
att lita på marginalen. Verktyg som naturligt har större rader (t.ex.
GeoJSON med polygoner) använder ett mindre tak via samma
`paginera`-funktion.

Två olika situationer, två olika funktioner:

    paginera()               hela träffmängden finns i minnet och ska
                             slicas här
    paketera_forpaginerat()  klienten har redan gjort LIMIT/OFFSET i SQL
                             och känner bara totalen

Att blanda ihop dem ger tyst fel data — se respektive docstring.

Fältnamnen är medvetet svenska: `sida`, `per_sida`, `antal_sidor`,
`datapunkter` — matchar resten av suiten. Engelska aliaser läggs aldrig
på paket-svaren; de är ett internt MCP-kontrakt.

Användning i en MCP-tool:

    from data_och_analys.infra.paginering import paginera, STANDARD_PER_SIDA

    @mcp.tool()
    async def nagot_verktyg(..., sida: int = 1, per_sida: int = STANDARD_PER_SIDA):
        rader = await _hamta_rader(...)
        return paginera(rader, sida=sida, per_sida=per_sida)

Ingångspunkter:
    paginera(rader, sida, per_sida) -> dict
    paketera_forpaginerat(rader, total, sida, per_sida) -> dict
    STANDARD_PER_SIDA          -> 500
    STANDARD_PER_SIDA_GEOJSON  -> 50 (för polygontunga svar)
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Paket(BaseModel, Generic[T]):
    """Pagineringskuvertet som typad modell.

    `paginera()` och `paketera_forpaginerat()` bygger samma form som
    dict. Den här modellen används som returannotation i MCP-verktyg:
    `Paket[Kommun]` ger klienten ett fältnamngivet `outputSchema` i
    stället för "en array av vad som helst", och gör att svaret levereras
    som ett content-block i stället för ett per rad.

    Konstrueras enklast ur dict-varianten:

        return Paket[Kommun](**paginering.paginera(rader, sida, per_sida))

    Wire-formatet är oförändrat — Pydantic serialiserar till samma JSON.
    """

    total: int = Field(description="Totalt antal rader efter filtrering")
    sida: int = Field(description="1-baserat sidnummer")
    per_sida: int = Field(description="Antal rader per sida")
    antal_sidor: int = Field(description="ceildiv(total, per_sida), minst 1")
    datapunkter: list[T] = Field(description="Radens platta form")

# 500 platta rader × ~150 bytes = ~75 KB med MCP-protokolloverhead
# klart under 1 MB-cap. Höj per-fall om radens datatyp är garanterat
# liten; sänk för GeoJSON med polygoner eller andra tunga payloads.
STANDARD_PER_SIDA = 500

# GeoJSON-features med polygongeometri kan vara 5-50 KB per feature.
# 50 features × 20 KB ≈ 1 MB — sätt taket lågt och låt användaren
# explicit höja om de vet att geometrin är enkel.
STANDARD_PER_SIDA_GEOJSON = 50


def paginera(
    rader: list[Any], sida: int = 1, per_sida: int = STANDARD_PER_SIDA
) -> dict[str, Any]:
    """Paginerar en lista till ett MCP-säkert svarspaket.

    `sida` är 1-baserad. Värden under 1 normaliseras till 1.
    `per_sida` under 1 normaliseras till `STANDARD_PER_SIDA`.

    Tomma listor returnerar `antal_sidor=1` (inte 0) — så att
    `sida=1 av 1` är giltig även när inget hittades.
    """
    total = len(rader)
    if per_sida < 1:
        per_sida = STANDARD_PER_SIDA
    if sida < 1:
        sida = 1
    start = (sida - 1) * per_sida
    slut = start + per_sida
    return _paket(rader[start:slut], total=total, sida=sida, per_sida=per_sida)


def paketera_forpaginerat(
    rader: list[Any], total: int, sida: int = 1, per_sida: int = STANDARD_PER_SIDA
) -> dict[str, Any]:
    """Bygger paketet när sidan redan slicats någon annanstans.

    Klienter som gör LIMIT/OFFSET i SQL har
    redan rätt rader men känner bara totalen. Att skicka en sådan lista
    till `paginera()` med rätt `sida` skulle slica bort allt — därför
    den här funktionen i stället.

    `rader` är den färdiga sidan, `total` antalet rader före sidindelning.
    """
    if per_sida < 1:
        per_sida = STANDARD_PER_SIDA
    if sida < 1:
        sida = 1
    return _paket(rader, total=total, sida=sida, per_sida=per_sida)


def _paket(
    datapunkter: list[Any], total: int, sida: int, per_sida: int
) -> dict[str, Any]:
    """Gemensam paketform — enda stället där antal_sidor räknas."""
    return {
        "total": total,
        "sida": sida,
        "per_sida": per_sida,
        "antal_sidor": max(1, -(-total // per_sida)),  # ceildiv, minst 1
        "datapunkter": datapunkter,
    }
