# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""DCAT-AP-SE — discovery mot dataportal.se via SPARQL.

dataportal.se (Sveriges dataportal, driven av DIGG) är den nationella
katalogen över öppna data enligt DCAT-AP-SE-profilen. Dess SPARQL-endpoint
exponerar hela katalogen som RDF, vilket gör det möjligt att fråga efter
exakt var och hur en datamängd nås:

    dcat:Dataset
        dcterms:title, dcterms:description, dcterms:publisher
        dcat:distribution -> dcat:Distribution
            dcat:accessURL, dcat:downloadURL, dcterms:format
            dcat:accessService -> dcat:endpointURL

Den maskinläsbara distributionsinformationen är poängen: en sökning här
säger inte bara att en datamängd finns, utan pekar mot endpointen
(PxWeb, OGC API, CKAN, nedladdnings-URL) som en hämtningsklient sedan
läser. Det är discovery-ledet — katalogen returnerar metadata, inte data.

SPARQL-endpointen kan överstyras via `DOA_DATAPORTAL_SPARQL_URL` i .env.

Ingångspunkter:
    kor_sparql(fraga, format="json")
    sok_dataset(fritext=None, limit=20)
    hitta_distributioner_med_url(url_monster, limit=50)
    hitta_pxweb_instanser(limit=200)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SPARQL_URL = "https://admin.dataportal.se/sparql"
USER_AGENT = "svensk-data-och-analys/DcatClient (DCAT-AP-SE; +https://github.com/)"
_TIMEOUT = httpx.Timeout(90.0, connect=10.0)

_PREFIXER = """
PREFIX dcat: <http://www.w3.org/ns/dcat#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
"""


def _endpoint() -> str:
    return os.getenv("DOA_DATAPORTAL_SPARQL_URL", _SPARQL_URL).strip().rstrip("/")


def _escape_literal(text: str) -> str:
    """Escapar en SPARQL-stränglitteral så fritext inte bryter frågan.

    Backslash och citattecken neutraliseras; nyrader strippas. Det här är
    inte fullständig injection-härdning men hindrar att en användartext med
    citattecken bryter ut ur literalen.
    """
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


# ============================================================================
# SPARQL-anrop
# ============================================================================


async def kor_sparql(fraga: str, format: str = "json") -> Any:
    """Kör en godtycklig SPARQL-fråga mot dataportal.se.

    `format="json"` returnerar SPARQL-resultat-JSON
    (`{head, results: {bindings: [...]}}`). `format="raw"` returnerar
    råtexten — användbart för CONSTRUCT/DESCRIBE som ger RDF.

    Prefixen dcat/dcterms/foaf injiceras automatiskt om frågan inte redan
    deklarerar dem, så anroparen kan skriva korta frågor.
    """
    if "PREFIX" not in fraga.upper():
        fraga = _PREFIXER + fraga

    accept = (
        "application/sparql-results+json"
        if format == "json"
        else "text/plain, */*"
    )
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": accept}
    ) as klient:
        svar = await klient.get(_endpoint(), params={"query": fraga})
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"SPARQL-anrop misslyckades ({svar.status_code}) mot "
            f"{_endpoint()}: {svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar.json() if format == "json" else svar.text


def _platta_bindings(resultat: dict[str, Any]) -> list[dict[str, Any]]:
    """Plattar SPARQL-JSON `results.bindings` till enkla `{var: värde}`-rader.

    Varje variabels `value` plockas ut; typ och språk slängs för att hålla
    raderna läsbara. Saknade valbara variabler utelämnas helt i raden.
    """
    rader: list[dict[str, Any]] = []
    for b in resultat.get("results", {}).get("bindings", []):
        rader.append({var: cell.get("value") for var, cell in b.items()})
    return rader


# ============================================================================
# Discovery-frågor
# ============================================================================


async def sok_dataset(fritext: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Söker dataset i katalogen på titel/beskrivning (fritext).

    Returnerar `[{dataset, titel, beskrivning, utgivare}]`. Utan
    `fritext` returneras de första `limit` dataseten — mest för
    utforskning. `dataset`-URI:n matas till `hamta_distributioner` (via
    SPARQL) eller används som nyckel mot katalogens webbsida.
    """
    if fritext:
        filter_klausul = (
            f'FILTER(CONTAINS(LCASE(STR(?titel)), LCASE("{_escape_literal(fritext)}")) '
            f'|| CONTAINS(LCASE(STR(?beskrivning)), LCASE("{_escape_literal(fritext)}")))'
        )
    else:
        filter_klausul = ""

    fraga = f"""
SELECT ?dataset ?titel ?beskrivning ?utgivare WHERE {{
  ?dataset a dcat:Dataset .
  ?dataset dcterms:title ?titel .
  OPTIONAL {{ ?dataset dcterms:description ?beskrivning }}
  OPTIONAL {{ ?dataset dcterms:publisher ?utg . ?utg foaf:name ?utgivare }}
  {filter_klausul}
}}
LIMIT {int(limit)}
"""
    return _platta_bindings(await kor_sparql(fraga))


async def hitta_distributioner_med_url(
    url_monster: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Hittar dataset vars distributions-`accessURL` matchar ett mönster.

    `url_monster` är en delsträng (t.ex. `"PXWeb"`, `"wfs"`,
    `"ogc/features"`). Returnerar `[{dataset, titel, accessURL, format}]`.
    Det här är discovery-pipelinens kärna: hitta alla datamängder som nås
    via en viss teknik och mata dem till rätt hämtningsklient.
    """
    fraga = f"""
SELECT ?dataset ?titel ?accessURL ?format WHERE {{
  ?dataset a dcat:Dataset .
  ?dataset dcat:distribution ?dist .
  ?dist dcat:accessURL ?accessURL .
  OPTIONAL {{ ?dataset dcterms:title ?titel }}
  OPTIONAL {{ ?dist dcterms:format ?format }}
  FILTER(CONTAINS(STR(?accessURL), "{_escape_literal(url_monster)}"))
}}
LIMIT {int(limit)}
"""
    return _platta_bindings(await kor_sparql(fraga))


async def hitta_pxweb_instanser(limit: int = 200) -> list[dict[str, Any]]:
    """Hittar alla PxWeb-instanser i katalogen via accessURL-mönstret.

    Bekvämlighetsvariant av `hitta_distributioner_med_url("PXWeb")` —
    motsvarar discovery-frågan i projektets DCAT-pipeline för att upptäcka
    nya PxWeb-myndigheter att lägga in i registret.
    """
    return await hitta_distributioner_med_url("PXWeb", limit=limit)
