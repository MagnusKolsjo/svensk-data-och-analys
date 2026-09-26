# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för ArcGIS Hub — kommunala öppna data-webbplatser på Esris plattform.

Jönköping, Kalmar, Luleå, Uppsala och Region Gotland publicerar via Hub.
En Hub-webbplats är en vy över organisationens poster i ArcGIS Online;
datat ligger i organisationens egna ArcGIS-tjänster.

Sökningen går mot webbplatsens eget sök-API
(`/api/search/v1/collections/dataset/items`), som bara ser webbplatsens
poster. Hubs globala v3-API söker över hela ArcGIS Online och ger
tusentals träffar från andra länder.

En post med en feature-tjänst blir en datamängd med tre distributioner:
tjänstelagret (frågbart via ArcGIS REST) och Hubs nedladdningar som CSV och
GeoJSON. Nedladdningarna är enklast att tolka; lagret är rätt när urvalet
ska göras i källan.

Ingångspunkter:
    sok(bas_url, fraga, limit=20, offset=0) -> dict
    hamta_datamangd(bas_url, post_id) -> Datamangd
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from data_och_analys.oppnadata.dcat_modell import (
    Datamangd, Datatjanst, Distribution, Fillank, Utgivare,
)

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/ArcGisHubClient (DCAT-AP-SE; +https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_POST_API = "https://www.arcgis.com/sharing/rest/content/items"
_TJANSTTYPER = ("Feature Service", "Map Service")


async def _hamta_json(url: str, parametrar: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise ValueError(f"ArcGIS Hub svarade {svar.status_code} på {url}")
    d = svar.json()
    if isinstance(d, dict) and d.get("error"):
        raise ValueError(f"ArcGIS svarade med fel på {url}: {d['error'].get('message')}")
    return d


def _datum(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _text(html_: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_ or "")).strip()


async def sok(bas_url: str, fraga: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Webbplatsens datamängder som matchar fritexten."""
    parametrar: dict[str, Any] = {"limit": limit, "startindex": offset + 1}
    if fraga.strip():
        parametrar["q"] = fraga
    d = await _hamta_json(f"{bas_url.rstrip('/')}/api/search/v1/collections/dataset/items",
                          parametrar)
    traffar = []
    for f in d.get("features", []):
        p = f.get("properties") or {}
        traffar.append({"id": p.get("id", ""), "titel": p.get("title", ""),
                        "beskrivning": _text(p.get("description") or p.get("snippet"))[:500],
                        "typ": p.get("type", ""), "andrad": _datum(p.get("modified")),
                        "utgivare": p.get("source") or p.get("owner", "")})
    return {"total": d.get("numberMatched", len(traffar)), "traffar": traffar}


async def _lager(tjanst_url: str) -> list[tuple[int, str]]:
    try:
        d = await _hamta_json(tjanst_url, {"f": "json"})
    except (ValueError, httpx.HTTPError):
        return []
    return [(l["id"], l.get("name", "")) for l in d.get("layers", [])]


async def hamta_datamangd(bas_url: str, post_id: str) -> Datamangd:
    """En Hub-post som datamängd med tjänstelager och nedladdningar."""
    post_id = post_id.split("_")[0]
    p = await _hamta_json(f"{_POST_API}/{post_id}", {"f": "json"})
    site = bas_url.rstrip("/")
    distributioner: list[Distribution] = []
    url = (p.get("url") or "").rstrip("/")
    if p.get("type") in _TJANSTTYPER and url:
        lager = [(0, p.get("title", ""))] if re.search(r"/\d+$", url) is None and \
            "Singlelayer" in (p.get("typeKeywords") or []) else await _lager(url)
        if re.search(r"/\d+$", url):
            lager = [(int(url.rsplit("/", 1)[1]), p.get("title", ""))]
            url = url.rsplit("/", 1)[0]
        for lager_id, lager_namn in lager:
            tjanst = Datatjanst(titel=f"ArcGIS REST: {lager_namn}",
                                endpoint_url=[f"{url}/{lager_id}"],
                                uppfyller=["ArcGIS REST API"])
            distributioner.append(Distribution(
                titel=f"{lager_namn} (tjänstelager)", access_url=[f"{url}/{lager_id}"],
                format=["ArcGIS REST"], datatjanster=[tjanst]))
            for fmt, mediatyp in (("csv", "text/csv"), ("geojson", "application/geo+json")):
                nedladdning = (f"{site}/api/download/v1/items/{post_id}/{fmt}"
                               f"?redirect=true&layers={lager_id}")
                distributioner.append(Distribution(
                    titel=f"{lager_namn} ({fmt.upper()})", access_url=[nedladdning],
                    filer=[Fillank(url=nedladdning, titel=lager_namn)], format=[mediatyp]))
    elif p.get("type"):
        # Filposter (CSV, Excel, PDF) ligger som data på själva posten.
        data = f"{_POST_API}/{post_id}/data"
        distributioner.append(Distribution(
            titel=p.get("name") or p.get("title", ""), access_url=[data],
            filer=[Fillank(url=data, titel=p.get("name") or "")], format=[p.get("type", "")]))
    return Datamangd(
        uri=f"{site}/datasets/{post_id}",
        titel=p.get("title", ""),
        beskrivning=_text(p.get("description") or p.get("snippet")),
        utgivare=Utgivare(namn=p.get("accessInformation") or p.get("owner", "")),
        nyckelord=list(p.get("tags") or []),
        utgiven=_datum(p.get("created")),
        andrad=_datum(p.get("modified")),
        landningssida=[f"{site}/datasets/{post_id}"],
        distributioner=distributioner,
        kalla=site,
    )
