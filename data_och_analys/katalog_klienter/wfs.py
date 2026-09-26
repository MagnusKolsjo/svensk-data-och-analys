# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Generisk klient för OGC Web Feature Service (WFS).

WFS är OGC:s äldre standardprotokoll för geo-data (efterföljaren OGC API
Features hanteras av `ogc_api_features`-klienten). Många svenska myndigheter
publicerar fortfarande primärt via WFS — SCB, SMHI, Trafikverket,
Naturvårdsverket m.fl. — så en generell klient är det som öppnar mest data.

Mönstret är samma som övriga katalogklienter: ett instansregister med
bas-URL per myndighet, env-överstyrning per instans, generiska operations.

WFS-protokoll i korthet:

    GET ?service=WFS&request=GetCapabilities    -> XML med tjänstemetadata
    GET ?service=WFS&request=DescribeFeatureType -> schema för ett lager
    GET ?service=WFS&request=GetFeature&typeNames=<lager> -> features

Klienten talar WFS 2.0.0 som default — GeoServer-baserade tjänster
(vilket de flesta svenska är) stöder båda 1.1.0 och 2.0.0. För
output-format begär klienten `application/json` när det är möjligt —
GeoServer ger då GeoJSON med automatisk reprojicering om `srsName`
specificeras.

Ingångspunkter:
    lista_instanser() -> list[dict]
    lista_lager(instans) -> list[dict]
    hamta_features(instans, type_name, bbox=None, count=1000, ...) -> dict
    stroma_alla_features(instans, type_name, ...) -> async generator
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, urlparse

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Instansregister
# ============================================================================


@dataclass(frozen=True)
class WfsInstans:
    namn: str
    bas_url: str
    version: str = "2.0.0"
    sprak: str | None = None
    beskrivning: str = ""
    # Parametrar som måste följa med varje anrop. De får inte ligga i
    # bas_url: httpx ersätter en befintlig frågesträng när `params` skickas
    # med, så en URL med `?map=...` tappar den vid första anropet. Det är
    # sådana MapServer-CGI-installationer som behöver det här — Socialstyrelsen
    # svarar "CGI variable map is not set" utan den.
    fasta_parametrar: tuple[tuple[str, str], ...] = ()
    # Utformat som instansen faktiskt kan leverera. GeoServer klarar GeoJSON;
    # MapServer-installationer gör det ofta inte och svarar då
    # "is not a permitted output format". Sätts det till ett GML-format
    # konverterar klienten svaret, så att kontraktet mot anroparen förblir
    # GeoJSON oavsett vad servern talar.
    utformat: str = "application/json"


# Baslinje. Fler myndigheter läggs in när deras WFS-endpoints är aktuella.
# SCB:s "stat"-workspace innehåller DeSO, RegSO, tätorter, befolkningsrutor
# m.fl. — 49 lager totalt. Andra svenska WFS-endpoints i bruk:
#   - SMHI:    https://opendata-view.smhi.se/SMHI_<tema>/<tema>/wfs
#   - Trafikverket / Naturvårdsverket har egna geoservrar
_BASLINJE: dict[str, WfsInstans] = {
    "scb_stat": WfsInstans(
        namn="scb_stat",
        bas_url="https://geodata.scb.se/geoserver/stat/wfs",
        version="2.0.0",
        beskrivning=(
            "SCB:s öppna geodata, workspace 'stat': DeSO, RegSO, tätorter, "
            "småorter, befolkning per 1km-ruta m.fl. — 49 lager."
        ),
    ),
    "sos_skador": WfsInstans(
        namn="sos_skador",
        # MapServer-CGI: map-parametern är obligatorisk och måste ligga i
        # bas-URL:en. Utan den svarar servern "CGI variable map is not set",
        # vilket är varför endpointen såg bruten ut. Sökvägen står i
        # Geodataportalens metadata, inte på Socialstyrelsens egna sidor.
        bas_url="https://geodata.socialstyrelsen.se/cgi/mapserv.exe",
        fasta_parametrar=(("map", "/ms4w/maps/sos-ska-wfs.map"),),
        utformat="text/xml; subtype=gml/3.2.1",
        version="2.0.0",
        beskrivning=(
            "Socialstyrelsens skador och förgiftningar, INSPIRE-namngivna "
            "lager på formen SE.HH.SKA.<diagnoskod>.<MEN|WOMEN|TOTAL>.<år>. "
            "Områdena är NUTS3 (RegionCode 'SE110'), inte SCB:s länskoder — "
            "joina via nuts-tabellen, inte direkt mot lan."
        ),
    ),
    "smhi_vatten": WfsInstans(
        namn="smhi_vatten",
        bas_url="https://opendata-view.smhi.se/SMHI_vatten/wfs",
        version="2.0.0",
        beskrivning=(
            "SMHI:s vattendata: avrinningsområden, delavrinningsområden, "
            "vattendistrikt, vattenförekomster, BARO m.m. — 26 lager."
        ),
    ),
    "nv_skyddad": WfsInstans(
        namn="nv_skyddad",
        bas_url="https://geodata.naturvardsverket.se/inspire/ps/wfs",
        version="2.0.0",
        beskrivning=(
            "Naturvårdsverket Skyddad natur (INSPIRE Protected Sites): "
            "alla skyddade områden i ett lager (`ps:ProtectedSite`)."
        ),
    ),
    "sjv_inspire": WfsInstans(
        namn="sjv_inspire",
        bas_url="https://epub.sjv.se/inspire/wfs",
        version="2.0.0",
        beskrivning=(
            "Jordbruksverkets INSPIRE-feed: blockindelning (årslager), "
            "skiftesindelning, artdata, erosion, jordbruksområden, "
            "stödområden m.fl. — 28 lager."
        ),
    ),
}


def _med_overstyrning(bas: WfsInstans) -> WfsInstans:
    overstyrd = os.getenv(f"DOA_WFS_{bas.namn.upper()}_BAS_URL", "").strip()
    if not overstyrd:
        return bas
    return WfsInstans(
        namn=bas.namn,
        bas_url=overstyrd.rstrip("/"),
        version=bas.version,
        sprak=bas.sprak,
        beskrivning=bas.beskrivning,
    )


# Parametrar som klienten själv sätter. Följer de med i en adress ur en
# DCAT-distribution skulle de krocka med anropets egna.
_WFS_EGNA = {"service", "request", "version", "typename", "typenames", "outputformat",
             "srsname", "count", "maxfeatures", "startindex", "bbox", "cql_filter"}


def _instans_ur_adress(url: str) -> WfsInstans:
    """En WFS ur en adress, som den står i en distribution eller datatjänst.

    Frågesträngens övriga parametrar — MapServers `map=` till exempel —
    följer med som fasta parametrar; utan dem svarar vissa servrar inte.
    """
    p = urlparse(url)
    fasta = tuple((k, v) for k, v in parse_qsl(p.query) if k.lower() not in _WFS_EGNA)
    return WfsInstans(namn=p.hostname or url, bas_url=f"{p.scheme}://{p.netloc}{p.path}",
                      fasta_parametrar=fasta)


def hamta_instans(namn: str) -> WfsInstans:
    """Namngiven instans ur registret, eller en instans ur en adress."""
    if namn.startswith(("http://", "https://")):
        return _instans_ur_adress(namn)
    if namn not in _BASLINJE:
        kanda = ", ".join(sorted(_BASLINJE))
        raise ValueError(f"okänd WFS-instans: {namn!r}. Tillgängliga: {kanda}")
    return _med_overstyrning(_BASLINJE[namn])


def lista_instanser() -> list[dict[str, str]]:
    return [
        {
            "namn": i.namn,
            "bas_url": i.bas_url,
            "version": i.version,
            "beskrivning": i.beskrivning,
        }
        for namn in sorted(_BASLINJE)
        for i in (_med_overstyrning(_BASLINJE[namn]),)
    ]


# ============================================================================
# HTTP-anrop
# ============================================================================

USER_AGENT = "svensk-data-och-analys/WfsClient (OGC WFS; +https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_LANG_TIMEOUT = httpx.Timeout(600.0, connect=30.0)


async def _hamta(
    instans: WfsInstans,
    parametrar: dict[str, Any],
    accept: str = "application/json",
    timeout: httpx.Timeout = _TIMEOUT,
) -> httpx.Response:
    parametrar.setdefault("service", "WFS")
    parametrar.setdefault("version", instans.version)
    for nyckel, varde in instans.fasta_parametrar:
        parametrar.setdefault(nyckel, varde)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    ) as klient:
        svar = await klient.get(instans.bas_url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"WFS-anrop misslyckades ({svar.status_code}) mot "
            f"{svar.request.url}: {svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar


# ============================================================================
# Capabilities-parsning
# ============================================================================


def _parsa_capabilities(xml: str) -> list[dict[str, str]]:
    """Plockar ut FeatureType-listan ur WFS GetCapabilities-XML.

    Använder regex istället för full XML-parsing — capabilities är stort
    och vi behöver bara fyra fält per lager: Name, Title, Abstract,
    DefaultCRS/DefaultSRS. Regex är pragmatiskt eftersom WFS-strukturen
    är välspecificerad och vi inte hanterar attribut-edge-cases.
    """
    lager: list[dict[str, str]] = []
    for blob in re.findall(r"<FeatureType\b[^>]*>(.*?)</FeatureType>", xml, re.DOTALL):
        post: dict[str, str] = {}
        for falt, mall in [
            ("name", r"<Name>([^<]+)</Name>"),
            ("titel", r"<Title>([^<]+)</Title>"),
            ("abstrakt", r"<Abstract>([^<]+)</Abstract>"),
            ("default_crs", r"<DefaultCRS>([^<]+)</DefaultCRS>"),
        ]:
            m = re.search(mall, blob)
            if m:
                post[falt] = m.group(1).strip()
        # WFS 1.1.0 använder DefaultSRS, 2.0.0 DefaultCRS — fall back
        if "default_crs" not in post:
            m = re.search(r"<DefaultSRS>([^<]+)</DefaultSRS>", blob)
            if m:
                post["default_crs"] = m.group(1).strip()
        if post.get("name"):
            lager.append(post)
    return lager


# ============================================================================
# Publikt API
# ============================================================================


async def lista_lager(instans: str = "scb_stat") -> list[dict[str, str]]:
    """Returnerar alla FeatureType (lager) som tjänsten erbjuder.

    Varje rad har `name` (det som används som type_name i hämtningar),
    `titel`, `abstrakt` och `default_crs`.
    """
    inst = hamta_instans(instans)
    svar = await _hamta(inst, {"request": "GetCapabilities"}, accept="application/xml")
    return _parsa_capabilities(svar.text)


def _query_per_version(version: str) -> tuple[str, str]:
    """Returnerar (typename-param-namn, count-param-namn) per WFS-version."""
    if version.startswith("2"):
        return ("typeNames", "count")
    return ("typeName", "maxFeatures")


async def hamta_features(
    instans: str,
    type_name: str,
    bbox: tuple[float, float, float, float] | None = None,
    count: int = 1000,
    start_index: int | None = None,
    srs: str = "EPSG:4326",
    output_format: str = "application/json",
    cql_filter: str | None = None,
) -> dict[str, Any]:
    """Hämtar en sida med features.

    `type_name` är fullständigt namn inkl. workspace, t.ex. `stat:DeSO_2025`.
    `bbox` är (minx, miny, maxx, maxy) i målet `srs`. `cql_filter` är
    GeoServers CQL-syntax för enkel filtrering (t.ex. `lanskod='01'`).

    Returnerar svaret som dict (GeoJSON FeatureCollection).
    """
    inst = hamta_instans(instans)
    tn_param, count_param = _query_per_version(inst.version)
    parametrar: dict[str, Any] = {
        "request": "GetFeature",
        tn_param: type_name,
        count_param: count,
        "srsName": srs,
        "outputFormat": output_format,
    }
    if start_index is not None:
        parametrar["startIndex"] = start_index
    if bbox is not None:
        parametrar["bbox"] = ",".join(str(x) for x in bbox) + f",{srs}"
    if cql_filter is not None:
        parametrar["CQL_FILTER"] = cql_filter
    if output_format == "application/json" and inst.utformat != "application/json":
        parametrar["outputFormat"] = inst.utformat
    svar = await _hamta(inst, parametrar, timeout=_LANG_TIMEOUT)
    if "json" in (parametrar.get("outputFormat") or "").lower():
        return svar.json()
    return _gml_till_geojson(svar.content, type_name)


def _gml_till_geojson(rå: bytes, type_name: str) -> dict[str, Any]:
    """Konverterar ett GML-svar till GeoJSON.

    Anroparen ska inte behöva veta vilket format servern talar. Konverteringen
    går via geopandas GDAL-läsare, som förstår GML 2 och 3.
    """
    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".gml", delete=False) as f:
        f.write(rå)
        sokvag = f.name
    try:
        ram = gpd.read_file(sokvag)
    except Exception as fel:  # noqa: BLE001
        raise ValueError(
            f"kunde inte tolka GML-svaret för {type_name!r} ({fel}). "
            "Instansen levererar inte GeoJSON och GML-konverteringen "
            "misslyckades — kontrollera lagernamnet."
        ) from fel
    finally:
        Path(sokvag).unlink(missing_ok=True)

    if ram.crs is not None and ram.crs.to_epsg() != 4326:
        ram = ram.to_crs(epsg=4326)
    return json.loads(ram.to_json())


async def stroma_alla_features(
    instans: str,
    type_name: str,
    bbox: tuple[float, float, float, float] | None = None,
    sida_storlek: int = 1000,
    srs: str = "EPSG:4326",
    cql_filter: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Strömmar alla features via paginering (startIndex + count).

    Yieldar enskilda features. GeoServer rapporterar `totalFeatures`
    (1.1.0) eller `numberMatched` (2.0.0) — vi använder det för att veta
    när vi är klara. Default sidstorlek 1000; SCB:s GeoServer hanterar
    det utan problem.
    """
    inst = hamta_instans(instans)
    offset = 0
    while True:
        res = await hamta_features(
            instans=instans,
            type_name=type_name,
            bbox=bbox,
            count=sida_storlek,
            start_index=offset,
            srs=srs,
            cql_filter=cql_filter,
        )
        features = res.get("features", [])
        for f in features:
            yield f
        total = res.get("numberMatched") or res.get("totalFeatures")
        antal_nu = offset + len(features)
        if not features or (total is not None and antal_nu >= total):
            return
        offset = antal_nu
