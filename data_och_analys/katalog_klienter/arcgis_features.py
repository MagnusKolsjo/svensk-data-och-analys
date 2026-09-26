# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Generisk klient för ArcGIS REST FeatureServer / MapServer.

Många svenska myndigheter publicerar geo-data via ESRI:s ArcGIS-stack
istället för OGC-standarderna (WFS, OGC API Features). De exponerar
oftast en REST-katalog under `/arcgis/rest/services/` med folders →
services → layers. ArcGIS FeatureServer-API:t är välspecificerat och
stöder GeoJSON-output, vilket gör det enkelt att nå utan ESRI-klient.

Klienten är generisk över instanser. En "instans" är roten i en
service-katalog (t.ex. `https://<host>/arcgis/rest/services`). Inom en
instans navigeras: folder → service → layer.

REST API i korthet:

    GET /                                       -> folders och rotservices
    GET /{folder}                               -> services i folder
    GET /{folder}/{service}/FeatureServer       -> service-metadata + layers
    GET /{folder}/{service}/FeatureServer/{id}  -> layer-metadata (fields, geometryType)
    GET /{folder}/{service}/FeatureServer/{id}/query  -> query features

Query-parametrar (de mest använda):

    where            SQL-like WHERE-sats (`Lan='STOCKHOLMS LÄN'`)
    outFields        kommaseparerad lista; `*` = alla
    returnGeometry   true (default) | false
    resultOffset     paginering, start
    resultRecordCount  paginering, antal
    f                json (default ESRI-format) | geojson | html
    returnCountOnly  true för att bara få antalet matchande
    inSR / outSR     spatial reference per EPSG-kod

Ingångspunkter:
    lista_instanser()                        -> list[dict]
    lista_folders(instans)                   -> list[str]
    lista_services(instans, folder=None)     -> list[dict]
    lista_layers(instans, service_path)      -> list[dict]
    hamta_layer_info(instans, service_path, layer_id) -> dict
    hamta_count(instans, service_path, layer_id, where="1=1") -> int
    hamta_features(instans, service_path, layer_id, ...) -> dict
    stroma_alla_features(instans, service_path, layer_id, ...) -> async generator
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Instansregister
# ============================================================================


@dataclass(frozen=True)
class ArcGisInstans:
    namn: str
    bas_url: str
    beskrivning: str = ""


# Baslinje. Länsstyrelsernas externa publik-katalog innehåller LST/-foldern
# med EBH-stödet (förorenade områden), miljöriskområden och fler dataset.
# Fler myndigheters ArcGIS-kataloger läggs in i takt med att de blir aktuella.
_BASLINJE: dict[str, ArcGisInstans] = {
    "lst_publik": ArcGisInstans(
        namn="lst_publik",
        bas_url=(
            "https://ext-geodata-nationella-visning.lansstyrelsen.se"
            "/arcgis/rest/services"
        ),
        beskrivning=(
            "Länsstyrelsernas publika ArcGIS REST-katalog — EBH-stödet "
            "(85 000+ förorenade områden), miljöriskområden, "
            "naturvård och kulturmiljö."
        ),
    ),
}


def _med_overstyrning(bas: ArcGisInstans) -> ArcGisInstans:
    overstyrd = os.getenv(f"DOA_ARCGIS_{bas.namn.upper()}_BAS_URL", "").strip()
    if not overstyrd:
        return bas
    return ArcGisInstans(
        namn=bas.namn,
        bas_url=overstyrd.rstrip("/"),
        beskrivning=bas.beskrivning,
    )


def hamta_instans(namn: str) -> ArcGisInstans:
    """Namngiven instans ur registret, eller katalogroten ur en adress
    (allt fram till och med `/rest/services`)."""
    if namn.startswith(("http://", "https://")):
        m = re.match(r"(https?://.+?/rest/services)", namn, re.I)
        if not m:
            raise ValueError(f"Ingen ArcGIS REST-adress: {namn}")
        return ArcGisInstans(namn=urlparse(namn).hostname or namn, bas_url=m.group(1))
    if namn not in _BASLINJE:
        kanda = ", ".join(sorted(_BASLINJE))
        raise ValueError(
            f"okänd ArcGIS-instans: {namn!r}. Tillgängliga: {kanda}"
        )
    return _med_overstyrning(_BASLINJE[namn])


def lista_instanser() -> list[dict[str, str]]:
    return [
        {"namn": i.namn, "bas_url": i.bas_url, "beskrivning": i.beskrivning}
        for namn in sorted(_BASLINJE)
        for i in (_med_overstyrning(_BASLINJE[namn]),)
    ]


# ============================================================================
# HTTP-anrop
# ============================================================================

USER_AGENT = (
    "svensk-data-och-analys/ArcGisClient "
    "(ArcGIS REST; +https://github.com/)"
)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_LANG_TIMEOUT = httpx.Timeout(600.0, connect=30.0)


async def _hamta_json(
    instans: ArcGisInstans,
    sokvag: str,
    parametrar: dict[str, Any] | None = None,
    timeout: httpx.Timeout = _TIMEOUT,
) -> Any:
    params = {"f": "json", **(parametrar or {})}
    url = f"{instans.bas_url}/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url, params=params)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"ArcGIS-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    data = svar.json()
    # ArcGIS returnerar fel som 200 med `error`-fält
    if isinstance(data, dict) and "error" in data:
        f = data["error"]
        raise RuntimeError(
            f"ArcGIS-API-fel ({f.get('code')}) mot {url}: "
            f"{f.get('message')} {f.get('details', '')}"
        )
    return data


# ============================================================================
# Publikt API — navigering
# ============================================================================


async def lista_folders(instans: str = "lst_publik") -> list[str]:
    """Returnerar alla folders i instansens rotkatalog."""
    inst = hamta_instans(instans)
    data = await _hamta_json(inst, "")
    return list(data.get("folders", []))


async def lista_services(
    instans: str = "lst_publik", folder: str | None = None
) -> list[dict[str, str]]:
    """Listar services i en folder (eller på rotnivå om folder=None).

    Varje service har `name` (inkl. folder), `type` (`FeatureServer`,
    `MapServer`, `GPServer` osv).
    """
    inst = hamta_instans(instans)
    data = await _hamta_json(inst, folder or "")
    return list(data.get("services", []))


async def lista_layers(
    instans: str, service_path: str, server_typ: str = "FeatureServer"
) -> list[dict[str, Any]]:
    """Listar lager i en service.

    `service_path` är folder + service-namn separerade med `/`, t.ex.
    `LST/LST_Potentiellt_fororenade_omraden_EBH_EXT`. `server_typ` är
    `FeatureServer` (vanligast) eller `MapServer`.
    """
    inst = hamta_instans(instans)
    data = await _hamta_json(inst, f"{service_path}/{server_typ}")
    return list(data.get("layers", []))


async def hamta_layer_info(
    instans: str,
    service_path: str,
    layer_id: int,
    server_typ: str = "FeatureServer",
) -> dict[str, Any]:
    """Returnerar layer-metadata: fields, geometryType, extent, capabilities."""
    inst = hamta_instans(instans)
    return await _hamta_json(
        inst, f"{service_path}/{server_typ}/{layer_id}"
    )


# ============================================================================
# Publikt API — query
# ============================================================================


async def hamta_count(
    instans: str,
    service_path: str,
    layer_id: int,
    where: str = "1=1",
    server_typ: str = "FeatureServer",
) -> int:
    """Returnerar antalet features som matchar `where`-satsen.

    `returnCountOnly=true` på servern — snabb och billig.
    """
    inst = hamta_instans(instans)
    data = await _hamta_json(
        inst,
        f"{service_path}/{server_typ}/{layer_id}/query",
        parametrar={"where": where, "returnCountOnly": "true"},
    )
    return int(data.get("count", 0))


async def hamta_features(
    instans: str,
    service_path: str,
    layer_id: int,
    where: str = "1=1",
    out_fields: str = "*",
    offset: int = 0,
    count: int = 100,
    format: str = "geojson",
    return_geometry: bool = True,
    out_sr: int | None = None,
    server_typ: str = "FeatureServer",
) -> dict[str, Any]:
    """Hämtar en sida med features ur ett lager.

    `format=geojson` ger standardiserat GeoJSON; `json` ger ESRI:s eget
    JSON-format med `attributes` och `geometry`-objekt.

    `out_sr=4326` reprojicerar till WGS84 vid hämtning — perfekt för
    PostGIS-import. Utelämna eller sätt `4326` för konsekvens med våra
    geom-tabeller.
    """
    inst = hamta_instans(instans)
    parametrar: dict[str, Any] = {
        "where": where,
        "outFields": out_fields,
        "resultOffset": offset,
        "resultRecordCount": count,
        "f": format,
        "returnGeometry": "true" if return_geometry else "false",
    }
    if out_sr is not None:
        parametrar["outSR"] = out_sr
    return await _hamta_json(
        inst,
        f"{service_path}/{server_typ}/{layer_id}/query",
        parametrar=parametrar,
        timeout=_LANG_TIMEOUT,
    )


async def stroma_alla_features(
    instans: str,
    service_path: str,
    layer_id: int,
    where: str = "1=1",
    out_fields: str = "*",
    sida_storlek: int = 1000,
    out_sr: int | None = 4326,
    server_typ: str = "FeatureServer",
) -> AsyncIterator[dict[str, Any]]:
    """Strömmar alla matchande features via paginering.

    ArcGIS sätter typiskt `maxRecordCount` per layer (1000-2000); klienten
    paginerar med `resultOffset` tills tom sida returneras. Yieldar
    enskilda features (GeoJSON Feature-objekt).
    """
    offset = 0
    while True:
        res = await hamta_features(
            instans=instans,
            service_path=service_path,
            layer_id=layer_id,
            where=where,
            out_fields=out_fields,
            offset=offset,
            count=sida_storlek,
            format="geojson",
            out_sr=out_sr,
            server_typ=server_typ,
        )
        features = res.get("features", []) or []
        for f in features:
            yield f
        if len(features) < sida_storlek:
            return
        offset += len(features)
