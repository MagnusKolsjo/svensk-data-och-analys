# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för EntryScape — katalog och rowstore.

EntryScape är den vanligaste katalogplattformen bland svenska kommuner och
myndigheter. Varje instans har en egen EntryStore under `/store/`, och
klienten talar med den direkt. EntryScapes samlade API på
api.entryscape.com når bara de instanser dess drift har släppt in; den
egna storen finns hos alla.

Tre saker att veta:

- Fritext söks i Solr-fältet `all`. Svenska sammansättningar kräver
  trunkering — `leverantör*` träffar "leverantörsfakturor", `leverantör`
  gör det inte — så varje ord får en asterisk.
- En instans kan delas av många utgivare: EntryScape Free och
  dataportalens registreringstjänst. Sökningen avgränsas då med
  `kontext`, EntryStores behållare för en katalog.
- Datamängdens metadata hämtas rekursivt (`?recursive=dcat`). Då kommer
  distributioner och datatjänster med i samma RDF/JSON-graf.

Rowstore gör en uppladdad CSV till ett API. `/info` ger kolumner och antal
rader; `/json` tar filter som `kolumn=värde` eller ett reguljärt uttryck.

Ingångspunkter:
    sok(bas_url, fraga, kontext="", limit=20, offset=0) -> dict
    hamta_datamangd(bas_url, kontext, post) -> Datamangd
    rowstore_info(url) -> dict
    rowstore_rader(url, filter=None, limit=100, offset=0) -> dict
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from data_och_analys.oppnadata import dcat_modell

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/EntryScapeClient (DCAT-AP-SE; +https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_DATAMANGD = r"rdfType:http\://www.w3.org/ns/dcat#Dataset"
_SERIE = r"rdfType:http\://www.w3.org/ns/dcat#DatasetSeries"
ROWSTORE_MAX_PER_ANROP = 100


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

async def _hamta_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/json"}) as klient:
        svar = await klient.get(url)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"EntryScape svarade {svar.status_code} på {url}: {svar.text[:200]}",
            request=svar.request, response=svar)
    return svar.json()


def _rot(bas_url: str) -> str:
    return bas_url.rstrip("/").removesuffix("/store")


# ---------------------------------------------------------------------------
# Katalog
# ---------------------------------------------------------------------------

def _solr_fras(fraga: str) -> str:
    """Varje ord trunkerat, alla ord måste finnas.

    Solr-specialtecken tas bort i stället för att escapas: en användarfråga
    ska aldrig kunna bli syntax.
    """
    ord_ = [o for o in re.split(r"[^\wåäöÅÄÖéü-]+", fraga.lower()) if len(o) > 1]
    return " AND ".join(f"all:{o}*" for o in ord_)


def _forsta_text(metadata: dict, predikat: str) -> str:
    for po in metadata.values():
        varden = po.get(predikat) or []
        for foredrag in ("sv", None, "en"):
            for v in varden:
                if v.get("lang") == foredrag:
                    return v.get("value", "")
        if varden:
            return varden[0].get("value", "")
    return ""


async def sok(bas_url: str, fraga: str, kontext: str = "",
              limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Datamängder och datamängdsserier som matchar fritexten.

    Returnerar `{"total", "traffar": [{kontext, post, uri, titel,
    beskrivning, andrad}]}`. `post` och `kontext` används för att hämta
    hela datamängden med `hamta_datamangd`.
    """
    rot = _rot(bas_url)
    delar = [f"({_DATAMANGD} OR {_SERIE})"]
    if fraga.strip():
        delar.append(f"({_solr_fras(fraga)})")
    if kontext:
        delar.append(f"context:{(rot + '/store/' + kontext).replace(':', chr(92) + ':')}")
    url = (f"{rot}/store/search?type=solr&limit={limit}&offset={offset}"
           f"&sort=modified+desc&query={quote(' AND '.join(delar))}")
    d = await _hamta_json(url)
    traffar = []
    for e in d.get("resource", {}).get("children", []):
        md = e.get("metadata") or {}
        # Metadatan kan ha flera noder (en Geonames-ort, en kontaktpunkt);
        # postens egen resurs är den som slutar på postens nummer.
        uri = next((s for s in md if s.rstrip("/").endswith(f"/resource/{e.get('entryId')}")),
                   next(iter(md), ""))
        traffar.append({
            "kontext": e.get("contextId", ""),
            "post": e.get("entryId", ""),
            "uri": uri,
            "titel": _forsta_text(md, dcat_modell.DCT + "title"),
            "beskrivning": _forsta_text(md, dcat_modell.DCT + "description")[:500],
            "andrad": _forsta_text(md, dcat_modell.DCT + "modified"),
        })
    return {"total": d.get("results", 0), "traffar": traffar}


async def hamta_datamangd(bas_url: str, kontext: str, post: str) -> dcat_modell.Datamangd:
    """Hela datamängden med distributioner och datatjänster."""
    rot = _rot(bas_url)
    graf = await _hamta_json(f"{rot}/store/{kontext}/metadata/{post}?recursive=dcat")
    datamangder = dcat_modell.datamangder_i_graf(graf)
    if not datamangder:
        # Poster som saknar rdf:type i metadatan — Gävle har sådana — ger en
        # tom rekursiv graf. Den vanliga metadatan har ändå titel och
        # beskrivning; saknas distributioner redovisas datamängden utan dem.
        graf = await _hamta_json(f"{rot}/store/{kontext}/metadata/{post}")
        egen = f"{rot}/store/{kontext}/resource/{post}"
        if egen not in graf:
            raise ValueError(f"Posten {kontext}/{post} i {rot} har ingen metadata som datamängd")
        return dcat_modell.fran_rdf_json(graf, egen, kalla=f"{rot}/store/{kontext}")
    # Den rekursiva grafen kan dra med närliggande datamängder; posten själv
    # är den vars resurs-URI slutar på postens nummer, annars den första.
    uri = next((u for u in datamangder if u.rstrip("/").endswith(f"/resource/{post}")),
               datamangder[0])
    return dcat_modell.fran_rdf_json(graf, uri, kalla=f"{rot}/store/{kontext}")


# ---------------------------------------------------------------------------
# Rowstore
# ---------------------------------------------------------------------------

def _rowstore_bas(url: str) -> str:
    m = re.match(r"(https?://[^?#]+/rowstore/dataset/[^/?#]+)", url)
    if not m:
        raise ValueError(f"Ingen rowstore-adress: {url}")
    return m.group(1)


# Första kolumnnamnet bär ibland ett BOM-tecken från CSV-filen tabellen
# laddades från. Utåt visas namnet utan det — annars går kolumnen inte att
# skriva — men rowstore kräver originalnamnet i filtret. Översättningen
# sparas per tabell.
_ORIGINALNAMN: dict[str, dict[str, str]] = {}


async def rowstore_info(url: str) -> dict[str, Any]:
    """Kolumner och antal rader."""
    bas = _rowstore_bas(url)
    d = await _hamta_json(bas + "/info")
    original = d.get("columnnames", [])
    _ORIGINALNAMN[bas] = {k.lstrip("﻿"): k for k in original}
    return {"kolumner": [k.lstrip("﻿") for k in original],
            "antal_rader": d.get("rowcount"),
            "skapad": d.get("created", "")}


async def rowstore_rader(url: str, filter: dict[str, str] | None = None,
                         limit: int = ROWSTORE_MAX_PER_ANROP, offset: int = 0) -> dict[str, Any]:
    """Rader, med filter som `{kolumn: värde}`. Värdet kan vara ett reguljärt
    uttryck (`Blomb.*`). Returnerar `{"antal_totalt", "rader"}`."""
    bas = _rowstore_bas(url)
    if filter and bas not in _ORIGINALNAMN:
        await rowstore_info(url)
    namn = _ORIGINALNAMN.get(bas, {})
    parametrar = {namn.get(k, k): v for k, v in (filter or {}).items()}
    parametrar.update({"_limit": min(limit, ROWSTORE_MAX_PER_ANROP), "_offset": offset})
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as klient:
        svar = await klient.get(bas + "/json", params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Rowstore svarade {svar.status_code} på {bas}: {svar.text[:200]}",
            request=svar.request, response=svar)
    d = svar.json()
    rader = [{k.lstrip("﻿"): v for k, v in r.items()} for r in d.get("results", [])]
    return {"antal_totalt": d.get("resultCount"), "rader": rader}
