# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Avgör vilka poster i sökindexet som är geografiskt indelade.

Sökindexet blandar 18 763 poster från tretton källor. Utan att veta vilka
som går att lägga på karta blir kartarbetet trial and error: man väljer en
träff, klickar visa, och får först då veta att tabellen saknar regiondimension.

Modulen fyller därför `sok_index.fasetter` med ett `geo`-block:

    {"nivaer": ["kommun", "lan"], "fran_ar": 2000, "till_ar": 2025,
     "dim": "Region"}

Poster utan geografisk indelning får inget `geo` alls, vilket gör
`/sok?geografisk=true` till ett indexvillkor i stället för en rundtur per
träff.

Nivåerna är suitens egna namn, inte källornas: kommun, lan, riket, deso,
regso, valdistrikt och punkt. `punkt` är enheter med koordinater —
skolenheter och liknande — som hör hemma i ett punktlager, aldrig i en
choropleth.

Varje källa avgörs på sitt eget sätt; se `upplosare.py`. Det som är
gemensamt ligger här: hur en kodmängd översätts till nivåer, och hur
resultatet skrivs.

Ingångspunkter:
    NIVAER
    klassa_koder(koder) -> list[str]
    skriv_geo(anslutning, id, geo) -> None
    posterna_utan_geo(anslutning, kalla, bara_nya) -> list[dict]
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

# Suitens nivånamn. Ordningen är från grov till fin och används när en
# nivålista visas för användaren.
NIVAER = ("riket", "lan", "kommun", "regso", "deso", "valdistrikt", "punkt")

# Rikstotalen har flera skepnader hos olika myndigheter.
_RIKET = {"00", "0", "riket", "sverige", "hela riket", "hela landet", "se"}

_DESO = re.compile(r"^\d{4}[A-C]\d{4}$")        # 0180C1390
_REGSO = re.compile(r"^\d{4}R\d{3}$")           # 0114R001
_VALDISTRIKT = re.compile(r"^\d{8}$")           # 01800101
_KOMMUN = re.compile(r"^\d{4}$")                # 0180
_LAN = re.compile(r"^\d{2}$")                   # 01

# Jordbruksverket skriver länskoden onollad: "1" för Stockholm, "25" för
# Norrbotten. En ren siffermatchning hade missat dem, och en generös
# "1-2 siffror = län" hade tagit med vad som helst. Listan över faktiska
# länskoder är kort och stabil, så den får avgöra.
_LANSKODER = {1, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14,
              17, 18, 19, 20, 21, 22, 23, 24, 25}


def klassa_koder(koder: Iterable[str]) -> list[str]:
    """Översätter en mängd regionkoder till suitens nivånamn.

    Klassningen går på kodens form, inte på etiketten. Etiketterna varierar
    mellan myndigheter och årgångar ("Stockholms län", "Stockholm county",
    "01 Stockholm"), medan koderna följer SCB:s standard hos alla källor som
    över huvud taget är kartbara mot suitens geometrier.

    En kod som inte matchar någon form ignoreras. Det är avsiktligt: en
    myndighet med egna trafikområden eller sjukvårdsregioner har data som
    inte går att lägga på suitens kartlager, och då ska nivån inte påstås
    finnas.
    """
    funna: set[str] = set()
    for rå in koder:
        kod = str(rå).strip()
        if not kod:
            continue
        if kod.lower() in _RIKET:
            funna.add("riket")
        elif _DESO.match(kod):
            funna.add("deso")
        elif _REGSO.match(kod):
            funna.add("regso")
        elif _VALDISTRIKT.match(kod):
            funna.add("valdistrikt")
        elif _KOMMUN.match(kod):
            funna.add("kommun")
        elif _LAN.match(kod) and int(kod) in _LANSKODER:
            funna.add("lan")
        elif kod.isdigit() and len(kod) == 1 and int(kod) in _LANSKODER:
            funna.add("lan")
    return [n for n in NIVAER if n in funna]


def bygg_geo(
    koder: Iterable[str] | None = None,
    nivaer: Iterable[str] | None = None,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    dim: str | None = None,
) -> dict[str, Any] | None:
    """Bygger geo-blocket. Returnerar None när inget kartbart finns.

    Antingen `koder` (som klassas) eller `nivaer` (redan kända, t.ex. för
    Kolada där alla KPI:er är kommunindelade per konstruktion).
    """
    lista = list(nivaer) if nivaer is not None else klassa_koder(koder or [])
    if not lista:
        return None
    geo: dict[str, Any] = {"nivaer": [n for n in NIVAER if n in set(lista)]}
    if fran_ar is not None:
        geo["fran_ar"] = fran_ar
    if till_ar is not None:
        geo["till_ar"] = till_ar
    if dim:
        geo["dim"] = dim
    return geo


def ar_ur_period(period: str | None) -> int | None:
    """Plockar årtalet ur SCB:s periodformat: 2015, 2015M04, 2015K1, 2015W03."""
    if not period:
        return None
    m = re.match(r"^(\d{4})", str(period))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Skrivning mot indexet
# ---------------------------------------------------------------------------
# `fasetter` bär redan {"amne": ...}. Geo-blocket läggs bredvid utan att röra
# det som finns — ämnesfasetten sätts av indexbygget och ägs av det.

async def skriv_geo(anslutning, post_id: int, geo: dict[str, Any] | None) -> None:
    """Sätter eller tar bort geo-blocket, och markerar posten som undersökt.

    `None` tar bort blocket i stället för att skriva "inget" — en post som
    en gång var geografisk och slutat vara det ska inte behålla en gammal
    nivålista.

    `geo_undersokt` sätts oavsett utfall. Utan den markeringen ser en post
    som undersökts och befunnits icke-geografisk likadan ut som en som
    aldrig undersökts, och påfyllnadsläget hade gjort om de fem tusen
    icke-geografiska posterna vid varje körning.
    """
    if geo is None:
        await anslutning.execute(
            "UPDATE sok_index "
            "SET fasetter = (coalesce(fasetter, '{}'::jsonb) - 'geo') "
            "               || '{\"geo_undersokt\": true}'::jsonb "
            "WHERE id = $1",
            post_id,
        )
    else:
        await anslutning.execute(
            "UPDATE sok_index "
            "SET fasetter = coalesce(fasetter, '{}'::jsonb) "
            "               || jsonb_build_object('geo', $2::jsonb, "
            "                                     'geo_undersokt', true) "
            "WHERE id = $1",
            post_id, json.dumps(geo),
        )


async def poster(
    anslutning, kalla: str, bara_nya: bool = True
) -> list[dict[str, Any]]:
    """Hämtar posterna för en källa som ska undersökas.

    `bara_nya` hoppar över dem som redan har ett geo-block. Det är läget för
    påfyllnad: en ny dataserie som dykt upp i indexet undersöks, resten står
    orörd. Ett fullt omtag körs med `bara_nya=False` när en källa lagt om
    sitt API.
    """
    villkor = "kalla = $1"
    if bara_nya:
        villkor += " AND NOT (fasetter ? 'geo_undersokt')"
    rader = await anslutning.fetch(
        f"SELECT id, kalla_id, namn, hamta_api, fasetter "
        f"FROM sok_index WHERE {villkor} ORDER BY id",
        kalla,
    )
    return [dict(r) for r in rader]
