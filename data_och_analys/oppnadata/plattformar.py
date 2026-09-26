# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Sök och hämtning över katalogplattformarna, med registret som karta.

Varje katalog i registret söks i sin egen plattform: EntryScapes store,
CKAN:s `package_search`, Huwise katalog, ArcGIS Hubs webbplatssök eller den
publicerade DCAT-filen. Sökningen går parallellt, och en katalog som inte
svarar redovisas med sitt fel — noll träffar och "gick inte att fråga" är
olika besked.

En träff bär ett id på formen `plattform|adress|nyckel…`. Det räcker för
att hämta datamängden igen utan att söka om, och det är det enda verktygen
behöver skicka vidare.

Ingångspunkter:
    Traff, Katalogsvar, Sokresultat
    sok(kataloger, fraga, per_katalog=5) -> Sokresultat
    hamta_datamangd(datamangd_id) -> Datamangd
"""

from __future__ import annotations

import asyncio
import logging
import re

from pydantic import BaseModel, Field

from data_och_analys.katalog_klienter import arcgis_hub, ckan, dcat_fil, entryscape, huwise
from data_och_analys.oppnadata.dcat_modell import (
    Datamangd, Distribution, Fillank, Utgivare,
)
from data_och_analys.oppnadata.register import Katalog

logger = logging.getLogger(__name__)

SEPARATOR = "|"
_SAMTIDIGA_KATALOGER = 12
_TIDSGRANS_SEKUNDER = 45


class Traff(BaseModel):
    id: str = Field(description="Skickas till oppnadata_hamta_datamangd")
    titel: str
    beskrivning: str = ""
    andrad: str = ""
    organisation: str
    kategori: str
    plattform: str


class Katalogsvar(BaseModel):
    organisation: str
    plattform: str
    katalog: str
    antal_traffar: int | None = Field(default=None, description="Totalt i katalogen; None vid fel")
    fel: str = ""


class Sokresultat(BaseModel):
    traffar: list[Traff]
    kataloger: list[Katalogsvar]


def _id(*delar: str) -> str:
    return SEPARATOR.join(delar)


# ---------------------------------------------------------------------------
# Sök per plattform
# ---------------------------------------------------------------------------

async def _sok_en(k: Katalog, fraga: str, antal: int) -> tuple[int, list[Traff]]:
    def traff(id_: str, t: dict) -> Traff:
        return Traff(id=id_, titel=t.get("titel", ""), beskrivning=t.get("beskrivning", ""),
                     andrad=str(t.get("andrad", ""))[:10], organisation=k.namn,
                     kategori=k.kategori, plattform=k.plattform)

    if k.plattform == "entryscape":
        r = await entryscape.sok(k.bas_url, fraga, kontext=k.kontext, limit=antal)
        return r["total"], [traff(_id("entryscape", k.bas_url, t["kontext"], t["post"]), t)
                            for t in r["traffar"]]
    if k.plattform == "ckan":
        r = await ckan.sok(k.bas_url, fraga, limit=antal)
        return r["total"], [traff(_id("ckan", k.bas_url, t["id"]), t) for t in r["traffar"]]
    if k.plattform == "huwise":
        r = await huwise.lista_datasets(k.bas_url, query=fraga or None, limit=antal)
        ut = []
        for d in r.get("results", []):
            meta = (d.get("metas") or {}).get("default") or {}
            ut.append(traff(_id("huwise", k.bas_url, d["dataset_id"]),
                            {"titel": meta.get("title", d["dataset_id"]),
                             "beskrivning": re.sub(r"<[^>]+>", " ", meta.get("description") or "")[:500],
                             "andrad": meta.get("modified", "")}))
        return r.get("total_count", len(ut)), ut
    if k.plattform == "arcgis_hub":
        r = await arcgis_hub.sok(k.bas_url, fraga, limit=antal)
        return r["total"], [traff(_id("arcgis_hub", k.bas_url, t["id"]), t) for t in r["traffar"]]
    if k.plattform == "dcat_fil":
        url = k.katalog_url or k.bas_url
        r = await dcat_fil.sok(url, fraga, limit=antal)
        return r["total"], [traff(_id("dcat_fil", url, t["uri"]), t) for t in r["traffar"]]
    raise ValueError(f"Plattformen {k.plattform} kan inte sökas")


async def sok(kataloger: list[Katalog], fraga: str, per_katalog: int = 5) -> Sokresultat:
    """Söker i alla kataloger samtidigt, högst `per_katalog` träffar ur varje."""
    grans = asyncio.Semaphore(_SAMTIDIGA_KATALOGER)

    async def en(k: Katalog) -> tuple[Katalogsvar, list[Traff]]:
        katalog = k.bas_url + (f" (kontext {k.kontext})" if k.kontext else "")
        async with grans:
            try:
                total, traffar = await asyncio.wait_for(_sok_en(k, fraga, per_katalog),
                                                        _TIDSGRANS_SEKUNDER)
                return Katalogsvar(organisation=k.namn, plattform=k.plattform, katalog=katalog,
                                   antal_traffar=total), traffar
            except asyncio.TimeoutError:
                fel = f"svarade inte inom {_TIDSGRANS_SEKUNDER} s"
            except Exception as e:  # noqa: BLE001 — en katalog får aldrig fälla hela sökningen
                fel = f"{type(e).__name__}: {e}"[:300]
            return Katalogsvar(organisation=k.namn, plattform=k.plattform, katalog=katalog,
                               fel=fel), []

    svar = await asyncio.gather(*(en(k) for k in kataloger))
    return Sokresultat(traffar=[t for _, lista in svar for t in lista],
                       kataloger=[s for s, _ in svar])


# ---------------------------------------------------------------------------
# Hämta datamängd
# ---------------------------------------------------------------------------

def _huwise_datamangd(bas: str, dataset_id: str, meta: dict) -> Datamangd:
    """Huwise beskriver sina datamängder i egen form. Distributionerna är
    datamängdens API och exporterna, som alltid finns."""
    m = (meta.get("metas") or {}).get("default") or {}
    api = f"{bas.rstrip('/')}/explore/dataset/{dataset_id}/"
    export = f"{bas.rstrip('/')}/api/explore/v2.1/catalog/datasets/{dataset_id}/exports"
    geo = any(f.get("type") in ("geo_point_2d", "geo_shape") for f in meta.get("fields", []))
    distributioner = [Distribution(titel="Huwise API", access_url=[api], format=["application/json"])]
    distributioner.append(Distribution(titel="CSV-export", access_url=[f"{export}/csv"],
                                       filer=[Fillank(url=f"{export}/csv?delimiter=%3B")],
                                       format=["text/csv"]))
    if geo:
        distributioner.append(Distribution(titel="GeoJSON-export", access_url=[f"{export}/geojson"],
                                           filer=[Fillank(url=f"{export}/geojson")],
                                           format=["application/geo+json"]))
    return Datamangd(
        uri=api, titel=m.get("title", dataset_id),
        beskrivning=re.sub(r"<[^>]+>", " ", m.get("description") or "").strip(),
        utgivare=Utgivare(namn=m.get("publisher") or ""),
        nyckelord=list(m.get("keyword") or []), tema=list(m.get("theme") or []),
        andrad=m.get("modified") or "", landningssida=[api],
        distributioner=distributioner, kalla=bas)


async def hamta_datamangd(datamangd_id: str) -> Datamangd:
    """Datamängden bakom ett träff-id, med distributioner och datatjänster."""
    plattform, *delar = datamangd_id.split(SEPARATOR)
    if plattform == "entryscape" and len(delar) == 3:
        return await entryscape.hamta_datamangd(*delar)
    if plattform == "ckan" and len(delar) == 2:
        return await ckan.hamta_datamangd(*delar)
    if plattform == "huwise" and len(delar) == 2:
        return _huwise_datamangd(delar[0], delar[1], await huwise.hamta_metadata(*delar))
    if plattform == "arcgis_hub" and len(delar) == 2:
        return await arcgis_hub.hamta_datamangd(*delar)
    if plattform == "dcat_fil" and len(delar) == 2:
        return await dcat_fil.hamta_datamangd(*delar)
    raise ValueError(f"Okänt id: {datamangd_id!r}. Id:t kommer ur oppnadata_sok.")
