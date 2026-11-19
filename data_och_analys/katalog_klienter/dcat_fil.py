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

Filen kommer från en okänd server och tolkas med försiktighet. XML med
dokumenttypsdeklaration avvisas, eftersom entiteter i en DTD kan blåsa upp
en liten fil till gigabyte. JSON-LD-kontexter som pekar ut en egen adress
hämtar rdflib själv, förbi suitens adresskontroll — de prövas därför först,
och andra protokoll än http och https avvisas. En sida får vara högst
`_MAX_SIDSTORLEK`, och nästa sida följs bara på katalogens egen värd.

Ingångspunkter:
    sok(katalog_url, fraga, limit=20, offset=0) -> dict
    hamta_datamangd(katalog_url, datamangd_uri) -> Datamangd
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from data_och_analys.infra import adresskontroll
from data_och_analys.oppnadata import dcat_modell

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/DcatFil (DCAT-AP-SE; +https://github.com/)"
_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_MINNE_SEKUNDER = 3600
_MAX_SIDOR = 50
_MAX_SIDSTORLEK = 100 * 1024 * 1024
_MAX_KONTEXTER = 10
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


async def _hamta_sida(klient: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    async with klient.stream("GET", url) as svar:
        if svar.status_code >= 400:
            raise ValueError(f"Katalogfilen {url} svarade {svar.status_code}")
        delar, storlek = [], 0
        async for bit in svar.aiter_bytes():
            storlek += len(bit)
            if storlek > _MAX_SIDSTORLEK:
                raise ValueError(f"Katalogfilen {url} är större än {_MAX_SIDSTORLEK // 2**20} MB")
            delar.append(bit)
        return b"".join(delar), svar.headers.get("content-type", "")


def _kontexter(nod: Any) -> list[str]:
    """Alla kontexter i ett JSON-LD-dokument som är adresser, även @import."""
    ut: list[str] = []
    if isinstance(nod, dict):
        for nyckel, varde in nod.items():
            if nyckel in ("@context", "@import"):
                for k in (varde if isinstance(varde, list) else [varde]):
                    if isinstance(k, str):
                        ut.append(k)
                    else:
                        ut += _kontexter(k)
            else:
                ut += _kontexter(varde)
    elif isinstance(nod, list):
        for v in nod:
            ut += _kontexter(v)
    return ut


async def _granska(innehall: bytes, fmt: str, url: str) -> None:
    """Avvisar det som rdflib annars skulle blåsa upp eller hämta på egen hand."""
    if fmt == "xml":
        if re.search(rb"<!(DOCTYPE|ENTITY)", innehall, re.I):
            raise ValueError(f"Katalogfilen {url} har en dokumenttypsdeklaration och tolkas inte")
        return
    if fmt != "json-ld":
        return
    try:
        dokument = json.loads(innehall)
    except ValueError:
        return  # rdflib ger ett tydligare fel om tolkningen
    kontexter = _kontexter(dokument)
    if len(kontexter) > _MAX_KONTEXTER:
        raise ValueError(f"Katalogfilen {url} hänvisar till {len(kontexter)} kontexter")
    for kontext in kontexter:
        adress = urljoin(url, kontext)
        if urlsplit(adress).scheme.lower() not in ("http", "https"):
            raise ValueError(f"Katalogfilen {url} hänvisar till kontexten {kontext}, som inte är http(s)")
        await adresskontroll.kontrollera(adress)


def _samma_vard(a: str, b: str) -> bool:
    return (urlsplit(a).scheme, urlsplit(a).hostname) == (urlsplit(b).scheme, urlsplit(b).hostname)


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
    async with adresskontroll.klient(timeout=_TIMEOUT, follow_redirects=True,
                                 # Kataloger levereras ofta som application/xml;
                                 # utan den i Accept svarar flera servrar 406.
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/rdf+xml, text/turtle, "
                                                    "application/ld+json;q=0.9, application/xml;q=0.8, "
                                                    "text/xml;q=0.8, */*;q=0.5"}) as klient:
        while nasta and sidor < _MAX_SIDOR:
            innehall, innehallstyp = await _hamta_sida(klient, nasta)
            sida = Graph()
            fmt = _rdf_format(innehallstyp, nasta, innehall)
            await _granska(innehall, fmt, nasta)
            try:
                sida.parse(data=innehall, format=fmt)
            except UnicodeDecodeError:
                # Filen deklarerar UTF-8 men innehåller Windows-1252. Felet är
                # källans; innehållet går ändå att läsa efter omkodning.
                logger.warning("Katalogfilen %s är inte giltig UTF-8; läses som Windows-1252", nasta)
                sida.parse(data=innehall.decode("cp1252", errors="replace").encode("utf-8"),
                           format=fmt)
            graf += sida
            sidor += 1
            foregaende = nasta
            nasta = next((str(o) for p in _HYDRA_NASTA for o in sida.objects(None, _uri(p))
                          if str(o) != foregaende), None)
            if nasta and not _samma_vard(nasta, katalog_url):
                logger.warning("Katalogfilen %s hänvisar nästa sida till en annan värd (%s); följs inte",
                               foregaende, nasta)
                nasta = None
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
