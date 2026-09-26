# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Generisk OAI-PMH 2.0-skördare (Open Archives Initiative).

OAI-PMH är ett standardiserat skördningsprotokoll för metadata över HTTP.
Svaren är XML i namespace `http://www.openarchives.org/OAI/2.0/`. Det
används av forskningsdataarkiv (researchdata.se, SND), bibliotekskataloger
och repositorier. Den här modulen är källagnostisk — `bas_url` pekar mot
vilken OAI-PMH-endpoint som helst; `researchdata.py` är en tunn wrapper
som fyller i researchdata.se:s bas-URL.

De sex protokollverben:

    Identify              -> repository-metadata
    ListMetadataFormats   -> tillgängliga metadataformat (oai_dc, ...)
    ListSets              -> tematiska/organisatoriska set
    ListIdentifiers       -> bara record-headers (id, datestamp, set)
    ListRecords           -> headers + full metadata
    GetRecord             -> en enskild record

Listande verb paginerar via `resumptionToken`: svaret bär en token som
matas tillbaka i nästa anrop tills den saknas. Tokenen är opak och får
inte kombineras med andra argument — det följer vi.

Metadata parsas generiskt: namespace-prefix strippas och upprepade element
(t.ex. flera `dc:subject`) samlas i listor. Det gör Dublin Core (oai_dc)
direkt läsbart utan formatspecifik kod, samtidigt som godtyckliga format
fungerar.

Ingångspunkter:
    identify(bas_url)
    lista_metadataformat(bas_url, identifier=None)
    lista_set(bas_url, resumption_token=None)
    lista_identifierare(bas_url, metadata_prefix, ...)
    lista_poster(bas_url, metadata_prefix, ...)
    hamta_post(bas_url, identifier, metadata_prefix)
"""

from __future__ import annotations

import logging
from typing import Any
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/OaiPmhClient (OAI-PMH 2.0; +https://github.com/)"
_TIMEOUT = httpx.Timeout(90.0, connect=10.0)

_OAI_NS = "http://www.openarchives.org/OAI/2.0/"


# ============================================================================
# HTTP + XML
# ============================================================================


async def _hamta_xml(bas_url: str, parametrar: dict[str, str]) -> ET.Element:
    """GET mot en OAI-PMH-endpoint och returnerar rot-elementet.

    Höjer på HTTP-fel och på OAI-PMH:s egna `<error>`-element (t.ex.
    `noRecordsMatch`, `badArgument`) så att felkoden syns för anroparen.
    """
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(bas_url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"OAI-PMH-anrop misslyckades ({svar.status_code}) mot {bas_url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    rot = ET.fromstring(svar.content)
    fel = rot.find(f"{{{_OAI_NS}}}error")
    if fel is not None:
        kod = fel.get("code", "okänd")
        raise RuntimeError(f"OAI-PMH-fel [{kod}]: {(fel.text or '').strip()}")
    return rot


def _lokalt_namn(tagg: str) -> str:
    """Strippar namespace ur en ElementTree-tagg: `{ns}local` -> `local`."""
    return tagg.rsplit("}", 1)[-1]


def _element_till_dict(el: ET.Element) -> Any:
    """Konverterar ett XML-element till dict/lista/sträng generiskt.

    Upprepade barn-taggar samlas i listor (Dublin Core upprepar t.ex.
    `subject` och `creator`). Lövnoder blir sin textsträng. Attribut
    bevaras under `@namn`-nycklar när de finns.
    """
    barn = list(el)
    if not barn:
        text = (el.text or "").strip()
        if el.attrib:
            d: dict[str, Any] = {f"@{_lokalt_namn(k)}": v for k, v in el.attrib.items()}
            if text:
                d["#text"] = text
            return d
        return text

    resultat: dict[str, Any] = {}
    for b in barn:
        namn = _lokalt_namn(b.tag)
        varde = _element_till_dict(b)
        if namn in resultat:
            if not isinstance(resultat[namn], list):
                resultat[namn] = [resultat[namn]]
            resultat[namn].append(varde)
        else:
            resultat[namn] = varde
    return resultat


def _parsa_header(header: ET.Element) -> dict[str, Any]:
    """Parsar en record-`<header>` till `{identifier, datestamp, set, status}`."""
    def _text(tagg: str) -> str | None:
        el = header.find(f"{{{_OAI_NS}}}{tagg}")
        return el.text.strip() if el is not None and el.text else None

    set_specs = [
        (el.text or "").strip()
        for el in header.findall(f"{{{_OAI_NS}}}setSpec")
    ]
    return {
        "identifier": _text("identifier"),
        "datestamp": _text("datestamp"),
        "set": set_specs,
        # status="deleted" markerar borttagna poster (OAI-PMH tombstone)
        "status": header.get("status"),
    }


def _parsa_record(record: ET.Element) -> dict[str, Any]:
    """Parsar en `<record>` till `{header, metadata}`.

    `metadata` är den generiskt parsade nyttolasten (formatets rot-element
    under `<metadata>`), eller `None` för borttagna poster utan kropp.
    """
    header_el = record.find(f"{{{_OAI_NS}}}header")
    header = _parsa_header(header_el) if header_el is not None else {}

    metadata_el = record.find(f"{{{_OAI_NS}}}metadata")
    metadata: Any = None
    if metadata_el is not None and len(metadata_el):
        # Hoppa förbi metadata-omslaget till formatets rot (t.ex. oai_dc:dc)
        metadata = _element_till_dict(list(metadata_el)[0])

    return {"header": header, "metadata": metadata}


def _resumption_token(rot: ET.Element, verb_tagg: str) -> str | None:
    """Plockar resumptionToken ur ett listsvar (None om tom eller saknas)."""
    behallare = rot.find(f"{{{_OAI_NS}}}{verb_tagg}")
    if behallare is None:
        return None
    token = behallare.find(f"{{{_OAI_NS}}}resumptionToken")
    if token is None or not (token.text or "").strip():
        return None
    return token.text.strip()


# ============================================================================
# Protokollverb
# ============================================================================


async def identify(bas_url: str) -> dict[str, Any]:
    """Identify — repository-metadata (namn, admin-epost, granularitet)."""
    rot = await _hamta_xml(bas_url, {"verb": "Identify"})
    el = rot.find(f"{{{_OAI_NS}}}Identify")
    return _element_till_dict(el) if el is not None else {}


async def lista_metadataformat(
    bas_url: str, identifier: str | None = None
) -> list[dict[str, Any]]:
    """ListMetadataFormats — format som stöds (för hela arkivet eller en post)."""
    parametrar = {"verb": "ListMetadataFormats"}
    if identifier:
        parametrar["identifier"] = identifier
    rot = await _hamta_xml(bas_url, parametrar)
    behallare = rot.find(f"{{{_OAI_NS}}}ListMetadataFormats")
    if behallare is None:
        return []
    return [
        _element_till_dict(f)
        for f in behallare.findall(f"{{{_OAI_NS}}}metadataFormat")
    ]


async def lista_set(
    bas_url: str, resumption_token: str | None = None
) -> dict[str, Any]:
    """ListSets — tematiska/organisatoriska set i arkivet.

    Returnerar `{set, resumption_token}`. När `resumption_token` är
    satt ignoreras övriga argument (OAI-PMH-krav).
    """
    parametrar = (
        {"verb": "ListSets", "resumptionToken": resumption_token}
        if resumption_token
        else {"verb": "ListSets"}
    )
    rot = await _hamta_xml(bas_url, parametrar)
    behallare = rot.find(f"{{{_OAI_NS}}}ListSets")
    sets = (
        [_element_till_dict(s) for s in behallare.findall(f"{{{_OAI_NS}}}set")]
        if behallare is not None
        else []
    )
    return {"set": sets, "resumption_token": _resumption_token(rot, "ListSets")}


def _list_parametrar(
    verb: str,
    metadata_prefix: str,
    set_spec: str | None,
    from_: str | None,
    until: str | None,
    resumption_token: str | None,
) -> dict[str, str]:
    """Bygger query för ListIdentifiers/ListRecords.

    En resumptionToken är exklusiv — den bär hela det selektiva tillståndet
    och får inte kombineras med metadataPrefix/set/from/until.
    """
    if resumption_token:
        return {"verb": verb, "resumptionToken": resumption_token}
    parametrar = {"verb": verb, "metadataPrefix": metadata_prefix}
    if set_spec:
        parametrar["set"] = set_spec
    if from_:
        parametrar["from"] = from_
    if until:
        parametrar["until"] = until
    return parametrar


async def lista_identifierare(
    bas_url: str,
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    from_: str | None = None,
    until: str | None = None,
    resumption_token: str | None = None,
) -> dict[str, Any]:
    """ListIdentifiers — bara record-headers (snabb katalogöversikt).

    Returnerar `{identifierare, resumption_token}`. Mata tillbaka
    `resumption_token` i nästa anrop för nästa sida.
    """
    parametrar = _list_parametrar(
        "ListIdentifiers", metadata_prefix, set_spec, from_, until, resumption_token
    )
    rot = await _hamta_xml(bas_url, parametrar)
    behallare = rot.find(f"{{{_OAI_NS}}}ListIdentifiers")
    headers = (
        [_parsa_header(h) for h in behallare.findall(f"{{{_OAI_NS}}}header")]
        if behallare is not None
        else []
    )
    return {
        "identifierare": headers,
        "resumption_token": _resumption_token(rot, "ListIdentifiers"),
    }


async def lista_poster(
    bas_url: str,
    metadata_prefix: str = "oai_dc",
    set_spec: str | None = None,
    from_: str | None = None,
    until: str | None = None,
    resumption_token: str | None = None,
) -> dict[str, Any]:
    """ListRecords — headers + full metadata, en sida i taget.

    Returnerar `{poster, resumption_token}`. Varje post är
    `{header, metadata}`. Mata tillbaka `resumption_token` för nästa sida.
    """
    parametrar = _list_parametrar(
        "ListRecords", metadata_prefix, set_spec, from_, until, resumption_token
    )
    rot = await _hamta_xml(bas_url, parametrar)
    behallare = rot.find(f"{{{_OAI_NS}}}ListRecords")
    poster = (
        [_parsa_record(r) for r in behallare.findall(f"{{{_OAI_NS}}}record")]
        if behallare is not None
        else []
    )
    return {
        "poster": poster,
        "resumption_token": _resumption_token(rot, "ListRecords"),
    }


async def hamta_post(
    bas_url: str, identifier: str, metadata_prefix: str = "oai_dc"
) -> dict[str, Any] | None:
    """GetRecord — en enskild post som `{header, metadata}` (None om tom)."""
    rot = await _hamta_xml(
        bas_url,
        {
            "verb": "GetRecord",
            "identifier": identifier,
            "metadataPrefix": metadata_prefix,
        },
    )
    behallare = rot.find(f"{{{_OAI_NS}}}GetRecord")
    if behallare is None:
        return None
    record = behallare.find(f"{{{_OAI_NS}}}record")
    return _parsa_record(record) if record is not None else None
