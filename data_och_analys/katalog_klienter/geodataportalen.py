# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för Geodataportalen (GeoNetwork-katalog).

Geodataportalen (www.geodata.se) är Sveriges nationella metadatakatalog
för geodata. Driven av Lantmäteriet men aggregerar dataset från ~25
myndigheter (SCB, Sjöfartsverket, Trafikverket, kommuner, länsstyrelser,
SMHI m.fl.). Plattformen är GeoNetwork — samma mjukvara används av
Eurostat, EuroGeographics och flera EU-länders nationella geodatakataloger.

Klienten är generisk över GeoNetwork-instanser — `instans`-parametern
slår upp bas-URL i `INSTANSER`-registret. Mönstret är identiskt med
PxWeb-myndighetsregistret och Huwise-instansregistret. Env-överstyrning
per instans via `DOA_GEODATA_<NAMN>_BAS_URL`.

Geodataportalen är en **metadatakatalog**, inte en datakälla — den säger
"dataset X finns på endpoint Y hos myndighet Z". Själva geometrierna
hämtas via separata WFS-, OGC API Features-, eller STAC-klienter mot
endpoints som katalogen pekar mot.

GeoNetwork-API:
    POST /srv/api/search/records/_search   -> Elasticsearch-stil sökning
    GET  /srv/api/records/<uuid>            -> full metadata
    GET  /srv/swe/csw                       -> OGC CSW-standard (XML)

Ingångspunkter:
    lista_instanser() -> list[str]
    sok(instans, query=None, tema=None, organisation=None, ...)
    hamta_metadata(instans, uuid) -> dict
    lista_teman() -> list[str]
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Instansregister
# ============================================================================


@dataclass(frozen=True)
class GeodataInstans:
    namn: str
    bas_url: str


_BASLINJE: dict[str, GeodataInstans] = {
    # Sveriges nationella geodatakatalog (driven av Lantmäteriet)
    "geodata": GeodataInstans(
        namn="geodata",
        bas_url="https://www.geodata.se/geodataportalen",
    ),
}


def _med_overstyrning(bas: GeodataInstans) -> GeodataInstans:
    overstyrd = os.getenv(f"DOA_GEODATA_{bas.namn.upper()}_BAS_URL", "").strip()
    if not overstyrd:
        return bas
    return GeodataInstans(namn=bas.namn, bas_url=overstyrd.rstrip("/"))


def hamta_instans(namn: str) -> GeodataInstans:
    if namn not in _BASLINJE:
        kanda = ", ".join(sorted(_BASLINJE))
        raise ValueError(
            f"okänd geodata-instans: {namn!r}. Tillgängliga: {kanda}"
        )
    return _med_overstyrning(_BASLINJE[namn])


def lista_instanser() -> list[str]:
    return sorted(_BASLINJE)


# ============================================================================
# ISO 19115-teman (cl_topic) — hardkodade konstanter
# ============================================================================
#
# Den officiella ISO 19115 topic category-listan består av 19 teman.
# Hardkodning här eftersom listan är stabil — ändras vid ISO-revidering
# vart cirka tionde år. För användarvänliga svenska benämningar.

TEMAN: dict[str, str] = {
    "farming": "Jordbruk",
    "biota": "Biota",
    "boundaries": "Gränser (mest icke-administrativa)",
    "climatologyMeteorologyAtmosphere": "Klimat och meteorologi",
    "economy": "Ekonomi",
    "elevation": "Höjddata",
    "environment": "Miljö",
    "geoscientificInformation": "Geovetenskaplig information",
    "health": "Hälsa",
    "imageryBaseMapsEarthCover": "Bildmaterial och bakgrundskartor",
    "intelligenceMilitary": "Militärt",
    "inlandWaters": "Inlandsvatten",
    "location": "Lokalisering",
    "oceans": "Hav",
    "planningCadastre": "Planering och fastigheter (admin gränser)",
    "society": "Samhälle",
    "structure": "Byggnader och konstruktioner",
    "transportation": "Transport",
    "utilitiesCommunication": "Försörjning och kommunikation",
}


def lista_teman() -> list[dict[str, str]]:
    """Returnerar ISO 19115-temalistan med svenska benämningar."""
    return [{"kod": k, "svensk_benamning": v} for k, v in TEMAN.items()]


# ============================================================================
# HTTP-anrop
# ============================================================================

USER_AGENT = (
    "svensk-data-och-analys/GeodataportalenClient "
    "(GeoNetwork; +https://github.com/)"
)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


async def _post_json(
    instans_namn: str, sokvag: str, kropp: dict[str, Any]
) -> Any:
    inst = hamta_instans(instans_namn)
    url = f"{inst.bas_url}/srv/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    ) as klient:
        svar = await klient.post(url, json=kropp)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Geodata-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


async def _get_json(
    instans_namn: str, sokvag: str, parametrar: dict[str, Any] | None = None
) -> Any:
    inst = hamta_instans(instans_namn)
    url = f"{inst.bas_url}/srv/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Geodata-anrop misslyckades ({svar.status_code}) mot {url}",
            request=svar.request,
            response=svar,
        )
    return svar.json()


# ============================================================================
# Publikt API
# ============================================================================


# Default-fält att returnera i sök — räcker för att lista datasets med
# källinfo och åtkomstlänkar utan att svaret blir tjockt.
_DEFAULT_FALT = [
    "resourceTitleObject",
    "resourceAbstractObject",
    "uuid",
    "OrgForResource",
    "cl_topic",
    "cl_resourceProvider",
    "link",
]


async def sok(
    instans: str = "geodata",
    query: str | None = None,
    tema: str | None = None,
    organisation: str | None = None,
    limit: int = 10,
    offset: int = 0,
    falt: list[str] | None = None,
) -> dict[str, Any]:
    """Söker dataset i katalogen.

    `query` är fritext mot titel/abstrakt. `tema` är ett ISO 19115-tema
    (se `lista_teman`). `organisation` filtrerar på publicistnamn
    (t.ex. "Lantmäteriet").

    Argumenten kombineras med boolean AND. Tomma sökargument ger
    hela katalogen (begränsad av `limit`).

    Returnerar Elasticsearch-rå-svar: `{hits: {total, hits: [...]}}`.
    """
    must: list[dict[str, Any]] = []
    if query:
        must.append({
            "multi_match": {
                "query": query,
                "fields": [
                    "resourceTitleObject.default^2",
                    "resourceAbstractObject.default",
                    "tag.default",
                ],
            }
        })
    if tema:
        must.append({"term": {"cl_topic.key": tema}})
    if organisation:
        # Fältet heter `OrgForResource` utan suffix och är en array av
        # strängar. Varken `.default` eller `.keyword` finns i GeoNetworks
        # index — båda ger noll träffar, vilket är varför filtret tidigare
        # såg ut att inte göra något. Verifierat: `term` på OrgForResource
        # ger 110 träffar för Lantmäteriet, de andra två ger 0.
        must.append({"term": {"OrgForResource": organisation}})

    es_kropp: dict[str, Any] = {
        "from": offset,
        "size": limit,
        "_source": falt or _DEFAULT_FALT,
    }
    if must:
        es_kropp["query"] = {"bool": {"must": must}}
    else:
        es_kropp["query"] = {"match_all": {}}

    return await _post_json(instans, "api/search/records/_search", es_kropp)


async def hamta_metadata(instans: str, uuid: str) -> dict[str, Any]:
    """Returnerar full metadata-record för ett dataset (en UUID från sök).

    Inkluderar alla länkar — WFS, WMS, OGC API Features, nedladdning,
    informationssidor — som behövs för att veta hur man når själva
    datat. Svaret är samma struktur som ett enskilt hit i `sok`-svaret,
    fast med alla `_source`-fält.
    """
    res = await _post_json(
        instans,
        "api/search/records/_search",
        {
            "from": 0,
            "size": 1,
            "query": {"term": {"_id": uuid}},
        },
    )
    träffar = res.get("hits", {}).get("hits", [])
    if not träffar:
        raise ValueError(f"inget dataset med uuid={uuid!r} hittades")
    return träffar[0].get("_source", {})


async def lista_organisationer(
    instans: str = "geodata", topp: int = 50
) -> list[dict[str, Any]]:
    """Returnerar publicistorganisationer rangerade efter antal dataset.

    Användbart för att se vilka myndigheter som har data i katalogen och
    för att fylla i `organisation`-parametern i sök.
    """
    # GeoNetwork använder `cl_resourceProvider`-fältet för aggregerbar
    # publicistnamn — `OrgForResource` är text-fält utan keyword-subfield.
    res = await _post_json(
        instans,
        "api/search/records/_search",
        {
            "size": 0,
            "aggs": {
                "orgs": {
                    # Samma fält som filtret. `.keyword` finns inte och gav
                    # en tom aggregering.
                    "terms": {"field": "OrgForResource", "size": topp}
                }
            },
        },
    )
    buckets = (
        res.get("aggregations", {}).get("orgs", {}).get("buckets", [])
    )
    return [
        {"organisation": b["key"], "antal_dataset": b["doc_count"]}
        for b in buckets
    ]
