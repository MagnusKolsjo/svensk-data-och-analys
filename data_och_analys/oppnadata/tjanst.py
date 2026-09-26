# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Tjänstelagret under MCP-verktygen för öppna data.

Verktygen arbetar i steg — sök, visa datamängd, beskriv data, aggregera —
och varje steg utgår från ett träff-id och ett distributionsnummer. Här
hålls de senast använda datamängderna och tabellerna i minnet, så att en
beskrivning följd av tre aggregeringar inte hämtar och tolkar samma filer
fyra gånger.

Ingångspunkter:
    DistributionSvar, DatamangdSvar, Tabellbeskrivning, Radsvar, Aggregering
    hitta_kataloger(namn, kategori, lan, plattform) -> list[Katalog]
    sok(fragor, kataloger, per_katalog) -> Sokresultat
    datamangd(datamangd_id) -> DatamangdSvar
    tabell(datamangd_id, distribution, filurval, blad) -> Tabell
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, Field

from data_och_analys.infra import db
from data_och_analys.oppnadata import plattformar, register
from data_och_analys.oppnadata import tabell as tabellager
from data_och_analys.oppnadata.dcat_modell import Datamangd
from data_och_analys.oppnadata.hamtningsvag import Hamtningsvag, klassa

_DATAMANGD_SEKUNDER = 3600
_TABELLER_I_MINNET = 4


# ---------------------------------------------------------------------------
# Svarsmodeller
# ---------------------------------------------------------------------------

class DistributionSvar(BaseModel):
    nummer: int = Field(description="Används som `distribution` i datverktygen")
    titel: str
    format: list[str]
    hamtningsvag: Hamtningsvag
    filtitlar: list[str] = Field(default_factory=list,
                                 description="Filernas titlar när de är flera — välj med `filurval`")


class DatamangdSvar(BaseModel):
    id: str
    titel: str
    beskrivning: str
    utgivare: str
    nyckelord: list[str]
    tidsperiod: tuple[str, str] | None
    andrad: str
    uppdateringsfrekvens: str
    landningssida: list[str]
    ar_serie: bool
    i_serie: list[str]
    distributioner: list[DistributionSvar]


class Kolumn(BaseModel):
    namn: str
    typ: str = Field(description="tal, datum, text eller tom")
    tomma: int
    olika_varden: int
    exempel: list[str]
    varden: dict[str, int] | None = Field(default=None, description="Fördelning när värdena är få")
    min: float | None = None
    max: float | None = None
    summa: float | None = None
    fran: str | None = None
    till: str | None = None


class Tabellbeskrivning(BaseModel):
    antal_rader: int
    kolumner: list[Kolumn]
    kallor: list[str]
    varningar: list[str]
    blad: list[str] = Field(description="Blad eller filer i ett arkiv att välja mellan med `blad`")


class Radsvar(BaseModel):
    antal_i_urvalet: int
    fran_rad: int
    rader: list[dict[str, Any]]
    kallor: list[str]
    varningar: list[str]


class Aggregering(BaseModel):
    rader_i_urvalet: int
    grupper: int
    utelamnade_grupper: int
    resultat: list[dict[str, Any]]
    totalt: float = Field(description="Över hela urvalet, inte bara de visade grupperna")
    kallor: list[str]
    varningar: list[str]


# ---------------------------------------------------------------------------
# Kataloger och sök
# ---------------------------------------------------------------------------

async def _lan_kod(lan: str | None) -> str | None:
    """Länskod ur kod ("14"), länsbokstav ("O") eller namn ("Västra Götaland")."""
    if not lan:
        return None
    lan = lan.strip()
    if lan.isdigit():
        return lan.zfill(2)
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(f"SELECT kod, namn, bokstav FROM {db.prefix()}lan")
    s = lan.lower().removesuffix(" län").removesuffix("s").strip()
    for r in rader:
        if lan.upper() == (r["bokstav"] or "") or r["namn"].lower().startswith(s):
            return r["kod"]
    raise ValueError(f"Känner inte igen länet {lan!r}. Ange länskod, länsbokstav eller namn.")


async def hitta_kataloger(namn: str | None = None, kategori: str | None = None,
                          lan: str | None = None, plattform: str | None = None) -> list[register.Katalog]:
    return await register.las(kategori=kategori, lan_kod=await _lan_kod(lan),
                              namn=namn, plattform=plattform)


async def sok(fragor: list[str], kataloger: list[register.Katalog],
              per_katalog: int) -> plattformar.Sokresultat:
    """Flera formuleringar slås ihop: kommuner kallar samma sak olika saker."""
    traffar: dict[str, plattformar.Traff] = {}
    per_katalog_svar: dict[str, plattformar.Katalogsvar] = {}
    for fraga in fragor or [""]:
        r = await plattformar.sok(kataloger, fraga, per_katalog)
        for t in r.traffar:
            traffar.setdefault(t.id, t)
        for k in r.kataloger:
            tidigare = per_katalog_svar.get(k.katalog + k.organisation)
            if tidigare is None or (k.antal_traffar or 0) > (tidigare.antal_traffar or 0):
                per_katalog_svar[k.katalog + k.organisation] = k
    return plattformar.Sokresultat(traffar=list(traffar.values()),
                                   kataloger=list(per_katalog_svar.values()))


# ---------------------------------------------------------------------------
# Datamängd och tabell
# ---------------------------------------------------------------------------

_datamangder: dict[str, tuple[float, Datamangd]] = {}
_tabeller: OrderedDict[tuple, tabellager.Tabell] = OrderedDict()


async def _hamta(datamangd_id: str) -> Datamangd:
    lagrad = _datamangder.get(datamangd_id)
    if lagrad and time.monotonic() - lagrad[0] < _DATAMANGD_SEKUNDER:
        return lagrad[1]
    d = await plattformar.hamta_datamangd(datamangd_id)
    _datamangder[datamangd_id] = (time.monotonic(), d)
    return d


async def datamangd(datamangd_id: str) -> DatamangdSvar:
    d = await _hamta(datamangd_id)
    return DatamangdSvar(
        id=datamangd_id, titel=d.titel, beskrivning=d.beskrivning[:3000],
        utgivare=d.utgivare.namn or d.utgivare.uri, nyckelord=d.nyckelord[:30],
        tidsperiod=d.tidsperiod, andrad=d.andrad, uppdateringsfrekvens=d.uppdateringsfrekvens,
        landningssida=d.landningssida, ar_serie=d.ar_serie, i_serie=d.i_serie,
        distributioner=[
            DistributionSvar(nummer=i, titel=x.titel, format=x.format, hamtningsvag=klassa(x),
                             filtitlar=[f.titel or f.url.rsplit("/", 1)[-1] for f in x.filer]
                             if len(x.filer) > 1 else [])
            for i, x in enumerate(d.distributioner)])


async def vag(datamangd_id: str, distribution: int) -> Hamtningsvag:
    d = await _hamta(datamangd_id)
    if not 0 <= distribution < len(d.distributioner):
        raise ValueError(f"Datamängden har {len(d.distributioner)} distributioner, "
                         f"numrerade 0–{len(d.distributioner) - 1}.")
    return klassa(d.distributioner[distribution])


async def tabell(datamangd_id: str, distribution: int | list[int], filurval: str | None = None,
                 blad: str | None = None) -> tabellager.Tabell:
    """Tabellen bakom en eller flera distributioner.

    Flera distributioner slås ihop. Så publicerar vissa kommuner perioder —
    en distribution per månad i stället för flera filer i en — och ett
    kvartal blir då tre distributioner. Kolumnen `_distribution` bär
    distributionens titel.
    """
    if isinstance(distribution, list):
        if len(distribution) == 1:
            distribution = distribution[0]
        else:
            d = await _hamta(datamangd_id)
            delar = []
            for n in distribution:
                t = await tabell(datamangd_id, n, filurval, blad)
                df = t.df.copy()
                df["_distribution"] = d.distributioner[n].titel or str(n)
                delar.append(tabellager.Tabell(df, t.kallor, t.varningar, t.blad))
            varningar = [v for t in delar for v in t.varningar]
            if len({tuple(c for c in t.df.columns if c != "_distribution") for t in delar}) > 1:
                varningar.append("Distributionerna har olika kolumner; saknade värden är tomma.")
            return tabellager.Tabell(
                tabellager.pd.concat([t.df for t in delar], ignore_index=True, sort=False).fillna(""),
                [k for t in delar for k in t.kallor], varningar)
    nyckel = (datamangd_id, distribution, filurval or "", blad or "")
    if nyckel in _tabeller:
        _tabeller.move_to_end(nyckel)
        return _tabeller[nyckel]
    v = await vag(datamangd_id, distribution)
    if not v.hamtbar and v.typ != "okand":
        raise ValueError(f"Distributionen går inte att läsa som data: {v.forklaring} ({v.url})")
    t = await tabellager.hamta_tabell(v, filurval=filurval, blad=blad)
    _tabeller[nyckel] = t
    while len(_tabeller) > _TABELLER_I_MINNET:
        _tabeller.popitem(last=False)
    return t
