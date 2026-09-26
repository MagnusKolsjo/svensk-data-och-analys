# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för DCAT-kataloger som publiceras som en fil.

Många myndigheter har ingen katalogplattform med sök-API. De publicerar
sin DCAT-AP-SE-katalog som en fil — RDF/XML, Turtle eller JSON-LD — som
dataportal.se skördar. Filen är då organisationens katalog, och klienten
läser den direkt i stället för att gå via dataportalens kopia.

Filen tolkas med rdflib och görs om till samma RDF/JSON-form som
EntryScape levererar, så att `dcat_modell.fran_rdf_json` läser båda.
Kataloger som delas upp på sidor (Hydra, som CKAN:s `catalog.rdf?page=`)
följs sida för sida.

Den tolkade grafen hålls i minnet en timme per adress; en katalogfil ändras
sällan oftare än dagligen, och sökningar i följd ska inte hämta om den.

Ingångspunkter:
    sok(katalog_url, fraga, limit=20, offset=0) -> dict
    hamta_datamangd(katalog_url, datamangd_uri) -> Datamangd
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from data_och_analys.oppnadata import dcat_modell

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/DcatFil (DCAT-AP-SE; +https://github.com/)"
_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_MINNE_SEKUNDER = 3600
_MAX_SIDOR = 50
_HYDRA_NASTA = ("http://www.w3.org/ns/hydra/core#next", "http://www.w3.org/ns/hydra/core#nextPage")

_minne: dict[str, tuple[float, dict]] = {}


def _rdf_format(innehallstyp: str, url: str, innehall: bytes) -> str:
    t = innehallstyp.lower()
    if "turtle" in t or url.endswith(".ttl"):
        return "turtle"
    if "json" in t or url.endswith((".jsonld", ".json")):
        return "json-ld"
    if "n-triples" in t or url.endswith(".nt"):
        return "nt"
    if innehall.lstrip()[:1] in (b"{", b"["):
        return "json-ld"
    if innehall.lstrip()[:1] == b"@" or b"@prefix" in innehall[:2000]:
        return "turtle"
    return "xml"


def _till_rdf_json(graf) -> dict[str, dict[str, list[dict]]]:
    from rdflib import BNode, Literal

    def nod(n) -> str:
        return f"_:{n}" if isinstance(n, BNode) else str(n)

    ut: dict[str, dict[str, list[dict]]] = {}
    for s, p, o in graf:
        if isinstance(o, Literal):
            v = {"type": "literal", "value": str(o)}
            if o.language:
                v["lang"] = o.language
        else:
            v = {"type": "bnode" if isinstance(o, BNode) else "uri", "value": nod(o)}
        ut.setdefault(nod(s), {}).setdefault(str(p), []).append(v)
    return ut


async def _graf(katalog_url: str) -> dict:
    lagrad = _minne.get(katalog_url)
    if lagrad and time.monotonic() - lagrad[0] < _MINNE_SEKUNDER:
        return lagrad[1]
    from rdflib import Graph
    graf = Graph()
    nasta, sidor = katalog_url, 0
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 # Kataloger levereras ofta som application/xml;
                                 # utan den i Accept svarar flera servrar 406.
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/rdf+xml, text/turtle, "
                                                    "application/ld+json;q=0.9, application/xml;q=0.8, "
                                                    "text/xml;q=0.8, */*;q=0.5"}) as klient:
        while nasta and sidor < _MAX_SIDOR:
            svar = await klient.get(nasta)
            if svar.status_code >= 400:
                raise ValueError(f"Katalogfilen {nasta} svarade {svar.status_code}")
            sida = Graph()
            fmt = _rdf_format(svar.headers.get("content-type", ""), nasta, svar.content)
            try:
                sida.parse(data=svar.content, format=fmt)
            except UnicodeDecodeError:
                # Filen deklarerar UTF-8 men innehåller Windows-1252. Felet är
                # källans; innehållet går ändå att läsa efter omkodning.
                logger.warning("Katalogfilen %s är inte giltig UTF-8; läses som Windows-1252", nasta)
                sida.parse(data=svar.content.decode("cp1252", errors="replace").encode("utf-8"),
                           format=fmt)
            graf += sida
            sidor += 1
            nasta = next((str(o) for p in _HYDRA_NASTA for o in sida.objects(None, _uri(p))
                          if str(o) != nasta), None)
    rdf_json = _till_rdf_json(graf)
    _minne[katalog_url] = (time.monotonic(), rdf_json)
    return rdf_json


def _uri(u: str):
    from rdflib import URIRef
    return URIRef(u)


async def sok(katalog_url: str, fraga: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Datamängder vars titel, beskrivning eller nyckelord innehåller alla
    orden i frågan — som ordbörjan, så att "bad" träffar "badplatser"."""
    graf = await _graf(katalog_url)
    ord_ = [o for o in re.split(r"[^\wåäöÅÄÖéü-]+", fraga.lower()) if len(o) > 1]
    traffar = []
    for uri in dcat_modell.datamangder_i_graf(graf):
        titel = dcat_modell._text(graf, uri, dcat_modell.DCT + "title")
        beskrivning = dcat_modell._text(graf, uri, dcat_modell.DCT + "description")
        nyckelord = " ".join(dcat_modell._texter(graf, uri, dcat_modell.DCAT + "keyword"))
        text = f"{titel} {beskrivning} {nyckelord}".lower()
        if all(re.search(rf"(?<!\w){re.escape(o)}", text) for o in ord_):
            traffar.append({"uri": uri, "titel": titel, "beskrivning": beskrivning[:500],
                            "andrad": dcat_modell._text(graf, uri, dcat_modell.DCT + "modified")})
    traffar.sort(key=lambda t: t["andrad"], reverse=True)
    return {"total": len(traffar), "traffar": traffar[offset:offset + limit]}


async def hamta_datamangd(katalog_url: str, datamangd_uri: str) -> dcat_modell.Datamangd:
    graf = await _graf(katalog_url)
    if datamangd_uri not in graf:
        raise ValueError(f"{datamangd_uri} finns inte i katalogen {katalog_url}")
    return dcat_modell.fran_rdf_json(graf, datamangd_uri, kalla=katalog_url)
