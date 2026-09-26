# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för geodata och indelningar.

Tunna proxies över `data_och_analys.geodata.indelningar`-access-lagret.
Returnerar listor av records som Power Query konverterar till tabeller
direkt med `Table.FromRecords`.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query

from data_och_analys.geodata import synkstatus, folkmangd, indelningar

router = APIRouter()


# ============================================================================
# Administrativa indelningar
# ============================================================================


@router.get("/lan")
async def lista_lan() -> list[dict[str, Any]]:
    """Alla län — kod, namn, bokstav."""
    return await indelningar.lista_lan()


@router.get("/regioner")
async def lista_regioner() -> list[dict[str, Any]]:
    """Alla regioner — kod, namn."""
    return await indelningar.lista_regioner()


@router.get("/kommuner")
async def lista_kommuner(
    lan_kod: str | None = Query(None, description="Filtrera på länkod, t.ex. '01' Stockholm"),
    region_kod: str | None = Query(None, description="Filtrera på regionkod"),
) -> list[dict[str, Any]]:
    """Lista kommuner, valfritt filtrerade på län eller region."""
    return await indelningar.lista_kommuner(
        lan_kod=lan_kod, region_kod=region_kod
    )


@router.get("/kommun/{kod}")
async def kommun_info(kod: str) -> dict[str, Any] | None:
    """Detalj för en kommun + alla aktuella klassificeringar."""
    return await indelningar.kommun_info(kod)


# ============================================================================
# Klassificeringar
# ============================================================================


@router.get("/system")
async def lista_system() -> list[dict[str, Any]]:
    """Alla klassificeringssystem (SKR, Tillväxtverket FA/SL3/SL6 osv)."""
    return await indelningar.lista_system()


@router.get("/grupper")
async def lista_grupper(
    system: str = Query(..., description="System-id, t.ex. 'skr_kommungrupp'"),
    ar: int | None = Query(None, description="År; senaste om utelämnat"),
) -> list[dict[str, Any]]:
    """Grupper i ett system för ett år."""
    return await indelningar.lista_grupper(system, ar=ar)


@router.get("/kommuner-i-grupp")
async def lista_kommuner_i_grupp(
    system: str = Query(...),
    grupp_kod: str = Query(...),
    ar: int | None = Query(None),
) -> list[dict[str, Any]]:
    """Kommuner som hör till en specifik grupp i ett system."""
    return await indelningar.lista_kommuner_i_grupp(
        system=system, grupp_kod=grupp_kod, ar=ar
    )


@router.get("/oversatt")
async def oversatt(
    koder: str = Query(..., description="Kommaseparerade kommunkoder, t.ex. '0180,1480,1280'"),
    system: str = Query(..., description="Mål-system att översätta till"),
    ar: int | None = Query(None),
) -> dict[str, dict[str, Any]]:
    """Mappar kommunkoder till deras grupp i ett klassificeringssystem."""
    kommun_koder = [k.strip() for k in koder.split(",") if k.strip()]
    return await indelningar.oversatt(kommun_koder, system, ar=ar)


@router.get("/filtrera")
async def filtrera_kommuner(
    kriterier: str = Query(
        ...,
        description=(
            "JSON-objekt med {system: grupp_kod | [grupp_kod, ...]}, t.ex. "
            "'{\"skr_kommungrupp\": \"C9\", \"tillvaxtverket_sl3\": \"3\"}'"
        ),
    ),
    ar: int | None = Query(None),
) -> list[dict[str, str]]:
    """Returnerar kommunkoder + namn som uppfyller alla kriterier samtidigt.

    Tar `kriterier` som JSON-sträng eftersom Power Query inte hanterar
    nested objects i query strings särskilt elegant. Returnerar lista
    av `{kommun_kod, namn}` så att tabellen blir användbar i Excel
    direkt utan extra lookup.
    """
    krit = json.loads(kriterier)
    koder = await indelningar.filtrera_kommuner(krit, ar=ar)
    # Anrika med kommun-namn för Excel-vänlig output
    if not koder:
        return []
    karta = await indelningar.oversatt(koder, "skr_kommungrupp", ar=None)
    # oversatt ger oss inte namn — gör en separat lookup via lista_kommuner
    alla = await indelningar.lista_kommuner()
    namn_per_kod = {k["kod"]: k["namn"] for k in alla}
    return [{"kommun_kod": k, "namn": namn_per_kod.get(k, "")} for k in koder]


# ============================================================================
# Historiska tidsserier — SCB folkmängd
# ============================================================================


@router.get("/kommun-folkmangd-historik/{kommun_kod}")
async def kommun_folkmangd_historik(
    kommun_kod: str,
    fran: int | None = Query(None, description="Startår, inklusive"),
    till: int | None = Query(None, description="Slutår, inklusive"),
    indelning_ar: int | None = Query(
        None, description="Vilken kommunindelning datat följer; default senaste"
    ),
) -> list[dict[str, Any]]:
    """Folkmängd per år för en kommun (SCB:s harmoniserade serie 1950→).

    En rad per år — direkt tabell i Power Query.
    """
    return await folkmangd.hamta_kommun_folkmangd_historik(
        kommun_kod=kommun_kod, fran=fran, till=till, indelning_ar=indelning_ar,
    )


@router.get("/kommun-folkmangd-aret/{ar}")
async def kommun_folkmangd_aret(
    ar: int,
    indelning_ar: int | None = Query(None),
) -> list[dict[str, Any]]:
    """Folkmängd för alla kommuner ett givet år."""
    return await folkmangd.hamta_aret_per_kommun(ar=ar, indelning_ar=indelning_ar)


@router.get("/kommun-folkmangd-matris")
async def kommun_folkmangd_matris(
    fran: int | None = Query(None),
    till: int | None = Query(None),
    kommun_koder: str | None = Query(
        None, description="Kommaseparerade kommunkoder; alla om utelämnat"
    ),
    indelning_ar: int | None = Query(None),
) -> dict[str, Any]:
    """Hela folkmängdsmatrisen i kompakt form (kommun × år).

    För bredbild-analys (rangordningar, persistens). Kompaktare än
    rad-per-cell — laddas direkt till pandas.
    """
    koder = (
        [k.strip() for k in kommun_koder.split(",") if k.strip()]
        if kommun_koder
        else None
    )
    return await folkmangd.hamta_matris(
        fran=fran, till=till, kommun_koder=koder, indelning_ar=indelning_ar,
    )


@router.get("/kommun-folkmangd-topp/{ar}")
async def kommun_folkmangd_topp(
    ar: int,
    antal: int = Query(50, ge=1, le=290),
    indelning_ar: int | None = Query(None),
) -> list[dict[str, Any]]:
    """Topp N kommuner efter folkmängd ett givet år (server-side sortering)."""
    return await folkmangd.hamta_topp(
        ar=ar, antal=antal, indelning_ar=indelning_ar,
    )


@router.get("/synkstatus")
async def synkstatus_endpoint() -> list[dict[str, Any]]:
    """Ålder och färskhetsbedömning för allt som lagras lokalt."""
    return await synkstatus.las_status()
