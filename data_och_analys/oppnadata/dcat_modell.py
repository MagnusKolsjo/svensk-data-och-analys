# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""DCAT-AP-SE som modell: datamängd, distribution och datatjänst.

Specifikationen är DIGG:s DCAT-AP-SE 3.0.1 (https://docs.dataportal.se/dcat/sv/).
Varje plattform — EntryScape, CKAN, Huwise, ArcGIS Hub, dataportalens
SPARQL — översätts hit, så att resten av suiten bara behöver förstå en form.

Tre drag i profilen styr hur modellen ser ut:

- `accessURL` är obligatorisk men betyder bara "en webbadress till
  distributionen". Den kan vara en fil, en webbsida eller ett API.
  `downloadURL` är valfri och betyder alltid en fil. Därför bär
  distributionen format, datatjänst och länkade scheman — de avgör vad
  `accessURL` är (se `hamtningsvag.py`).
- Sverige tillät tidigt flera filer på en distribution genom upprepad
  `downloadURL`, gärna med en titel per fil (rekommendation 4). Den är
  överspelad av datamängdsserier men stöds bakåtkompatibelt, och finns
  kvar i äldre kataloger. Filerna är därför en lista med titlar.
- Datamängdsserier (rekommendation 17–20) samlar periodiska datamängder;
  en datatjänst kan försörja hela serien via `servesDataset`.

Ingångspunkter:
    Datamangd, Distribution, Datatjanst, Fillank, Utgivare
    fran_rdf_json(graf, datamangd_uri) -> Datamangd
    datamangder_i_graf(graf) -> list[str]
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Namnrymder
# ---------------------------------------------------------------------------

DCAT = "http://www.w3.org/ns/dcat#"
DCT = "http://purl.org/dc/terms/"
FOAF = "http://xmlns.com/foaf/0.1/"
RDF_TYP = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
ADMS = "http://www.w3.org/ns/adms#"
DCATAP = "http://data.europa.eu/r5r/"
VCARD = "http://www.w3.org/2006/vcard/ns#"
SCHEMA = "http://schema.org/"


# ---------------------------------------------------------------------------
# Modell
# ---------------------------------------------------------------------------

class Fillank(BaseModel):
    """En fil i en distribution. Titeln skiljer filerna åt när de är flera."""
    url: str
    titel: str = ""


class Datatjanst(BaseModel):
    """dcat:DataService — ett API som ger åtkomst till distributionen."""
    uri: str = ""
    titel: str = ""
    endpoint_url: list[str] = Field(default_factory=list,
                                    description="Basadress eller startpunkt (dcat:endpointURL)")
    endpoint_beskrivning: list[str] = Field(
        default_factory=list,
        description="Beskrivning av åtkomstadressen: OpenAPI, GetCapabilities (dcat:endpointDescription)")
    uppfyller: list[str] = Field(default_factory=list,
                                 description="Standard eller protokoll som tjänsten följer (dcterms:conformsTo)")
    typ: list[str] = Field(default_factory=list, description="Arkitekturstil (dcterms:type)")
    format: list[str] = Field(default_factory=list)


class Distribution(BaseModel):
    """dcat:Distribution — ett sätt att komma åt datamängden."""
    uri: str = ""
    titel: str = ""
    beskrivning: str = ""
    access_url: list[str] = Field(default_factory=list,
                                  description="dcat:accessURL — fil, webbsida eller API")
    filer: list[Fillank] = Field(default_factory=list,
                                 description="dcat:downloadURL — alltid filer, ibland en per period")
    format: list[str] = Field(default_factory=list,
                              description="Mediatyper. Tjänstemediatyper som application/vnd.ogc.wfs_xml "
                                          "anger protokoll, inte filformat.")
    uppfyller: list[str] = Field(default_factory=list,
                                 description="Länkade scheman, t.ex. en OpenAPI-beskrivning (dcterms:conformsTo)")
    datatjanster: list[Datatjanst] = Field(default_factory=list)
    storlek_byte: int | None = None
    andrad: str = ""
    licens: str = ""
    dokumentation: list[str] = Field(default_factory=list)


class Utgivare(BaseModel):
    uri: str = ""
    namn: str = ""


class Datamangd(BaseModel):
    """dcat:Dataset eller dcat:DatasetSeries."""
    uri: str
    titel: str = ""
    beskrivning: str = ""
    utgivare: Utgivare = Field(default_factory=Utgivare)
    nyckelord: list[str] = Field(default_factory=list)
    tema: list[str] = Field(default_factory=list)
    tidsperiod: tuple[str, str] | None = Field(
        default=None, description="Start och slut ur dcterms:temporal, när de anges")
    geografi: list[str] = Field(default_factory=list)
    utgiven: str = ""
    andrad: str = ""
    uppdateringsfrekvens: str = ""
    landningssida: list[str] = Field(default_factory=list)
    ar_serie: bool = False
    i_serie: list[str] = Field(default_factory=list, description="Serier datamängden ingår i")
    distributioner: list[Distribution] = Field(default_factory=list)
    kalla: str = Field(default="", description="Katalogen datamängden lästes ur")


# ---------------------------------------------------------------------------
# RDF/JSON
# ---------------------------------------------------------------------------
# RDF/JSON är {subjekt: {predikat: [{"type", "value", "lang"?}]}}. Både
# EntryScape-storens metadata och dataportalens SPARQL levererar det, så
# en tolk räcker för båda.

def _varden(graf: dict, subjekt: str, predikat: str) -> list[dict]:
    return (graf.get(subjekt) or {}).get(predikat) or []


def _uris(graf: dict, subjekt: str, predikat: str) -> list[str]:
    return [v["value"] for v in _varden(graf, subjekt, predikat) if v.get("value")]


def _text(graf: dict, subjekt: str, predikat: str) -> str:
    """Svensk språkversion om den finns, annars utan språk, annars engelska."""
    varden = _varden(graf, subjekt, predikat)
    for foredrag in ("sv", None, "en"):
        for v in varden:
            if v.get("type") == "literal" and v.get("lang") == foredrag:
                return v["value"].strip()
    return varden[0]["value"].strip() if varden else ""


def _texter(graf: dict, subjekt: str, predikat: str) -> list[str]:
    """Alla värden; nyckelord har en post per språk och ska inte dubbleras."""
    sv = [v["value"] for v in _varden(graf, subjekt, predikat) if v.get("lang") in ("sv", None)]
    return list(dict.fromkeys(sv or [v["value"] for v in _varden(graf, subjekt, predikat)]))


def _typer(graf: dict, subjekt: str) -> set[str]:
    return set(_uris(graf, subjekt, RDF_TYP))


def datamangder_i_graf(graf: dict) -> list[str]:
    return [s for s in graf if _typer(graf, s) & {DCAT + "Dataset", DCAT + "DatasetSeries"}]


def _fillankar(graf: dict, dist: str) -> list[Fillank]:
    """Upprepad downloadURL. En titel per fil kan ligga på URL:en som subjekt
    (rekommendation 4) — då skiljer den filerna åt, oftast som ett år."""
    return [Fillank(url=u, titel=_text(graf, u, DCT + "title"))
            for u in _uris(graf, dist, DCAT + "downloadURL")]


def _format(graf: dict, subjekt: str) -> list[str]:
    """Format anges som mediatyp i text eller som URI till EU:s filtypslista
    eller IANA. Båda normaliseras till det som går att läsa ut."""
    ut = []
    for p in (DCT + "format", DCAT + "mediaType"):
        for v in _varden(graf, subjekt, p):
            varde = v.get("value", "")
            # Blanka noder bär ibland formatet som rdfs:label eller rdf:value.
            if v.get("type") == "bnode":
                varde = (_text(graf, varde, "http://www.w3.org/2000/01/rdf-schema#label")
                         or _text(graf, varde, "http://www.w3.org/1999/02/22-rdf-syntax-ns#value"))
            if varde:
                ut.append(varde.rsplit("/", 1)[-1] if "publications.europa.eu" in varde else
                          varde.replace("https://www.iana.org/assignments/media-types/", "")
                               .replace("http://www.iana.org/assignments/media-types/", ""))
    return list(dict.fromkeys(ut))


def _datatjanst(graf: dict, s: str) -> Datatjanst:
    return Datatjanst(
        uri=s,
        titel=_text(graf, s, DCT + "title"),
        endpoint_url=_uris(graf, s, DCAT + "endpointURL"),
        endpoint_beskrivning=_uris(graf, s, DCAT + "endpointDescription"),
        uppfyller=_uris(graf, s, DCT + "conformsTo"),
        typ=_uris(graf, s, DCT + "type"),
        format=_format(graf, s),
    )


def _distribution(graf: dict, s: str) -> Distribution:
    storlek = _text(graf, s, DCAT + "byteSize")
    return Distribution(
        uri=s,
        titel=_text(graf, s, DCT + "title"),
        beskrivning=_text(graf, s, DCT + "description"),
        access_url=_uris(graf, s, DCAT + "accessURL"),
        filer=_fillankar(graf, s),
        format=_format(graf, s),
        uppfyller=_uris(graf, s, DCT + "conformsTo"),
        datatjanster=[_datatjanst(graf, t) for t in _uris(graf, s, DCAT + "accessService")],
        storlek_byte=int(float(storlek)) if storlek.replace(".", "", 1).isdigit() else None,
        andrad=_text(graf, s, DCT + "modified"),
        licens=next(iter(_uris(graf, s, DCT + "license")), ""),
        dokumentation=_uris(graf, s, FOAF + "page"),
    )


def _tidsperiod(graf: dict, s: str) -> tuple[str, str] | None:
    for t in _uris(graf, s, DCT + "temporal"):
        start = (_text(graf, t, DCAT + "startDate") or _text(graf, t, SCHEMA + "startDate"))
        slut = (_text(graf, t, DCAT + "endDate") or _text(graf, t, SCHEMA + "endDate"))
        if start or slut:
            return (start[:10], slut[:10])
    return None


def fran_rdf_json(graf: dict[str, Any], datamangd_uri: str, kalla: str = "") -> Datamangd:
    """Bygger en Datamangd ur en RDF/JSON-graf som innehåller den.

    Distributioner och datatjänster som grafen inte innehåller blir tomma
    skal med bara URI:n. Det är ett besked i sig: katalogen har inte
    levererat beskrivningen, och hämtningsvägen kan då inte avgöras.
    """
    s = datamangd_uri
    utgivare_uri = next(iter(_uris(graf, s, DCT + "publisher")), "")
    utgivare_namn = (_text(graf, utgivare_uri, FOAF + "name")
                     or _text(graf, utgivare_uri, VCARD + "fn")) if utgivare_uri else ""
    return Datamangd(
        uri=s,
        titel=_text(graf, s, DCT + "title"),
        beskrivning=_text(graf, s, DCT + "description"),
        utgivare=Utgivare(uri=utgivare_uri, namn=utgivare_namn),
        nyckelord=_texter(graf, s, DCAT + "keyword"),
        tema=_uris(graf, s, DCAT + "theme"),
        tidsperiod=_tidsperiod(graf, s),
        geografi=_uris(graf, s, DCT + "spatial"),
        utgiven=_text(graf, s, DCT + "issued"),
        andrad=_text(graf, s, DCT + "modified"),
        uppdateringsfrekvens=next(iter(_uris(graf, s, DCT + "accrualPeriodicity")), ""),
        landningssida=_uris(graf, s, DCAT + "landingPage"),
        ar_serie=DCAT + "DatasetSeries" in _typer(graf, s),
        i_serie=_uris(graf, s, DCAT + "inSeries"),
        distributioner=[_distribution(graf, d) for d in _uris(graf, s, DCAT + "distribution")],
        kalla=kalla,
    )
