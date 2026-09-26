# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Federerat sök-nav — "vad finns och var hämtar jag det?".

Excel-integrationens svaga punkt är att Power Query kräver att användaren
redan vet exakt vilken KPI-kod, tabell-id eller serie som finns. Det här
endpointet vänder på det: en fritextfråga fan-out:as samtidigt mot de
datakällor som har en billig sök, och varje träff bär ett konkret
`hamta_api`-fält — den exakta Excel-endpoint man hämtar datat från.

Det är navet som varje användarvänligt lager (sidopanel, anpassade
funktioner, fristående data-väljare) bygger ovanpå. Resultatet är en platt
array så att Power Query kan visa den som tabell direkt.

Fan-out är samtidig med en per-källa-timeout: en långsam eller nere källa
faller tyst bort i stället för att blockera hela svaret. Vilka källor som
svarade rapporteras inte i radlistan (för Excel-vänlighet) men loggas.

Källor med billig fritextsök: Kolada (KPI:er), dataportal.se
(hela Sveriges öppna data via DCAT), SCB (PxWeb-tabeller), Riksbanken
(serier), Trafikanalys (produkter), Socialstyrelsen (databaser). Fler kan
läggas till genom att registrera en adapter i `_ADAPTRAR`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, BackgroundTasks, Query

from data_och_analys.klienter import (
    kolada,
    pxweb_2,
    riksbanken,
    socialstyrelsen,
    trafa,
)
from data_och_analys.katalog_klienter import dcat
from data_och_analys.sok import index

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-källa-timeout. En källa som inte svarar inom detta faller bort ur
# svaret i stället för att hålla upp hela frågan.
_KALLA_TIMEOUT = 8.0


# ============================================================================
# Käll-adaptrar — var och en returnerar normaliserade träffar
# ============================================================================
#
# Normaliserad träff:
#   {kalla, typ, id, namn, beskrivning, hamta_api, extern_url}
# `hamta_api` är den relativa Excel-API-sökväg som hämtar datat (eller
# None när källan pekar utåt, då används `extern_url` i stället).


def _traff(
    kalla: str,
    typ: str,
    id: Any,
    namn: Any,
    beskrivning: Any = None,
    hamta_api: str | None = None,
    extern_url: str | None = None,
) -> dict[str, Any]:
    return {
        "kalla": kalla,
        "typ": typ,
        "id": id,
        "namn": namn,
        "beskrivning": beskrivning,
        "hamta_api": hamta_api,
        "extern_url": extern_url,
    }


async def _sok_kolada(q: str, limit: int) -> list[dict[str, Any]]:
    res = await kolada.sok_kpier(title=q, per_page=limit)
    return [
        _traff(
            "kolada", "kpi", k.get("id"), k.get("title"),
            k.get("description") or k.get("operating_area"),
            hamta_api=f"/kolada/data?kpi={k.get('id')}",
        )
        for k in res.get("values", [])
    ]


async def _sok_dataportal(q: str, limit: int) -> list[dict[str, Any]]:
    # DCAT ger en rad per distribution — flera per dataset. Hämta extra och
    # deduplicera på dataset-URI så listan inte fylls av samma titel.
    res = await dcat.sok_dataset(fritext=q, limit=limit * 4)
    sedda: set[str] = set()
    traffar: list[dict[str, Any]] = []
    for r in res:
        uri = r.get("dataset")
        if uri in sedda:
            continue
        sedda.add(uri)
        traffar.append(_traff(
            "dataportal.se", "dataset", uri, r.get("titel"),
            r.get("beskrivning"),
            extern_url=uri,
        ))
        if len(traffar) >= limit:
            break
    return traffar


async def _sok_scb(q: str, limit: int) -> list[dict[str, Any]]:
    tabeller = await pxweb_2.lista_tabeller("scb", query=q, sida_storlek=limit)
    return [
        _traff(
            "scb", "tabell", t.id, t.label, t.description,
            hamta_api=f"/pxweb-2/metadata?myndighet=scb&tabell_id={t.id}",
        )
        for t in tabeller
    ]


async def _sok_riksbank(q: str, limit: int) -> list[dict[str, Any]]:
    # SWEA har ingen serversök — hämta serielistan och filtrera lokalt.
    res = await riksbanken.lista_serier(per_sida=1000)
    ql = q.lower()
    traffar: list[dict[str, Any]] = []
    for s in res.get("datapunkter", []):
        text = " ".join(
            str(s.get(k, ""))
            for k in ("seriesId", "shortDescription", "longDescription")
        ).lower()
        if ql in text:
            sid = s.get("seriesId")
            traffar.append(_traff(
                "riksbanken", "serie", sid, s.get("shortDescription"),
                s.get("longDescription"),
                hamta_api=f"/riksbanken/observationer/{sid}",
            ))
            if len(traffar) >= limit:
                break
    return traffar


async def _sok_trafa(q: str, limit: int) -> list[dict[str, Any]]:
    produkter = await trafa.lista_produkter()
    ql = q.lower()
    traffar: list[dict[str, Any]] = []
    for p in produkter:
        if ql in str(p.get("label", "")).lower() or ql in str(p.get("namn", "")).lower():
            traffar.append(_traff(
                "trafa", "produkt", p.get("namn"), p.get("label"),
                p.get("beskrivning"),
                hamta_api=f"/trafa/struktur?query={p.get('namn')}",
            ))
            if len(traffar) >= limit:
                break
    return traffar


async def _sok_socialstyrelsen(q: str, limit: int) -> list[dict[str, Any]]:
    databaser = await socialstyrelsen.lista_databaser()
    ql = q.lower()
    traffar: list[dict[str, Any]] = []
    for d in databaser:
        if ql in str(d.get("text", "")).lower() or ql in str(d.get("namn", "")).lower():
            namn = d.get("namn")
            traffar.append(_traff(
                "socialstyrelsen", "databas", namn, d.get("text"),
                hamta_api=f"/socialstyrelsen/dimensioner/{namn}",
            ))
            if len(traffar) >= limit:
                break
    return traffar


_ADAPTRAR: dict[str, Callable[[str, int], Awaitable[list[dict[str, Any]]]]] = {
    "kolada": _sok_kolada,
    "dataportal": _sok_dataportal,
    "scb": _sok_scb,
    "riksbanken": _sok_riksbank,
    "trafa": _sok_trafa,
    "socialstyrelsen": _sok_socialstyrelsen,
}


# ============================================================================
# Endpoints
# ============================================================================


async def _kor_adapter(
    kalla: str, fn: Callable[[str, int], Awaitable[list[dict[str, Any]]]],
    q: str, limit: int,
) -> list[dict[str, Any]]:
    """Kör en adapter med timeout; fel/timeout ger tom lista (loggas)."""
    try:
        return await asyncio.wait_for(fn(q, limit), _KALLA_TIMEOUT)
    except Exception as fel:  # noqa: BLE001
        logger.warning("Sök-källa %s föll bort: %s", kalla, fel)
        return []


async def _federerad(q: str, per_kalla: int, kallor: str | None) -> list[dict[str, Any]]:
    """Live-federerad fan-out (literal sök) — fallback när indexet är tomt."""
    valda = (
        [k.strip() for k in kallor.split(",") if k.strip() in _ADAPTRAR]
        if kallor
        else list(_ADAPTRAR)
    )
    grupper = await asyncio.gather(
        *(_kor_adapter(k, _ADAPTRAR[k], q, per_kalla) for k in valda)
    )
    return [traff for grupp in grupper for traff in grupp]


@router.get("")
async def sok(
    q: str | None = Query(None, description="Fritextfråga; tom = bläddra på fasetter"),
    n: int = Query(20, ge=1, le=100, description="Max antal träffar"),
    kallor: str | None = Query(
        None, description="Kommaseparerad delmängd av källor; alla om utelämnat"
    ),
    amne: str | None = Query(
        None, description="Ämnesfasett tvärs över källor (t.ex. Befolkning, Miljö)"
    ),
    skolform: str | None = Query(
        None, description="Fasettfilter: skolform (t.ex. Gymnasieskolan)"
    ),
    huvudman: str | None = Query(
        None, description="Fasettfilter: huvudman (Kommunal, Fristående, …)"
    ),
    geografisk: bool = Query(
        False,
        description=(
            "Bara träffar som är geografiskt indelade och går att lägga på "
            "karta"
        ),
    ),
    niva: str | None = Query(
        None,
        description=(
            "Bara träffar som finns på den här indelningsnivån: riket, lan, "
            "kommun, regso, deso, valdistrikt eller punkt"
        ),
    ),
    lage: str = Query(
        "auto",
        description=(
            "auto (semantisk om index finns, annars federerad), "
            "semantisk (hybrid FTS+vektor mot index), "
            "federerad (live literal fan-out)"
        ),
    ),
) -> list[dict[str, Any]]:
    """Sök över datakällorna — semantisk hybrid eller federerad live.

    `lage=semantisk` kör hybrid FTS+vektor mot sök-indexet (synonym-/
    begreppsmatchning, t.ex. "jobb"→sysselsättning, "styrränta"→Policy rate).
    `lage=federerad` fan-out:ar literalt mot källornas egna sök. `lage=auto`
    (default) väljer semantisk om indexet är byggt, annars federerad.

    `skolform`/`huvudman` är hårda fasettfilter (skolenheter) som tillämpas i
    indexet före rankning — t.ex. bara gymnasieskolor med kommunal huvudman.
    Anges en fasett krävs semantiskt läge (federerad kan inte filtrera).

    Returnerar en platt lista `{kalla, typ, id, namn, beskrivning, hamta_api,
    extern_url, fasetter, ...}`. `hamta_api` är Excel-API-sökvägen.
    """
    kallor_lista = (
        [k.strip() for k in kallor.split(",") if k.strip()] if kallor else None
    )
    fasetter = {
        k: v
        for k, v in (
            ("amne", amne), ("skolform", skolform), ("huvudman", huvudman),
            ("geo", True if geografisk or niva else None), ("geo_niva", niva),
        )
        if v
    }

    # Utan sökfråga: bläddra på fasetter (välj ämne/källa, få resultat).
    if not (q and len(q.strip()) >= 2):
        if not (fasetter or kallor_lista):
            return []
        return await index.bladdra(
            amne=amne, kallor=kallor_lista,
            skolform=skolform, huvudman=huvudman, n=n,
        )

    # Fasettfilter kräver indexet — federerad live kan inte filtrera på dem.
    kraver_semantisk = lage == "semantisk" or bool(fasetter)

    if lage in ("auto", "semantisk") or fasetter:
        try:
            traffar = await index.hybrid_sok(
                q, n=n, kallor=kallor_lista, fasetter=fasetter or None
            )
            if traffar or kraver_semantisk:
                return traffar
        except Exception as fel:  # noqa: BLE001
            logger.warning("Semantisk sök föll tillbaka på federerad: %s", fel)
            if kraver_semantisk:
                raise
    return await _federerad(q, per_kalla=5, kallor=kallor)


@router.get("/fasettval")
async def fasettval(
    kallor: str | None = Query(None, description="Redan valda källor, kommaseparerat"),
    amne: str | None = Query(None, description="Redan valt ämne"),
    skolform: str | None = Query(None, description="Redan vald skolform"),
    huvudman: str | None = Query(None, description="Redan vald huvudman"),
    niva: str | None = Query(None, description="Redan vald indelningsnivå"),
    geografisk: bool = Query(False, description="Bara kartbara poster"),
) -> dict[str, Any]:
    """Vad som fortfarande är valbart, givet det som redan valts.

    Driver filterpanelerna i QGIS-pluginet och Excel-tillägget. Varje fasett
    räknas med de övriga tillämpade men inte sin egen, så man kan byta värde
    inom en fasett utan att först nollställa den.

    Alternativ med noll träffar utelämnas. Det är hela poängen: ett filterval
    som inte leder någonstans ska inte gå att göra, så att ingen behöver
    experimentera sig fram till en kombination som finns.
    """
    return await index.fasettval({
        "kallor": [k.strip() for k in kallor.split(",") if k.strip()] if kallor else None,
        "amne": amne,
        "skolform": skolform,
        "huvudman": huvudman,
        "niva": niva,
        "geografisk": geografisk,
    })


@router.get("/geo-nivaer")
async def geo_nivaer(
    kallor: str | None = Query(None, description="Delmängd källor; alla om utelämnat"),
) -> list[dict[str, Any]]:
    """Vilka indelningsnivåer som finns i indexet, med antal poster.

    Driver kartfiltret: i stället för en hårdkodad lista över områdestyper
    visas de nivåer som faktiskt går att rita, med hur mycket data som finns
    på var och en.
    """
    return await index.geo_nivaer(
        kallor=[k.strip() for k in kallor.split(",") if k.strip()] if kallor else None
    )


@router.get("/amnen")
async def amnen() -> list[dict[str, Any]]:
    """Ämnen i indexet med antal (tvärs över källor) — driver ämnesfasetten.

    Returnerar `[{amne, antal}]`. Använd `amne`-värdet som `amne`-parameter
    i `/sok` för att filtrera på kategori i stället för att gissa söktermer.
    """
    return await index.amnen()


@router.get("/fasetter")
async def fasetter(
    kalla: str = Query("skolverket", description="Källa att lista fasettvärden för"),
) -> dict[str, list[str]]:
    """Distinkta fasettvärden för en källa (för UI:ns filterlistor).

    För `skolverket`: `{skolform: [...], huvudman: [...]}` med de värden som
    faktiskt finns i indexet.
    """
    return await index.fasett_varden(kalla)


@router.get("/status")
async def status() -> dict[str, Any]:
    """Index-status: antal indexerade objekt per källa + utvidgning på/av."""
    from data_och_analys.infra import query_expansion

    try:
        antal = await index.antal_indexerat()
    except Exception as fel:  # noqa: BLE001
        antal = {"_fel": str(fel)}
    return {
        "indexerat": antal,
        "totalt": sum(v for v in antal.values() if isinstance(v, int)),
        "frageutvidgning_aktiverad": query_expansion.aktiverad(),
        "federerade_kallor": list(_ADAPTRAR),
    }


@router.post("/bygg-index")
async def bygg_index(
    bakgrund: BackgroundTasks,
    kallor: str | None = Query(None, description="Delmängd källor; alla om utelämnat"),
    max_per_kalla: int | None = Query(
        None, description="Tak per källa (för snabb testkörning)"
    ),
) -> dict[str, str]:
    """Startar ombyggnad av sök-indexet i bakgrunden.

    Ingest av hela katalogen tar några minuter (embedding på CPU), så jobbet
    körs i bakgrunden och endpointet returnerar direkt. Följ utfallet via
    `/sok/status`. Lämpar sig även som mål för en daglig synk.
    """
    kallor_lista = (
        [k.strip() for k in kallor.split(",") if k.strip()] if kallor else None
    )
    bakgrund.add_task(index.bygg_index, kallor=kallor_lista, max_per_kalla=max_per_kalla)
    return {"status": "ombyggnad startad i bakgrunden — följ /sok/status"}


@router.get("/kallor")
async def lista_kallor() -> list[str]:
    """Listar de källnycklar som federerad sök kan använda."""
    return list(_ADAPTRAR)
