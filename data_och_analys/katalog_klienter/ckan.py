# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Klient för CKAN Action API v3.

CKAN används av bland annat Malmö, Ängelholm, Båstad och Svenska kraftnät.
Klienten söker paket, hämtar dem och översätter dem till suitens
DCAT-AP-SE-modell, så att resten av suiten inte behöver känna CKAN:s form:

- ett paket blir en datamängd, en resurs en distribution
- en uppladdad resurs (`url_type = upload`) är en fil och blir
  `downloadURL`; en länkad resurs kan vara vad som helst och blir
  `accessURL`
- en resurs med aktiv datastore blir dessutom en datatjänst, eftersom den
  går att fråga rad för rad med `datastore_search`

Ingångspunkter:
    sok(bas_url, fraga, limit=20, offset=0) -> dict
    hamta_datamangd(bas_url, paket_id) -> Datamangd
    datastore_rader(bas_url, resurs_id, filter=None, fritext=None, limit=100, offset=0) -> dict
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from data_och_analys.oppnadata.dcat_modell import (
    Datamangd, Datatjanst, Distribution, Fillank, Utgivare,
)

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/CkanClient (DCAT-AP-SE; +https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
DATASTORE_MAX_PER_ANROP = 1000


async def _action(bas_url: str, action: str, parametrar: dict[str, Any]) -> Any:
    url = f"{bas_url.rstrip('/')}/api/3/action/{action}"
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as klient:
        svar = await klient.get(url, params=parametrar)
    try:
        d = svar.json()
    except ValueError:
        raise ValueError(f"CKAN svarade {svar.status_code} utan JSON på {url}") from None
    if not d.get("success"):
        fel = d.get("error") or {}
        raise ValueError(f"CKAN {action} misslyckades: {fel.get('message') or fel}")
    return d["result"]


async def sok(bas_url: str, fraga: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Paket som matchar fritexten. Returnerar `{"total", "traffar"}`."""
    r = await _action(bas_url, "package_search",
                      {"q": fraga or "*:*", "rows": limit, "start": offset,
                       "sort": "metadata_modified desc"})
    return {"total": r.get("count", 0), "traffar": [
        {"id": p.get("name") or p.get("id"),
         "titel": p.get("title", ""),
         "beskrivning": (p.get("notes") or "")[:500],
         "utgivare": (p.get("organization") or {}).get("title", ""),
         "andrad": p.get("metadata_modified", ""),
         "antal_resurser": len(p.get("resources", []))}
        for p in r.get("results", [])]}


def _distribution(bas_url: str, r: dict) -> Distribution:
    url = r.get("url", "")
    format_ = [f for f in (r.get("mimetype"), r.get("format")) if f]
    uppladdad = r.get("url_type") == "upload"
    tjanster = []
    if r.get("datastore_active"):
        tjanster.append(Datatjanst(
            titel="CKAN datastore",
            endpoint_url=[f"{bas_url.rstrip('/')}/api/3/action/datastore_search?resource_id={r['id']}"],
            uppfyller=["CKAN datastore_search"]))
    return Distribution(
        uri=f"{bas_url.rstrip('/')}/dataset/{r.get('package_id', '')}/resource/{r.get('id', '')}",
        titel=r.get("name") or "",
        beskrivning=(r.get("description") or "")[:500],
        access_url=[url] if url else [],
        filer=[Fillank(url=url, titel=r.get("name") or "")] if uppladdad and url else [],
        format=format_,
        datatjanster=tjanster,
        storlek_byte=r.get("size") if isinstance(r.get("size"), int) else None,
        andrad=r.get("last_modified") or r.get("metadata_modified") or "",
    )


async def hamta_datamangd(bas_url: str, paket_id: str) -> Datamangd:
    p = await _action(bas_url, "package_show", {"id": paket_id})
    org = p.get("organization") or {}
    return Datamangd(
        uri=f"{bas_url.rstrip('/')}/dataset/{p.get('name') or p.get('id')}",
        titel=p.get("title", ""),
        beskrivning=p.get("notes") or "",
        utgivare=Utgivare(uri=org.get("name", ""), namn=org.get("title", "")),
        nyckelord=[t.get("display_name") or t.get("name") for t in p.get("tags", [])],
        utgiven=p.get("metadata_created", ""),
        andrad=p.get("metadata_modified", ""),
        landningssida=[f"{bas_url.rstrip('/')}/dataset/{p.get('name')}"],
        distributioner=[_distribution(bas_url, r) for r in p.get("resources", [])],
        kalla=bas_url,
    )


async def hamta_resurs(bas_url: str, resurs_id: str) -> dict[str, Any]:
    """En resurs: adress, format och om den har en datastore-tabell."""
    return await _action(bas_url, "resource_show", {"id": resurs_id})


async def datastore_rader(bas_url: str, resurs_id: str, filter: dict[str, Any] | None = None,
                          fritext: str | None = None, limit: int = 100,
                          offset: int = 0) -> dict[str, Any]:
    """Rader ur en datastore-tabell. `filter` är `{kolumn: värde}` med exakt matchning."""
    import json
    parametrar: dict[str, Any] = {"resource_id": resurs_id,
                                  "limit": min(limit, DATASTORE_MAX_PER_ANROP), "offset": offset}
    if filter:
        parametrar["filters"] = json.dumps(filter, ensure_ascii=False)
    if fritext:
        parametrar["q"] = fritext
    r = await _action(bas_url, "datastore_search", parametrar)
    kolumner = [f["id"] for f in r.get("fields", []) if f["id"] != "_id"]
    return {"antal_totalt": r.get("total"), "kolumner": kolumner,
            "rader": [{k: v for k, v in rad.items() if k != "_id"} for rad in r.get("records", [])]}
