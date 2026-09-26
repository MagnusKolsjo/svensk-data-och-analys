# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Synk av lagrade dataset — en väg för MCP-verktygen och den schemalagda synken.

Varje synk hämtar ur en källa, skriver en eller flera tabeller och ska
därefter lämna två spår: en rad i journalen (`synkkorning`), så att takten
går att mäta, och källans signatur (`kallsignatur`), så att bevakningens
larm kvitteras. Larmet får bara kvitteras av en synk — aldrig av att någon
tittat på det — och därför sker kvitteringen här, efter att skrivningen
lyckats.

Källans adress löses i ordningen: angiven adress, `DOA_*_URL` i `.env`,
standardadress i koden. Myndigheter som byter adress vid varje publicering
— SKR, Tillväxtverket, val.se — har ingen standardadress; för dem krävs en
adress i `.env` eller i anropet.

Ingångspunkter:
    SYNKAR
    Synk
    kor(synk_id, url=None, **parametrar) -> dict
    journalfor(synk_id, url, resultat, kvittera=True) -> None
    synk_for_dataset(dataset) -> str | None
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from data_och_analys.geodata import bevakning, synkstatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Synk:
    dataset: tuple[str, ...]          # poster i synkregistret som synken fyller
    funktion: Callable[..., Awaitable[Any]]
    url_env: str | None = None        # DOA_*_URL i .env
    standard_url: str | None = None
    kommentar: str = ""
    tar_url: bool = field(default=False)


def _indelningar():
    from data_och_analys.geodata import indelningar
    return indelningar


def _geometrier():
    from data_och_analys.geodata import geometrier
    return geometrier


def _folkmangd():
    from data_och_analys.geodata import folkmangd
    return folkmangd


async def _kommuntyper_alla_nivaer(url: str | None = None, **p: Any) -> dict[str, int]:
    """Båda nivåerna ur samma fil. Verktyget synkar en nivå per anrop, men
    en ny fil ändrar båda — den schemalagda synken får inte lämna den ena
    på förra årgången."""
    if "niva" in p:
        return {p["niva"].lower(): await _indelningar().synka_tillvaxtverket_kommuntyper(url=url, **p)}
    return {niva.lower(): await _indelningar().synka_tillvaxtverket_kommuntyper(niva=niva, url=url, **p)
            for niva in ("SL3", "SL6")}


# Importerna sker vid anrop: modulerna drar in geopandas och annat tungt, och
# den schemalagda synken ska inte ladda mer än det steg den kör.
SYNKAR: dict[str, Synk] = {
    "tillvaxtverket_kommuntyper": Synk(
        ("kommun", "lan", "tillvaxtverket_kommuntyper"),
        _kommuntyper_alla_nivaer,
        "DOA_TV_KOMMUNTYPER_URL", tar_url=True),
    "skr_kommungrupp": Synk(
        ("skr_kommungrupp",),
        lambda url=None, **p: _indelningar().synka_skr_kommungrupp(url=url, **p),
        "DOA_SKR_KOMMUNGRUPP_URL", tar_url=True),
    "tillvaxtverket_fa": Synk(
        ("tillvaxtverket_fa",),
        lambda url=None, **p: _indelningar().synka_tillvaxtverket_fa_region(url=url, **p),
        "DOA_TV_FA_REGION_URL", tar_url=True),
    "deso_och_regso": Synk(
        ("deso", "regso"),
        lambda url=None, **p: _indelningar().synka_deso_och_regso(url=url, **p),
        "DOA_DESO_REGSO_URL", tar_url=True),
    "valdistrikt": Synk(
        ("valdistrikt",),
        lambda url=None, **p: _indelningar().synka_valdistrikt(url=url, **p),
        "DOA_VALDISTRIKT_URL", tar_url=True),
    "valdistrikt_geom": Synk(
        ("valdistrikt_geom",),
        lambda url=None, **p: _geometrier().synka_valdistrikt_geom(url=url, **p),
        "DOA_VALDISTRIKT_GEOM_URL", tar_url=True),
    "kommun_folkmangd": Synk(
        ("kommun_folkmangd",),
        lambda url=None, **p: _folkmangd().synka_kommun_folkmangd(url=url, **p),
        None, bevakning.KANDA_URLER["kommun_folkmangd"], tar_url=True),
    "postnummer": Synk(
        ("postnummer",),
        lambda url=None, **p: _indelningar().synka_postnummer_via_huwise(**p)),
    "deso_geom": Synk(
        ("deso_geom",),
        lambda url=None, **p: _geometrier().synka_deso_geom_via_wfs(**p)),
    "regso_geom": Synk(
        ("regso_geom",),
        lambda url=None, **p: _geometrier().synka_regso_geom_via_wfs(**p)),
    "regioner": Synk(
        ("region",),
        lambda url=None, **p: _indelningar().synka_regioner(**p),
        kommentar="Hårdkodad katalog — ändras bara med koden."),
    "nuts": Synk(
        ("nuts",),
        lambda url=None, **p: _indelningar().synka_nuts(**p),
        kommentar="Hårdkodad katalog — ändras bara med koden."),
    "valkretsar": Synk(
        ("valkrets",),
        lambda url=None, **p: _indelningar().synka_valkretsar(**p)),
}


def synk_for_dataset(dataset: str) -> str | None:
    """Vilken synk som fyller ett dataset i synkregistret."""
    return next((s for s, d in SYNKAR.items() if dataset in d.dataset), None)


def los_url(synk_id: str, url: str | None = None) -> str | None:
    s = SYNKAR[synk_id]
    if not s.tar_url:
        return None
    return url or (os.getenv(s.url_env, "").strip() if s.url_env else "") or s.standard_url


def _antal(resultat: Any) -> int | None:
    if isinstance(resultat, int):
        return resultat
    if isinstance(resultat, dict):
        tal = [v for v in resultat.values() if isinstance(v, int)]
        return sum(tal) if tal else None
    return None


async def journalfor(synk_id: str, url: str | None, resultat: Any,
                     kvittera: bool = True) -> None:
    """Journal och kvittering efter en lyckad synk.

    `kvittera=False` när datat inte kom från källans fil — ett verktyg som
    fått raderna direkt har inte hämtat in det bevakningen larmar om.
    Varken journalen eller signaturen får fälla en synk som redan skrivit
    sitt data; de är spår av körningen, inte en del av den.
    """
    s = SYNKAR[synk_id]
    anvand = los_url(synk_id, url) if kvittera else None
    for dataset in s.dataset:
        try:
            await synkstatus.notera_synk(dataset, _antal(resultat), s.kommentar or None)
            if anvand:
                await bevakning.notera_kalla(dataset, anvand)
        except Exception as e:  # noqa: BLE001
            logger.warning("Kunde inte journalföra %s efter synk: %s", dataset, e)


async def kor(synk_id: str, url: str | None = None, **parametrar: Any) -> dict[str, Any]:
    """Kör en synk och journalför den. Adressen löses som modulen beskriver."""
    if synk_id not in SYNKAR:
        raise ValueError(f"Okänd synk {synk_id!r}. Kända: {', '.join(SYNKAR)}")
    s = SYNKAR[synk_id]
    anvand = los_url(synk_id, url)
    if s.tar_url and not anvand:
        raise ValueError(f"{synk_id} saknar adress. Ange den i anropet eller sätt {s.url_env} i .env — "
                         "källan byter adress vid varje publicering och har ingen standardadress.")
    resultat = await s.funktion(url=anvand, **parametrar)
    await journalfor(synk_id, anvand, resultat)
    return {"synk": synk_id, "url": anvand, "resultat": resultat}
