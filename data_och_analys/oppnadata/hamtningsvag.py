# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Avgör hur en DCAT-AP-SE-distribution hämtas.

Profilen säger bara att `accessURL` är "en webbadress till distributionen".
Den kan vara en fil, en webbsida eller ett API, och i dataportal.se har
bara var tionde distribution en `downloadURL`. Hämtningsvägen måste därför
läsas ut ur allt distributionen bär, i den ordning profilen väger det:

1. `accessService` — datatjänstens `endpointURL`, `conformsTo` (protokoll
   eller standard) och `endpointDescription` (OpenAPI, GetCapabilities).
2. `format` — tjänstemediatyperna `application/vnd.ogc.wfs_xml`, `wms_xml`,
   `wcs_xml` och `wmts_xml` anger protokoll, inte filformat
   (geodatakonventionen i DCAT-AP-SE). `text/html` är en webbsida.
3. `downloadURL` — alltid filer, ibland flera med en titel per period.
4. `accessURL` tolkad efter form: rowstore, CKAN-resurs, Huwise,
   PxWeb, Kolada, DOI, ArcGIS REST — eller som fil när formatet är ett.

Klassningen hämtar ingenting; den säger vart hämtningen ska gå och varför.

Ingångspunkter:
    Hamtningsvag
    klassa(distribution) -> Hamtningsvag
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from data_och_analys.oppnadata.dcat_modell import Distribution, Fillank

# ---------------------------------------------------------------------------
# Vägar
# ---------------------------------------------------------------------------

# typ -> (går att läsa som data, beskrivning)
TYPER: dict[str, tuple[bool, str]] = {
    "fil": (True, "Fil som hämtas och tolkas (CSV, Excel, JSON, GeoJSON)"),
    "rowstore": (True, "EntryScapes rowstore: tabell med filter och sidindelning"),
    "ckan": (True, "CKAN-resurs: fil eller datastore-tabell"),
    "huwise": (True, "Huwise: poster och exporter"),
    "wfs": (True, "OGC WFS: geodata som objekt"),
    "ogc_api_features": (True, "OGC API Features: geodata som GeoJSON"),
    "arcgis_rest": (True, "ArcGIS REST: lager som går att fråga"),
    "pxweb": (True, "PxWeb: statistiktabell — läses med doa-pxweb-2 (SCB) eller doa-pxweb-1"),
    "kolada": (True, "Kolada: nyckeltal för kommuner och regioner — läses med doa-kolada"),
    "researchdata": (True, "Forskningsdata via DOI — läses med doa-katalogs researchdata-verktyg"),
    "karttjanst": (False, "Kartbilder (WMS/WMTS) — visning, inte data"),
    "rastertjanst": (False, "OGC WCS: rasterdata, hämtas inte som tabell"),
    "api": (False, "API beskrivet av datatjänsten; anropet måste formuleras efter dess beskrivning"),
    "webbsida": (False, "Webbsida — data nås genom sidan, inte maskinellt"),
    "okand": (False, "Hämtningsvägen går inte att avgöra ur metadatan"),
}

FILFORMAT = {
    "text/csv": "csv", "csv": "csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx", "xlsx": "xlsx",
    "application/vnd.ms-excel": "xls", "xls": "xls",
    "application/vnd.oasis.opendocument.spreadsheet": "ods", "ods": "ods",
    "application/json": "json", "json": "json",
    "application/geo+json": "geojson", "application/vnd.geo+json": "geojson", "geojson": "geojson",
    "application/zip": "zip", "zip": "zip",
    "application/xml": "xml", "text/xml": "xml", "xml": "xml",
    "application/parquet": "parquet", "application/vnd.apache.parquet": "parquet", "parquet": "parquet",
    "text/plain": "txt", "txt": "txt",
    "application/geopackage+sqlite3": "gpkg", "application/geopackage+vnd.sqlite3": "gpkg",
    "gpkg": "gpkg", "geopackage": "gpkg",
    "application/vnd.google-earth.kml+xml": "kml", "kml": "kml",
    "application/x-shapefile": "shp", "shp": "shp",
}
TJANSTEFORMAT = {
    "application/vnd.ogc.wfs_xml": "wfs", "wfs": "wfs",
    "application/vnd.ogc.wms_xml": "karttjanst", "wms": "karttjanst",
    "application/vnd.ogc.wmts_xml": "karttjanst", "wmts": "karttjanst",
    "application/vnd.ogc.wcs_xml": "rastertjanst", "wcs": "rastertjanst",
}
FILANDELSE = re.compile(r"\.(csv|xlsx|xls|ods|json|geojson|zip|xml|parquet|txt|gpkg|kml)(?:$|\?)", re.I)


class Hamtningsvag(BaseModel):
    typ: str = Field(description="En av: " + ", ".join(TYPER))
    hamtbar: bool = Field(description="Går att läsa som data med suitens verktyg")
    url: str = Field(default="", description="Adressen hämtningen går till")
    filer: list[Fillank] = Field(default_factory=list,
                                 description="Filerna när distributionen har flera")
    format: str = Field(default="", description="Filformat när typen är fil")
    beskrivning_url: str = Field(default="", description="OpenAPI, GetCapabilities eller liknande")
    forklaring: str = ""


# ---------------------------------------------------------------------------
# Klassning
# ---------------------------------------------------------------------------

def _normalisera_format(f: str) -> str:
    f = (f or "").strip().lower()
    f = f.split(";")[0].strip()
    return f.rsplit("/", 1)[-1] if f.startswith("http") and "iana" not in f else f


def _filformat(format_: list[str], url: str = "") -> str:
    for f in format_:
        n = _normalisera_format(f)
        if n in FILFORMAT:
            return FILFORMAT[n]
        # text/csv+zip, application/json+zip: en packad fil.
        if n.endswith("+zip"):
            return "zip"
    m = FILANDELSE.search(urlparse(url).path + ("?" if "?" in url else ""))
    return m.group(1).lower() if m else ""


def _efter_adress(url: str) -> str:
    """Hämtningsväg ur adressens form. Tom sträng när formen inte säger något."""
    u = url.lower()
    vard = urlparse(u).hostname or ""
    if "/rowstore/dataset/" in u:
        return "rowstore"
    if re.search(r"/dataset/[^/]+/resource/", u) or "/api/3/action/" in u:
        return "ckan"
    if "/explore/dataset/" in u or "/api/explore/" in u or "/api/records/" in u:
        return "huwise"
    if "service=wfs" in u or re.search(r"/wfs(\?|/|$)", u):
        return "wfs"
    if "service=wms" in u or "service=wmts" in u or re.search(r"/(wms|wmts)(\?|/|$)", u) or "wmsserver" in u:
        return "karttjanst"
    if "/collections" in u or "/ogc/features" in u:
        return "ogc_api_features"
    if "/arcgis/rest/services" in u or "/featureserver" in u:
        return "arcgis_rest"
    if "pxweb" in u or re.search(r"/api/v[12]/(sv|en)/", u):
        return "pxweb"
    if vard.endswith("kolada.se"):
        return "kolada"
    if vard in ("doi.org", "dx.doi.org") or vard.endswith("researchdata.se"):
        return "researchdata"
    return ""


def _efter_tjanst(tjanst) -> tuple[str, str]:
    """Hämtningsväg ur en datatjänst: (typ, beskrivnings-URL)."""
    beskrivning = next(iter(tjanst.endpoint_beskrivning), "")
    tecken = " ".join(tjanst.uppfyller + tjanst.typ + tjanst.format + tjanst.endpoint_url
                      + tjanst.endpoint_beskrivning).lower()
    # Protokollkoderna ur geodatakonventionen: HTTP:OGC:WFS, HTTP:OGC:API-Features …
    if "api-features" in tecken or "ogcapi-features" in tecken:
        return "ogc_api_features", beskrivning
    if "ogc:wfs" in tecken or "wfs_xml" in tecken:
        return "wfs", beskrivning
    if "ogc:wms" in tecken or "wms_xml" in tecken or "wmts" in tecken or "api-maps" in tecken:
        return "karttjanst", beskrivning
    if "ogc:wcs" in tecken or "wcs_xml" in tecken:
        return "rastertjanst", beskrivning
    for u in tjanst.endpoint_url:
        typ = _efter_adress(u)
        if typ:
            return typ, beskrivning
    if "nedladdning" in tecken and "atom" in tecken:
        return "fil", beskrivning
    return "api", beskrivning


def klassa(dist: Distribution) -> Hamtningsvag:
    """Hämtningsväg för en distribution, med en förklaring till valet."""
    access = next(iter(dist.access_url), "")

    def vag(typ: str, url: str, forklaring: str, **extra) -> Hamtningsvag:
        return Hamtningsvag(typ=typ, hamtbar=TYPER[typ][0], url=url,
                            forklaring=forklaring, **extra)

    # 1. Datatjänst. En typad tjänst väger tyngst; en otypad med bara en
    # OpenAPI-beskrivning får vänta — distributionen kan ha en fil som är
    # enklare att läsa.
    for t in dist.datatjanster:
        typ, beskrivning = _efter_tjanst(t)
        if typ != "api":
            url = next(iter(t.endpoint_url), access)
            return vag(typ, url, f"Datatjänsten anger {typ} ({', '.join(t.uppfyller) or 'endpointURL'}).",
                       beskrivning_url=beskrivning)

    # 2. Tjänstemediatyp i formatet.
    for f in dist.format:
        n = _normalisera_format(f)
        if n in TJANSTEFORMAT:
            typ = TJANSTEFORMAT[n]
            return vag(typ, access, f"Formatet {f} anger protokoll, inte fil.")

    html = any(_normalisera_format(f) in ("text/html", "html") for f in dist.format)

    # 3. downloadURL är alltid filer — enligt profilen. Utgivare pekar ändå
    # ibland downloadURL på en webbsida och anger text/html. Motsäger formatet
    # egenskapen, och adressen inte har en filändelse, gäller formatet.
    if dist.filer and html and not _filformat([], dist.filer[0].url):
        return vag("webbsida", dist.filer[0].url,
                   "downloadURL anges men formatet är text/html och adressen saknar filändelse.")
    if dist.filer:
        fmt = _filformat(dist.format, dist.filer[0].url) or "okänt"
        typ_ur_adress = _efter_adress(dist.filer[0].url)
        if typ_ur_adress in ("rowstore", "huwise"):
            return vag(typ_ur_adress, dist.filer[0].url, "downloadURL pekar på plattformens API.")
        return vag("fil", dist.filer[0].url,
                   f"{len(dist.filer)} fil(er) via downloadURL.",
                   filer=dist.filer, format=fmt)

    # 4. accessURL efter form.
    if access:
        typ = _efter_adress(access)
        if typ:
            return vag(typ, access, f"accessURL har formen för {typ}.")
        fmt = _filformat(dist.format, access)
        if fmt and not html:
            return vag("fil", access, f"accessURL med filformatet {fmt}.",
                       filer=[Fillank(url=access, titel=dist.titel)], format=fmt)
        if html:
            return vag("webbsida", access, "Formatet är text/html.")

    # Otypad datatjänst med API-beskrivning: rekommendation 13 i DCAT-AP-SE.
    for t in dist.datatjanster:
        url = next(iter(t.endpoint_url), access)
        return vag("api", url, "Datatjänst utan känt protokoll.",
                   beskrivning_url=next(iter(t.endpoint_beskrivning or dist.uppfyller), ""))

    if access:
        return vag("okand", access, "Varken format, datatjänst eller adress avgör vad accessURL är. "
                                    "Hämtningen kan pröva adressen och avgöra ur svarets innehållstyp.")
    return vag("okand", "", "Distributionen saknar adress.")
