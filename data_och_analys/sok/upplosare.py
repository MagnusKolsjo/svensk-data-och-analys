# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Per källa: hur man tar reda på om en post är geografiskt indelad.

Suitens källor delar inget API-mönster, så geografin måste avgöras olika
för var och en. Varje upplösare tar posterna för sin källa och returnerar
`{post_id: geo-block}`; det gemensamma — nivåklassning och skrivning —
ligger i `geo_fasetter`.

En upplösare får rapportera framsteg och ska tåla att avbrytas: passen är
långa och rate-limitade, och det som hunnit skrivas ska stå kvar.

Ingångspunkter:
    UPPLOSARE          källa -> upplösarfunktion
    upplos(kalla, poster, rapportera) -> dict[int, dict | None]
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from data_och_analys.sok.geo_fasetter import ar_ur_period, bygg_geo

# Rate-limiten ligger i klienterna, som alla går via hamta_grind. Ett extra
# lager här hade halverat genomströmningen utan att skydda något.

logger = logging.getLogger(__name__)

Rapportor = Callable[[int, int | None, str], Awaitable[None]]


async def _tyst(gjort: int, av: int | None, meddelande: str) -> None:
    return None


# ---------------------------------------------------------------------------
# SCB — PxWebApi 2
# ---------------------------------------------------------------------------
# Tabellistan bär `variableNames`, så vilka tabeller som ÄR regionala kostar
# fem anrop i stället för 4 228. Bara de regionala behöver sedan metadata
# för att nivåerna ska gå att avgöra.
#
# Exakt matchning, inte delsträng: "födelseregion" och "mammans
# födelseregion" är demografiska attribut, inte geografisk indelning av
# observationen. Delsträngsmatchning tar med ~150 tabeller för mycket.

_SCB_REGIONVARIABLER = {"region", "regioner"}


def _scb_ar_regional(variabelnamn: list[str] | None) -> bool:
    return any(
        (v or "").strip().lower() in _SCB_REGIONVARIABLER
        for v in (variabelnamn or [])
    )


async def _upplos_scb(
    poster: list[dict[str, Any]], rapportera: Rapportor
) -> dict[int, dict[str, Any] | None]:
    from data_och_analys.klienter import pxweb_2

    per_id = {str(p["kalla_id"]): p for p in poster}
    resultat: dict[int, dict[str, Any] | None] = {}

    await rapportera(0, None, "hämtar SCB:s tabellista")
    listan: dict[str, Any] = {}
    sida = 1
    while True:
        tabeller = await pxweb_2.lista_tabeller("scb", sida_storlek=1000, sida=sida)
        if not tabeller:
            break
        for t in tabeller:
            listan[str(t.id)] = t
        if len(tabeller) < 1000:
            break
        sida += 1

    # Tabeller utan regionvariabel avgörs direkt — inget metadataanrop.
    kandidater: list[str] = []
    for tid, post in per_id.items():
        t = listan.get(tid)
        if t is None:
            # Finns i indexet men inte i listan: tabellen är borttagen hos
            # SCB. Lämna posten orörd hellre än att påstå att den saknar
            # geografi — indexbygget städar bort den vid nästa körning.
            continue
        if _scb_ar_regional(getattr(t, "variableNames", None)
                            or getattr(t, "variable_names", None)):
            kandidater.append(tid)
        else:
            resultat[post["id"]] = None

    await rapportera(
        0, len(kandidater),
        f"{len(kandidater)} regionala tabeller av {len(listan)} — hämtar nivåer",
    )

    for n, tid in enumerate(kandidater, start=1):
        post = per_id[tid]
        t = listan[tid]
        try:
            md = await pxweb_2.hamta_metadata("scb", tid, max_varden_per_dim=5000)
        except Exception as fel:  # noqa: BLE001
            logger.warning("SCB %s: metadata misslyckades (%s)", tid, fel)
            continue

        koder, dimnamn = _scb_regionkoder(md)
        resultat[post["id"]] = bygg_geo(
            koder=koder,
            fran_ar=ar_ur_period(getattr(t, "firstPeriod", None)
                                 or getattr(t, "first_period", None)),
            till_ar=ar_ur_period(getattr(t, "lastPeriod", None)
                                 or getattr(t, "last_period", None)),
            dim=dimnamn,
        )
        if n % 25 == 0 or n == len(kandidater):
            await rapportera(n, len(kandidater), f"SCB {n}/{len(kandidater)}")
    return resultat


def _scb_regionkoder(metadata: Any) -> tuple[list[str], str | None]:
    """Plockar regionkoderna ur ett metadatasvar.

    `hamta_metadata` returnerar en Tabellmetadata-modell med `variables`,
    inte en rå dict. Variabelns `values` är koderna och `value_texts`
    etiketterna; en högkardinal dimension kan vara trunkerad, men det gör
    inget här — nivåklassningen behöver bara se kodformen, inte varje kod.
    """
    variabler = (
        getattr(metadata, "variables", None)
        or (metadata.get("variables") if isinstance(metadata, dict) else None)
        or []
    )
    for v in variabler:
        namn = str(
            getattr(v, "id", None)
            or (v.get("id") if isinstance(v, dict) else "")
            or ""
        ).strip().lower()
        etikett = str(
            getattr(v, "label", None)
            or (v.get("label") if isinstance(v, dict) else "")
            or ""
        ).strip().lower()
        if namn not in _SCB_REGIONVARIABLER and etikett not in _SCB_REGIONVARIABLER:
            continue
        koder = (
            getattr(v, "values", None)
            or (v.get("values") if isinstance(v, dict) else None)
            or []
        )
        return [str(k) for k in koder], (namn or etikett)
    return [], None


# ---------------------------------------------------------------------------
# Kolada
# ---------------------------------------------------------------------------
# Antagandet att alla KPI:er är kommunindelade håller inte. `municipality_type`
# i KPI-listningen delar de 6 138 i tre: K bara kommun (3 675), L bara region
# (1 322), A båda (1 141). Fältet kommer med i listningen, så hela källan
# avgörs på ett fåtal anrop utan metadatapass per KPI.
#
# Koladas regionkoder är fyrsiffriga (0001 Region Stockholm), alltså samma
# form som SCB:s kommunkoder. Nivåerna sätts därför ur municipality_type och
# aldrig genom att klassa koderna — den vägen hade gjort varje region till
# en kommun.
#
# Åren står inte i KPI-metadatan. De skulle kräva ett dataanrop per KPI, och
# 6 138 anrop för att fylla i två årtal är inte värt det. Utan fran_ar/till_ar
# låter kartpanelen året stå fritt för Kolada.

_KOLADA_NIVAER = {
    "K": ["riket", "kommun"],
    "L": ["riket", "lan"],
    "A": ["riket", "lan", "kommun"],
}


async def _upplos_kolada(
    poster: list[dict[str, Any]], rapportera: Rapportor
) -> dict[int, dict[str, Any] | None]:
    from data_och_analys.klienter import kolada

    await rapportera(0, None, "hämtar Koladas KPI-lista")
    typ_per_kpi: dict[str, str] = {}
    sida = 1
    while True:
        svar = await kolada.sok_kpier(per_page=5000, page=sida)
        varden = (svar or {}).get("values") or []
        if not varden:
            break
        for k in varden:
            if k.get("id"):
                typ_per_kpi[str(k["id"])] = (k.get("municipality_type") or "").strip()
        if len(varden) < 5000 and not (svar or {}).get("next_url"):
            break
        sida += 1

    await rapportera(0, len(poster), f"{len(typ_per_kpi)} KPI:er i listan")
    resultat: dict[int, dict[str, Any] | None] = {}
    for post in poster:
        typ = typ_per_kpi.get(str(post["kalla_id"]))
        if typ is None:
            continue          # finns i indexet men inte hos Kolada — lämna orörd
        resultat[post["id"]] = bygg_geo(
            nivaer=_KOLADA_NIVAER.get(typ), dim="municipality"
        )
    await rapportera(len(resultat), len(poster), "Kolada klar")
    return resultat


# ---------------------------------------------------------------------------
# Socialstyrelsen
# ---------------------------------------------------------------------------
# Femton databaser, så hela källan avgörs på femton anrop mot
# dimensions-endpointen. Fjorton har en `region`-dimension med SCB:s
# standardkoder — de flesta på länsnivå, ett par på kommunnivå.
#
# Två faller igenom av sig själva och ska göra det: dodsorsaker_manad saknar
# region helt, och drgstatistikislutenvard har 306 sjukhus utan kodstruktur.
# Ett sjukhus är inte en yta i någon av suitens indelningar, så klassningen
# ger tom lista och posten markeras som icke-geografisk.

_SOS_REGIONDIM = {"region"}
_SOS_ARDIM = {"ar", "år", "period"}


async def _upplos_socialstyrelsen(
    poster: list[dict[str, Any]], rapportera: Rapportor
) -> dict[int, dict[str, Any] | None]:
    from data_och_analys.klienter import socialstyrelsen

    resultat: dict[int, dict[str, Any] | None] = {}
    for n, post in enumerate(poster, start=1):
        databas = str(post["kalla_id"])
        try:
            dimensioner = await socialstyrelsen.hamta_dimensioner(databas)
        except Exception as fel:  # noqa: BLE001
            logger.warning("Socialstyrelsen %s: %s", databas, fel)
            continue

        # Dimensionslistan bär bara namn och beskrivning; värdena kräver ett
        # eget anrop per dimension. Femton databaser gör det billigt ändå.
        namn = {str(d.get("namn") or "").lower() for d in dimensioner or []}
        koder: list[str] = []
        dimnamn: str | None = None
        ar: list[int] = []

        if _SOS_REGIONDIM & namn:
            dimnamn = "region"
            try:
                varden = await socialstyrelsen.hamta_dimensionsvarden(databas, "region")
                # Bara `kod` duger. drgstatistikislutenvard har 306 sjukhus
                # utan kodfält alls, och faller man tillbaka på `id` blir
                # sjukhus nr 1 ett län och nr 1565 en kommun.
                koder = [
                    str(v["kod"]) for v in varden or []
                    if isinstance(v, dict) and v.get("kod")
                ]
            except Exception as fel:  # noqa: BLE001
                logger.warning("Socialstyrelsen %s/region: %s", databas, fel)

        for ardim in _SOS_ARDIM & namn:
            try:
                varden = await socialstyrelsen.hamta_dimensionsvarden(databas, ardim)
            except Exception:  # noqa: BLE001
                continue
            ar = [
                a for a in (ar_ur_period(str(v.get("text") or v.get("id") or ""))
                            for v in varden or []) if a
            ]
            break

        resultat[post["id"]] = bygg_geo(
            koder=koder,
            fran_ar=min(ar) if ar else None,
            till_ar=max(ar) if ar else None,
            dim=dimnamn,
        )
        await rapportera(n, len(poster), f"Socialstyrelsen {databas}")
    return resultat


# ---------------------------------------------------------------------------
# PxWeb v1 — Folkhälsomyndigheten, KI, KI prognos, Jordbruksverket, Tillväxtanalys
# ---------------------------------------------------------------------------
# v1:s nodträd bär inga variabelnamn, till skillnad från v2:s tabellista. Varje
# tabell kräver därför ett metadataanrop, och rate-limiten sätter takten:
# Jordbruksverket 1000/10s går på minuter, Konjunkturinstitutet 10/10s inte.
#
# Regionvariabeln heter olika: "Region" hos Folkhälsomyndigheten och KI, "Län"
# hos Jordbruksverket och Tillväxtanalys. Tre saker skiljer dem åt utöver namnet:
#
#   Jordbruksverket skriver koden onollad ("1" för Stockholm) och blandar in
#   f.d. län från före länsreformerna för att bära serien tillbaka till 1981.
#   Klassningen hanterar båda — de utgångna länen faller bort av sig själva
#   eftersom suiten saknar geometri för dem.
#
#   Konjunkturinstitutets regionala tabeller är NUTS2, EU:s åtta riksområden.
#   De är en riktig geografisk indelning men suiten har inget NUTS-kartlager,
#   så klassningen ger tom lista och tabellerna markeras som icke-kartbara.
#   Det är avsiktligt: hellre utelämnad än utlovad och sedan omöjlig att rita.
#
#   KI:s prognosdatabaser är makroprognoser på riksnivå utan regiondimension
#   alls. De 800 tabellerna kostar ändå ett anrop var för att bekräftas.

_V1_REGIONVARIABLER = {"region", "regioner", "län", "lan"}
_V1_ARVARIABLER = {"år", "ar", "tid", "period"}


def _v1_varden(variabel: Any) -> tuple[list[str], list[str]]:
    """Plockar (koder, texter) ur en v1-variabel oavsett attributform."""
    koder = (
        getattr(variabel, "values", None)
        or (variabel.get("values") if isinstance(variabel, dict) else None)
        or []
    )
    texter = (
        getattr(variabel, "valueTexts", None)
        or (variabel.get("valueTexts") if isinstance(variabel, dict) else None)
        or []
    )
    return [str(k) for k in koder], [str(x) for x in texter]


def _v1_namn(variabel: Any) -> tuple[str, str]:
    kod = getattr(variabel, "code", None) or (
        variabel.get("code") if isinstance(variabel, dict) else "")
    text = getattr(variabel, "text", None) or (
        variabel.get("text") if isinstance(variabel, dict) else "")
    return str(kod or ""), str(text or "")


async def _upplos_pxweb_1(
    myndighet: str, poster: list[dict[str, Any]], rapportera: Rapportor
) -> dict[int, dict[str, Any] | None]:
    from data_och_analys.klienter import pxweb_1

    resultat: dict[int, dict[str, Any] | None] = {}
    for n, post in enumerate(poster, start=1):
        # kalla_id bär tabellens stig i nodträdet.
        stig = pxweb_1.dela_stig(str(post["kalla_id"]))
        try:
            md = await pxweb_1.hamta_metadata(myndighet, stig)
        except Exception as fel:  # noqa: BLE001
            logger.warning("%s %s: %s", myndighet, post["kalla_id"], fel)
            continue

        variabler = (
            getattr(md, "variables", None)
            or (md.get("variables") if isinstance(md, dict) else None)
            or []
        )
        koder: list[str] = []
        dimnamn: str | None = None
        ar: list[int] = []
        for v in variabler:
            kod, text = _v1_namn(v)
            etikett = (kod or text).strip().lower()
            varden, texter = _v1_varden(v)
            if etikett in _V1_REGIONVARIABLER:
                koder, dimnamn = varden, (kod or text)
            elif etikett in _V1_ARVARIABLER:
                # Perioder kan vara "2004-2007" eller "2011Q01"; fyra siffror
                # i början räcker för att få ut årtalet.
                for x in (varden + texter):
                    a = ar_ur_period(x)
                    if a and 1900 <= a <= 2100:
                        ar.append(a)

        resultat[post["id"]] = bygg_geo(
            koder=koder,
            fran_ar=min(ar) if ar else None,
            till_ar=max(ar) if ar else None,
            dim=dimnamn,
        )
        if n % 50 == 0 or n == len(poster):
            await rapportera(n, len(poster), f"{myndighet} {n}/{len(poster)}")
    return resultat


def _v1_upplosare(myndighet: str):
    async def kor(poster, rapportera):
        return await _upplos_pxweb_1(myndighet, poster, rapportera)
    return kor


# ---------------------------------------------------------------------------
# Försäkringskassan
# ---------------------------------------------------------------------------
# Geografin syns i datats egna `dimensions`-nycklar: `lan_kod` när datasetet är
# länsnedbrutet. Värdemängden är SCB:s 21 länskoder plus FK:s egen "ALL" för
# riket, som klassningen ignorerar — riksnivån kommer i stället från att
# datasetet över huvud taget har en länsdimension.

_FK_GEOFALT = {"lan_kod": "lan", "kommun_kod": "kommun", "region_kod": "lan"}


async def _upplos_forsakringskassan(
    poster: list[dict[str, Any]], rapportera: Rapportor
) -> dict[int, dict[str, Any] | None]:
    from data_och_analys.klienter import forsakringskassan

    resultat: dict[int, dict[str, Any] | None] = {}
    for n, post in enumerate(poster, start=1):
        try:
            data = await forsakringskassan.hamta_dataset(str(post["kalla_id"]))
        except Exception as fel:  # noqa: BLE001
            logger.warning("FK %s: %s", post["kalla_id"], fel)
            continue

        # Klienten plattar svaret till ett pagineringspaket där dimensions-
        # och måttfält ligger sida vid sida på varje rad. Äldre svar har dem
        # nästlade under `dimensions`; båda formerna läses.
        rader = (
            data if isinstance(data, list)
            else (data or {}).get("datapunkter") or (data or {}).get("data") or []
        )
        koder: list[str] = []
        dimnamn: str | None = None
        ar: list[int] = []
        for rad in rader[:2000]:
            falt = {**(rad or {}), **((rad or {}).get("dimensions") or {})}
            for namn in _FK_GEOFALT:
                if falt.get(namn) not in (None, ""):
                    dimnamn = dimnamn or namn
                    koder.append(str(falt[namn]))
            a = ar_ur_period(str(falt.get("ar") or ""))
            if a:
                ar.append(a)

        resultat[post["id"]] = bygg_geo(
            koder=koder,
            fran_ar=min(ar) if ar else None,
            till_ar=max(ar) if ar else None,
            dim=dimnamn,
        )
        await rapportera(n, len(poster), f"Försäkringskassan {n}/{len(poster)}")
    return resultat


# ---------------------------------------------------------------------------
# Skolverket — punktlager
# ---------------------------------------------------------------------------
# En skolenhet är en punkt, inte en yta. Den hör hemma i ett punktlager och
# aldrig i en choropleth, så den får nivån `punkt` plus `kommun` — den senare
# för att enheterna går att aggregera per kommun.
#
# Kommunkoden ligger redan i sökindexets fasetter från indexbygget, så
# flaggningen kostar inga anrop alls. Koordinaterna gör det däremot: de finns
# bara i detalj-API:et, ett anrop per enhet, 6 665 stycken mot 30/10s. Den
# hämtningen hör till bygget av själva punktlagret och inte hit — det här
# passet svarar bara på frågan om posten är geografisk.

async def _upplos_skolverket(
    poster: list[dict[str, Any]], rapportera: Rapportor
) -> dict[int, dict[str, Any] | None]:
    geo = bygg_geo(nivaer=["kommun", "punkt"], dim="Kommunkod")
    await rapportera(len(poster), len(poster),
                     f"Skolverket: {len(poster)} enheter som punktlager")
    return {p["id"]: geo for p in poster}


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
# Källor utan geografisk indelning står med som uttryckligt None. Det skiljer
# "undersökt, saknar geografi" från "ingen upplösare skriven än", vilket
# annars är omöjligt att se i efterhand.

# KB Bibstat: observationer på biblioteksnivå, utan kommun- eller
# länstillhörighet i datat. Riksbanken: räntor och valutakurser.
# Trafikanalys: strukturen har fält som heter "region", men de är
# hierarkinoder för UI-gruppering (Type "H"), inte frågebara dimensioner
# (Type "D"). API:et svarar med fel när de används som nedbrytning. Övriga
# geo-klingande fält är falska vänner — `vagomr` är vägtyp, `land` är
# Sverige/utland.
# KB Bibstat: observationer på biblioteksnivå utan kommun- eller
# länstillhörighet. Riksbanken: räntor och valutakurser.
INGEN_GEOGRAFI = {"riksbanken", "kb_bibstat", "trafa"}

UPPLOSARE: dict[str, Callable[..., Awaitable[dict[int, dict | None]]]] = {
    "scb": _upplos_scb,
    "kolada": _upplos_kolada,
    "socialstyrelsen": _upplos_socialstyrelsen,
    "forsakringskassan": _upplos_forsakringskassan,
    "skolverket": _upplos_skolverket,
    "folkhalsomyndigheten": _v1_upplosare("folkhalsomyndigheten"),
    "ki_prognos": _v1_upplosare("ki_prognos"),
    "konjunkturinstitutet": _v1_upplosare("konjunkturinstitutet"),
    "jordbruksverket": _v1_upplosare("jordbruksverket"),
    "tillvaxtanalys": _v1_upplosare("tillvaxtanalys"),
}


async def upplos(
    kalla: str,
    poster: list[dict[str, Any]],
    rapportera: Rapportor | None = None,
) -> dict[int, dict[str, Any] | None]:
    """Kör källans upplösare. Källor utan geografi ger None rakt av."""
    rap = rapportera or _tyst
    if kalla in INGEN_GEOGRAFI:
        await rap(len(poster), len(poster), f"{kalla}: aldrig geografisk")
        return {p["id"]: None for p in poster}
    funktion = UPPLOSARE.get(kalla)
    if funktion is None:
        raise ValueError(
            f"ingen upplösare för {kalla!r} — kända: "
            + ", ".join(sorted(set(UPPLOSARE) | INGEN_GEOGRAFI))
        )
    return await funktion(poster, rap)
