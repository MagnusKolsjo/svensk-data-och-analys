# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Sök-index: ingest av datakällornas kataloger + hybrid FTS/vektor-sökning.

Det här är den semantiska back-enden bakom `/sok`. I stället för att fan-out:a
mot källornas API:er vid varje fråga (literal textmatchning) bygger vi ett
lokalt index `sok_index` med en rad per sökbart objekt — KPI, SCB-tabell,
Riksbanksserie, Trafa-produkt, Socialstyrelse-databas — och både en FTS-
vektor och en embedding per rad.

Sökningen är hybrid enligt projektets standardvikter (35 % FTS, 65 % vektor):
två kandidatmängder (fulltextträffar respektive närmaste grannar i vektorrum)
slås ihop och rerankas med viktad poäng. FTS-frågan utvidgas via
`query_expansion` (synonymer/terminologi); vektor-sökningen använder alltid
originalfrågans embedding.

Embeddings hanteras av `sok.embeddings` (KBLab svensk modell). Vektorerna
skickas till pgvector som textliteral `[...]::vector` så att den generiska
db-adaptern räcker — ingen typregistrering behövs.

Ingången till indexet byggs om av `bygg_index` (lämpar sig för daglig synk).
Katalogerna ändras långsamt, så en daglig ombyggnad räcker.

Ingångspunkter:
    bygg_index(kallor=None, max_per_kalla=None) -> dict   (antal per källa)
    hybrid_sok(fraga, n=20, kallor=None) -> list[dict]
    antal_indexerat() -> dict
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable

from data_och_analys.infra import db, query_expansion
from data_och_analys.sok.kallnamn import visningsnamn
from data_och_analys.katalog_klienter import dcat
from data_och_analys.klienter import (
    forsakringskassan,
    kolada,
    pxweb_2,
    riksbanken,
    skolverket,
    socialstyrelsen,
    trafa,
)
from data_och_analys.sok import embeddings

logger = logging.getLogger(__name__)

# Standardvikter — exponeras via .env för finjustering utan kodändring.
FTS_VIKT = float(os.getenv("DOA_FTS_WEIGHT", "0.35"))
VEC_VIKT = float(os.getenv("DOA_VEC_WEIGHT", "0.65"))

# Embedding-batchstorlek vid ingest.
_BATCH = 128


# ============================================================================
# Katalog-hämtare — en per källa, returnerar normaliserade objekt
# ============================================================================
#
# Varje objekt: {typ, kalla_id, namn, beskrivning, hamta_api, extern_url,
#                sok_text}. `sok_text` är det som FTS och embedding byggs på.


def _objekt(
    typ: str, kalla_id: Any, namn: Any, beskrivning: Any,
    hamta_api: str | None = None, extern_url: str | None = None,
    fasetter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delar = [str(namn or ""), str(beskrivning or "")]
    return {
        "typ": typ,
        "kalla_id": str(kalla_id),
        "namn": namn,
        "beskrivning": beskrivning,
        "hamta_api": hamta_api,
        "extern_url": extern_url,
        "sok_text": " ".join(d for d in delar if d).strip(),
        "fasetter": fasetter,
    }


# ============================================================================
# Ämnesfasett — tvärgående kategori över källor
# ============================================================================
#
# Kanonisk bas är SCB:s officiella ämnesområden (tabellernas `paths`-toppnivå).
# Övriga källor mappas in i samma vokabulär så att ett ämnesfilter fungerar
# tvärs över källorna — det gör att man kan bläddra på kategori i stället för
# att gissa söktermer.


# SCB använder både äldre och nyare namn på vissa ämnesområden. Normalisera
# till ett kanoniskt namn så att fasetten inte får dubbletter.
_AMNE_NORMALISERING: dict[str, str] = {
    "Utbildning och forskning": "Utbildning",
    "Transporter och kommunikationer": "Transporter",
    "Boende och byggande": "Boende, byggande och bebyggelse",
}


def _normalisera_amne(amne: str | None) -> str | None:
    if amne is None:
        return None
    return _AMNE_NORMALISERING.get(amne, amne)


def _scb_amne(paths: Any) -> str | None:
    """Toppnivåns ämnesområde ur en SCB-tabells `paths` (t.ex. 'Miljö')."""
    try:
        return _normalisera_amne(paths[0][0]["label"])
    except (TypeError, KeyError, IndexError):
        return None


# Kolada spänner över många ämnen — mappa verksamhetsområdet på nyckelord.
_KOLADA_AMNE: dict[str, str] = {
    "befolkning": "Befolkning",
    "arbetsmarknad": "Arbetsmarknad",
    "näringsliv": "Näringsverksamhet",
    "utbildning": "Utbildning",
    "skola": "Utbildning",
    "förskola": "Utbildning",
    "barn": "Utbildning",
    "vård": "Hälso- och sjukvård",
    "hälsa": "Hälso- och sjukvård",
    "omsorg": "Socialtjänst",
    "social": "Socialtjänst",
    "miljö": "Miljö",
    "energi": "Energi",
    "demokrati": "Demokrati",
    "infrastruktur": "Transporter",
    "trafik": "Transporter",
    "samhällsbyggnad": "Boende, byggande och bebyggelse",
    "bostad": "Boende, byggande och bebyggelse",
    "ekonomi": "Offentlig ekonomi",
    "kultur": "Kultur och fritid",
}


def _kolada_amne(operating_area: str | None) -> str:
    s = (operating_area or "").lower()
    for nyckel, amne in _KOLADA_AMNE.items():
        if nyckel in s:
            return amne
    return "Kommunal verksamhet"


# Källor med ett enda naturligt ämne.
_FAST_AMNE: dict[str, str] = {
    "riksbanken": "Finansmarknad",
    "socialstyrelsen": "Hälso- och sjukvård",
    "trafa": "Transporter",
    "forsakringskassan": "Socialförsäkring",
    "skolverket": "Utbildning",
}


async def _kat_kolada(max_antal: int | None) -> list[dict[str, Any]]:
    objekt: list[dict[str, Any]] = []
    sida = 1
    while True:
        res = await kolada.sok_kpier(title=None, per_page=200, page=sida)
        varden = res.get("values", [])
        if not varden:
            break
        for k in varden:
            besk = " ".join(
                str(k.get(f, "")) for f in ("description", "operating_area", "perspective")
            ).strip()
            objekt.append(_objekt(
                "kpi", k.get("id"), k.get("title"), besk,
                hamta_api=f"/kolada/data?kpi={k.get('id')}",
                fasetter={"amne": _kolada_amne(k.get("operating_area"))},
            ))
        if max_antal and len(objekt) >= max_antal:
            break
        if not res.get("next_url") and len(varden) < 200:
            break
        sida += 1
    return objekt[:max_antal] if max_antal else objekt


async def _kat_scb(max_antal: int | None) -> list[dict[str, Any]]:
    objekt: list[dict[str, Any]] = []
    sida = 1
    while True:
        tabeller = await pxweb_2.lista_tabeller("scb", sida_storlek=1000, sida=sida)
        if not tabeller:
            break
        for t in tabeller:
            paths = getattr(t, "paths", None)
            objekt.append(_objekt(
                "tabell", t.id, t.label, t.description,
                hamta_api=f"/pxweb-2/metadata?myndighet=scb&tabell_id={t.id}",
                fasetter={"amne": _scb_amne(paths)} if _scb_amne(paths) else None,
            ))
        if max_antal and len(objekt) >= max_antal:
            break
        if len(tabeller) < 1000:
            break
        sida += 1
    return objekt[:max_antal] if max_antal else objekt


async def _kat_riksbank(max_antal: int | None) -> list[dict[str, Any]]:
    res = await riksbanken.lista_serier(per_sida=2000)
    objekt = []
    for s in res.get("datapunkter", []):
        sid = s.get("seriesId")
        besk = " ".join(
            str(s.get(f, "")) for f in ("shortDescription", "longDescription")
        ).strip()
        objekt.append(_objekt(
            "serie", sid, s.get("shortDescription"), besk,
            hamta_api=f"/riksbanken/observationer/{sid}",
            fasetter={"amne": _FAST_AMNE["riksbanken"]},
        ))
    return objekt[:max_antal] if max_antal else objekt


async def _kat_trafa(max_antal: int | None) -> list[dict[str, Any]]:
    produkter = await trafa.lista_produkter()
    objekt = [
        _objekt(
            "produkt", p.get("namn"), p.get("label"), p.get("beskrivning"),
            hamta_api=f"/trafa/struktur?query={p.get('namn')}",
            fasetter={"amne": _FAST_AMNE["trafa"]},
        )
        for p in produkter
    ]
    return objekt[:max_antal] if max_antal else objekt


async def _kat_socialstyrelsen(max_antal: int | None) -> list[dict[str, Any]]:
    databaser = await socialstyrelsen.lista_databaser()
    objekt = [
        _objekt(
            "databas", d.get("namn"), d.get("text"), None,
            hamta_api=f"/socialstyrelsen/dimensioner/{d.get('namn')}",
            fasetter={"amne": _FAST_AMNE["socialstyrelsen"]},
        )
        for d in databaser
    ]
    return objekt[:max_antal] if max_antal else objekt


async def _kat_skolverket(max_antal: int | None) -> list[dict[str, Any]]:
    # Källan är Utbildningsinfo (planned-educations), inte skolenhetsregistret,
    # eftersom den bär skolformen (typeOfSchooling) per enhet. Skolform är
    # avgörande för analys — annars hamnar förskolor i en gymnasieanalys. Varje
    # skola blir sökbar på namn, kommun OCH skolform; embedding hittar
    # "gymnasium"/"förskola", FTS hittar exakta namn och orter.
    kommun_namn: dict[str, str] = {}
    try:
        async with db.hamta_db() as anslutning:
            for r in await anslutning.fetch(
                f"SELECT kod, namn FROM {db.prefix()}kommun"
            ):
                kommun_namn[str(r["kod"])] = r["namn"]
    except Exception as fel:  # noqa: BLE001
        logger.warning("Kunde inte slå upp kommunnamn för skolenheter: %s", fel)

    objekt: list[dict[str, Any]] = []
    sida = 1
    while True:
        res = await skolverket.sok_planerade_utbildningar(sida=sida, per_sida=100)
        enheter = res.get("datapunkter", [])
        if not enheter:
            break
        for e in enheter:
            kod = e.get("code")
            namn = e.get("name")
            kommun = kommun_namn.get(str(e.get("geographicalAreaCode")), "")
            former = [
                t.get("displayName")
                for t in e.get("typeOfSchooling", [])
                if t.get("displayName")
            ]
            huvudman = e.get("principalOrganizerType")
            # Skolform först i beskrivningen — det är den viktigaste fasetten
            # för att filtrera/söka rätt skolor i en analys. Huvudman (kommunal/
            # fristående/…) läggs också i fritexten så den är sökbar, och som
            # hård fasett för exakt filtrering.
            beskrivning = " · ".join(
                p for p in (", ".join(former), huvudman, kommun) if p
            )
            objekt.append(_objekt(
                "skolenhet", kod, namn, beskrivning,
                hamta_api=f"/skolverket/skolenhet/{kod}",
                fasetter={
                    "skolform": former,
                    "huvudman": huvudman,
                    "amne": _FAST_AMNE["skolverket"],
                },
            ))
        if max_antal and len(objekt) >= max_antal:
            break
        antal_sidor = res.get("server_antal_sidor")
        if antal_sidor and sida >= antal_sidor:
            break
        if len(enheter) < 100:
            break
        sida += 1
    return objekt[:max_antal] if max_antal else objekt


async def _kat_fk(max_antal: int | None) -> list[dict[str, Any]]:
    dataset = await forsakringskassan.lista_dataset(limit=max_antal or 200)
    return [
        _objekt(
            "dataset", d.get("url"), d.get("titel"), None,
            hamta_api=f"/fk/data?sokvag={d.get('url')}",
            fasetter={"amne": _FAST_AMNE["forsakringskassan"]},
        )
        for d in dataset
    ]


async def _kat_kb(max_antal: int | None) -> list[dict[str, Any]]:
    # KB:s biblioteksstatistik är ett sammanhållet dataset (per bibliotek och
    # år) — ett sökbart objekt som infogar data direkt via /bibstat.
    return [_objekt(
        "dataset", "bibstat", "Sveriges biblioteksstatistik (KB)",
        "Biblioteksstatistik per bibliotek, år och målgrupp.",
        hamta_api="/bibstat/observationer",
        fasetter={"amne": "Kultur och fritid"},
    )]


# ============================================================================
# PxWeb-1-myndigheter — crawlas (ingen platt tabellsök i v1)
# ============================================================================
#
# Varje v1-myndighet får ett fast ämne (v1 exponerar inte ämnesväg som SCB v2).
_PXWEB1_AMNE: dict[str, str] = {
    "csn": "Utbildning",
    "energimyndigheten": "Energi",
    "skogsstyrelsen": "Jord- och skogsbruk, fiske",
    "folkhalsomyndigheten": "Hälso- och sjukvård",
    "jordbruksverket": "Jord- och skogsbruk, fiske",
    "ki_prognos": "Nationalräkenskaper",
    "konjunkturinstitutet": "Nationalräkenskaper",
    "tillvaxtanalys": "Näringsverksamhet",
}
# Taket finns för att en trasig eller cirkulär nodstruktur inte ska ge en
# oändlig crawl, inte för att begränsa täckningen. På 800 slog det för
# Folkhälsomyndigheten och KI-prognos, som stod på exakt 800 poster i
# indexet och alltså var avhuggna. Trädvandringen tar djupgränsen 7 som
# egentlig broms.
_PXWEB1_MAX = 5000


async def _crawla_pxweb1(myndighet: str, max_tabeller: int) -> list[dict[str, Any]]:
    """Vandrar en PxWeb-1-myndighets tabellträd och indexerar tabellerna.

    v1 saknar platt tabellsök — man navigerar databaser → mappar (`typ='l'`)
    → tabeller (`typ='t'`). Varje tabell får `hamta_api` mot /pxweb-1/metadata
    (sökväg som komma-separerad stig) så att panelens dimensionssteg kan
    öppna den (samma vy som SCB, v1-metadatan normaliseras till samma form).
    """
    from urllib.parse import quote

    from data_och_analys.klienter import pxweb_1

    amne = _PXWEB1_AMNE.get(myndighet)
    objekt: list[dict[str, Any]] = []

    async def vandra(path: list[str], djup: int = 0) -> None:
        if djup > 7 or len(objekt) >= max_tabeller:
            return
        try:
            noder = await pxweb_1.lista_noder(myndighet, path)
        except Exception:  # noqa: BLE001
            return
        for n in noder:
            if len(objekt) >= max_tabeller:
                return
            if n.typ == "t":
                stig = path + [n.id]
                sv = pxweb_1.foga_stig(stig)
                objekt.append(_objekt(
                    "tabell", sv, n.text, "",
                    hamta_api=(
                        f"/pxweb-1/metadata?myndighet={myndighet}"
                        f"&sokvag={quote(sv)}"
                    ),
                    fasetter={"amne": amne} if amne else None,
                ))
            elif n.typ == "l":
                await vandra(path + [n.id], djup + 1)

    try:
        for db in await pxweb_1.lista_databaser(myndighet):
            await vandra([db.dbid])
    except Exception as fel:  # noqa: BLE001
        logger.warning("PxWeb-1-crawl för %s misslyckades: %s", myndighet, fel)
    return objekt[:max_tabeller]


def _pxweb1_hamtare(
    myndighet: str,
) -> Callable[[int | None], Awaitable[list[dict[str, Any]]]]:
    async def hamtare(max_antal: int | None) -> list[dict[str, Any]]:
        return await _crawla_pxweb1(myndighet, max_antal or _PXWEB1_MAX)

    return hamtare


_KAT_HAMTARE: dict[str, Callable[[int | None], Awaitable[list[dict[str, Any]]]]] = {
    "kolada": _kat_kolada,
    "scb": _kat_scb,
    "riksbanken": _kat_riksbank,
    "trafa": _kat_trafa,
    "socialstyrelsen": _kat_socialstyrelsen,
    "forsakringskassan": _kat_fk,
    "skolverket": _kat_skolverket,
    "kb_bibstat": _kat_kb,
    **{m: _pxweb1_hamtare(m) for m in _PXWEB1_AMNE},
}


# ============================================================================
# Ingest
# ============================================================================


def _vektorlitteral(vektor: list[float]) -> str:
    """Formaterar en vektor som pgvector-litteral `[v1,v2,...]`."""
    return "[" + ",".join(repr(float(x)) for x in vektor) + "]"


# Semantisk sökning kräver pgvector och är därför Postgres-specifik —
# placeholders hårdkodas till $N i stället för db.ph() (sqlite saknar vektor).
_UPSERT = """
INSERT INTO {p}sok_index
    (kalla, typ, kalla_id, namn, beskrivning, hamta_api, extern_url, sok_text,
     embedding, fasetter)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector, $10::jsonb)
ON CONFLICT (kalla, kalla_id) DO UPDATE SET
    typ = EXCLUDED.typ, namn = EXCLUDED.namn, beskrivning = EXCLUDED.beskrivning,
    hamta_api = EXCLUDED.hamta_api, extern_url = EXCLUDED.extern_url,
    sok_text = EXCLUDED.sok_text, embedding = EXCLUDED.embedding,
    -- Fasetterna slås ihop i stället för att ersättas. Katalogbygget sätter
    -- `amne`; geo-blocket härleds separat av cli/synka_geo_fasetter.py och
    -- kostar ett metadataanrop per post. Ersattes hela objektet skulle varje
    -- ombyggnad kasta bort det arbetet — fyrtio minuter nätverk för att
    -- återskapa något som inte ändrats.
    fasetter = coalesce(sok_index.fasetter, '{}'::jsonb)
               || coalesce(EXCLUDED.fasetter, '{}'::jsonb),
    uppdaterad = now()
"""


def _kraver_postgres() -> None:
    if not db.ar_postgres():
        raise RuntimeError(
            "Semantisk sökning kräver Postgres med pgvector "
            "(DOA_DB_BACKEND=postgres). SQLite-vektorsök stöds inte här."
        )


def _schema_satser() -> list[str]:
    """Läser db/schema_sok.sql och delar i körbara satser (utan kommentarer)."""
    from pathlib import Path

    fil = Path(__file__).resolve().parents[2] / "db" / "schema_sok.sql"
    rader = [
        rad for rad in fil.read_text(encoding="utf-8").splitlines()
        if rad.strip() and not rad.lstrip().startswith("--")
    ]
    return [s.strip() for s in "\n".join(rader).split(";") if s.strip()]


async def _sakerstall_schema() -> None:
    """Skapar sök-schemat idempotent via en async-anslutning.

    `db.initiera_schema()` kan inte användas här — den startar en egen
    event-loop och passar bara i `__main__` före loopen. Den här varianten
    kör samma DDL men inifrån en redan körande loop.
    """
    async with db.hamta_db() as anslutning:
        for sats in _schema_satser():
            await anslutning.execute(sats)


async def bygg_index(
    kallor: list[str] | None = None,
    max_per_kalla: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Bygger (eller uppdaterar) sök-indexet från källornas kataloger.

    `kallor` begränsar till en delmängd (default alla). `max_per_kalla`
    tar bara de första N objekten per källa — användbart för snabba
    testkörningar. Returnerar antal indexerade objekt per källa.

    Bygget är kumulativt. En källa raderas aldrig i sin helhet för att
    något i den ändrats: nytillkomna objekt läggs till, ändrade uppdateras,
    oförändrade lämnas orörda, och bara det som försvunnit hos myndigheten
    beskärs bort.

    Skillnaden är inte kosmetisk. Embeddingen är det dyra steget, och
    `sok_index.fasetter` bär ett geo-block som härleds separat med ett
    metadataanrop per post. En ombyggnad som rör allt kostar därför både
    omberäkning av nitton tusen vektorer och fyrtio minuter nätverk för att
    återskapa fasetter som inte ändrats.

    Returnerar antal per källa: `{kalla: {nya, andrade, ororda, borttagna}}`.
    """
    _kraver_postgres()
    await _sakerstall_schema()
    valda = kallor or list(_KAT_HAMTARE)
    resultat: dict[str, int] = {}

    for kalla in valda:
        hamtare = _KAT_HAMTARE.get(kalla)
        if hamtare is None:
            continue
        try:
            objekt = await hamtare(max_per_kalla)
        except Exception as fel:  # noqa: BLE001
            logger.warning("Katalog-hämtning för %s misslyckades: %s", kalla, fel)
            resultat[kalla] = {"fel": str(fel)[:120]}
            continue

        async with db.hamta_db() as anslutning:
            # Vad som redan finns, och hur det såg ut. `sok_text` är det som
            # embeddingen bygger på — är den oförändrad är vektorn det också,
            # och raden behöver inte röras.
            befintliga = {
                r["kalla_id"]: r["sok_text"]
                for r in await anslutning.fetch(
                    f"SELECT kalla_id, sok_text FROM {db.prefix()}sok_index "
                    f"WHERE kalla = $1",
                    kalla,
                )
            }

            att_skriva = [
                o for o in objekt
                if befintliga.get(str(o["kalla_id"])) != o["sok_text"]
            ]
            nya = sum(1 for o in att_skriva if str(o["kalla_id"]) not in befintliga)
            andrade = len(att_skriva) - nya
            ororda = len(objekt) - len(att_skriva)

            for start in range(0, len(att_skriva), _BATCH):
                grupp = att_skriva[start:start + _BATCH]
                vektorer = embeddings.embed_texter([o["sok_text"] for o in grupp])
                rader = [
                    (
                        kalla, o["typ"], o["kalla_id"], o["namn"], o["beskrivning"],
                        o["hamta_api"], o["extern_url"], o["sok_text"],
                        _vektorlitteral(v),
                        json.dumps(o["fasetter"]) if o.get("fasetter") else None,
                    )
                    for o, v in zip(grupp, vektorer)
                ]
                await anslutning.executemany(
                    _UPSERT.replace("{p}", db.prefix()), rader)

            # Beskärningen: det som inte kom med i körningen är nedlagt hos
            # myndigheten. Bara det raderas — aldrig en hel källa.
            borttagna = await anslutning.fetchval(
                f"WITH bort AS ("
                f"  DELETE FROM {db.prefix()}sok_index "
                f"  WHERE kalla = $1 AND kalla_id <> ALL($2) RETURNING 1"
                f") SELECT count(*) FROM bort",
                kalla, [str(o["kalla_id"]) for o in objekt],
            )

        resultat[kalla] = {
            "nya": nya, "andrade": andrade,
            "ororda": ororda, "borttagna": borttagna or 0,
        }
        logger.info(
            "%s: %d nya, %d ändrade, %d orörda, %d borttagna",
            kalla, nya, andrade, ororda, borttagna or 0,
        )

    return resultat


# ============================================================================
# Hybrid-sökning
# ============================================================================


# Reciprocal Rank Fusion: varje signal bidrar med vikt/(k+position). Skalfritt
# — undviker att ts_rank (litet/ojämnt för korta titlar) och cosinuslikhet
# (0..1) måste tvingas till samma skala. Vikterna håller projektets 35/65-
# balans; k=60 är RRF-standard. Fusionen görs i Python (robust mot
# typinferens) ovanpå två enkla ranklistor från databasen.
_RRF_K = 60

# Fulltextträffar i fallande ts_rank-ordning. `{fasett}` byts mot ev.
# fasettvillkor (AND ...) före körning.
_FTS_SQL = """
SELECT si.id, si.kalla, si.typ, si.kalla_id, si.namn, si.beskrivning,
       si.hamta_api, si.extern_url, si.fasetter
FROM {p}sok_index si, websearch_to_tsquery('swedish', $1) AS tsq
WHERE si.fts @@ tsq{fasett}
ORDER BY ts_rank(si.fts, tsq) DESC
LIMIT 200
"""

# HNSW-indexet efterfiltrerar: grafen besöks först, villkoren tillämpas sedan
# på de kandidater som hittats. Med default ef_search=40 blir en filtrerad
# sökning därför nästan tom — geo-fasetten gav 2 rader av 13 405 matchande.
# Kandidatmängden måste vara minst lika stor som LIMIT för att sökningen ska
# kunna fylla den. 200 kostar 0,06 s mot 0,02, och taket i pgvector är 1000.
_EF_SEARCH = 200

# Närmaste grannar i vektorrum (cosine).
_VEC_SQL = """
SELECT si.id, si.kalla, si.typ, si.kalla_id, si.namn, si.beskrivning,
       si.hamta_api, si.extern_url, si.fasetter
FROM {p}sok_index si
WHERE TRUE{fasett}
ORDER BY si.embedding <=> $1::vector
LIMIT 200
"""

# Tillåtna fasettnycklar (whitelist — namnen injiceras i SQL). `skolform` är
# en lista (array-medlemskap), `huvudman` är skalär (likhet).
_ARRAY_FASETTER = {"skolform"}
# `geo` och `geo_niva` är inte vanliga fasetter utan villkor mot geo-blocket
# som `cli/synka_geo_fasetter.py` fyller. De hanteras separat nedan.
_TILLATNA_FASETTER = {"skolform", "huvudman", "amne", "geo", "geo_niva"}


def _fasett_villkor(
    fasetter: dict[str, Any] | None, start_idx: int
) -> tuple[str, list[Any]]:
    """Bygger AND-villkor + parametrar för fasettfiltrering.

    Endast vitlistade nycklar släpps in (namnen injiceras i SQL).
    Array-fasetter (skolform) matchas med JSONB-medlemskap (`?`), skalära
    (huvudman) med likhet. Returnerar (sql_fragment, params).
    """
    villkor: list[str] = []
    params: list[Any] = []
    idx = start_idx
    for nyckel, varde in (fasetter or {}).items():
        if nyckel not in _TILLATNA_FASETTER or varde in (None, ""):
            continue
        # Geo-blocket är ett objekt, inte en sträng eller array. `geo` frågar
        # bara om posten är geografiskt indelad alls; `geo_niva` om den finns
        # på en viss indelningsnivå.
        if nyckel == "geo":
            villkor.append("si.fasetter ? 'geo'")
            continue
        if nyckel == "geo_niva":
            villkor.append(f"(si.fasetter->'geo'->'nivaer') ? ${idx}")
            params.append(str(varde))
            idx += 1
            continue
        if nyckel in _ARRAY_FASETTER:
            villkor.append(f"(si.fasetter->'{nyckel}') ? ${idx}")
        else:
            villkor.append(f"si.fasetter->>'{nyckel}' = ${idx}")
        params.append(str(varde))
        idx += 1
    return "".join(f" AND {v}" for v in villkor), params


async def hybrid_sok(
    fraga: str,
    n: int = 20,
    kallor: list[str] | None = None,
    fasetter: dict[str, Any] | None = None,
    fts_vikt: float | None = None,
    vec_vikt: float | None = None,
) -> list[dict[str, Any]]:
    """Hybrid fulltext+vektor-sökning över indexet.

    Frågan utvidgas (synonymer) för FTS-delen; vektor-delen embeddar
    originalfrågan. Kandidater = fulltextträffar ∪ närmaste grannar, rerankade
    med viktad poäng (default 35 % FTS / 65 % vektor). Returnerar träffar med
    `hamta_api` — samma kontrakt som det federerade `/sok`.

    `kallor` filtrerar på källa efter rerank. Tomt index eller nedkopplad DB
    ger en tom lista (anroparen kan då falla tillbaka på federerad sök).
    """
    _kraver_postgres()
    fw = FTS_VIKT if fts_vikt is None else fts_vikt
    vw = VEC_VIKT if vec_vikt is None else vec_vikt

    # FTS: utvidgad fråga (OR mellan termer). Vektor: originalfrågan.
    termer = await query_expansion.expandera(fraga)
    fts_fraga = " or ".join(termer)
    qv = _vektorlitteral(embeddings.embed_fraga(fraga))

    fasett_clause, fasett_params = _fasett_villkor(fasetter, start_idx=2)
    fts_sql = _FTS_SQL.replace("{p}", db.prefix()).replace("{fasett}", fasett_clause)
    vec_sql = _VEC_SQL.replace("{p}", db.prefix()).replace("{fasett}", fasett_clause)
    async with db.hamta_db() as anslutning:
        fts_rader = await anslutning.fetch(fts_sql, fts_fraga, *fasett_params)
        if db.ar_postgres():
            await anslutning.execute(f"SET hnsw.ef_search = {_EF_SEARCH}")
        vec_rader = await anslutning.fetch(vec_sql, qv, *fasett_params)

    # Reciprocal Rank Fusion i Python.
    poang: dict[Any, float] = {}
    via_fts: set[Any] = set()
    via_vec: set[Any] = set()
    rad_per_id: dict[Any, dict[str, Any]] = {}

    for pos, rad in enumerate(fts_rader, start=1):
        rid = rad["id"]
        poang[rid] = poang.get(rid, 0.0) + fw / (_RRF_K + pos)
        via_fts.add(rid)
        rad_per_id.setdefault(rid, rad)
    for pos, rad in enumerate(vec_rader, start=1):
        rid = rad["id"]
        poang[rid] = poang.get(rid, 0.0) + vw / (_RRF_K + pos)
        via_vec.add(rid)
        rad_per_id.setdefault(rid, rad)

    # Token-andelsbonus: lyft träffar där frågans ord faktiskt förekommer i
    # namn/beskrivning, proportionellt mot andelen ord som matchar. Det fångar
    # både proper nouns ("Rudebecks") och kombinationer av begrepp och ort
    # ("grundskola umeå" → en Umeå-skola får 2/2, en Hjo-skola 1/2) som ren
    # vektor-närhet annars tappar. RRF-poängen ligger runt 0.01, så en
    # full-match-bonus på 0.05 lyfter tillförlitligt förbi vektor-bruset.
    qtokens = [t for t in fraga.casefold().split() if len(t) >= 2]
    if qtokens:
        for rid, rad in rad_per_id.items():
            text = f"{rad['namn'] or ''} {rad['beskrivning'] or ''}".casefold()
            traffade = sum(1 for t in qtokens if t in text)
            if traffade:
                poang[rid] = poang.get(rid, 0.0) + 0.05 * (traffade / len(qtokens))

    rangordnade = sorted(poang.items(), key=lambda kv: kv[1], reverse=True)

    valda = set(kallor) if kallor else None
    traffar: list[dict[str, Any]] = []
    for rid, score in rangordnade:
        rad = rad_per_id[rid]
        if valda and rad["kalla"] not in valda:
            continue
        rf = rad.get("fasetter")
        if isinstance(rf, str):
            try:
                rf = json.loads(rf)
            except (ValueError, TypeError):
                rf = None
        traffar.append({
            "kalla": rad["kalla"],
            "typ": rad["typ"],
            "id": rad["kalla_id"],
            "namn": rad["namn"],
            "beskrivning": rad["beskrivning"],
            "hamta_api": rad["hamta_api"],
            "extern_url": rad["extern_url"],
            "fasetter": rf,
            "score": round(score, 5),
            "via_fts": rid in via_fts,
            "via_vec": rid in via_vec,
        })
        if len(traffar) >= n:
            break
    return traffar


async def fasett_varden(kalla: str = "skolverket") -> dict[str, list[str]]:
    """Distinkta fasettvärden för en källa — för UI:ns filterlistor.

    Returnerar `{skolform: [...], huvudman: [...]}` med de värden som faktiskt
    förekommer i indexet, sorterade. Tomt om källan saknar fasetter.
    """
    p = db.prefix()
    async with db.hamta_db() as anslutning:
        skolform = await anslutning.fetch(
            f"SELECT DISTINCT jsonb_array_elements_text(fasetter->'skolform') AS v "
            f"FROM {p}sok_index WHERE kalla = $1 AND fasetter ? 'skolform' ORDER BY v",
            kalla,
        )
        huvudman = await anslutning.fetch(
            f"SELECT DISTINCT fasetter->>'huvudman' AS v FROM {p}sok_index "
            f"WHERE kalla = $1 AND fasetter ? 'huvudman' "
            f"AND fasetter->>'huvudman' IS NOT NULL ORDER BY v",
            kalla,
        )
    return {
        "skolform": [r["v"] for r in skolform if r["v"]],
        "huvudman": [r["v"] for r in huvudman if r["v"]],
    }


async def bladdra(
    amne: str | None = None,
    kallor: list[str] | None = None,
    skolform: str | None = None,
    huvudman: str | None = None,
    n: int = 50,
    sida: int = 1,
) -> list[dict[str, Any]]:
    """Bläddrar i indexet på enbart fasetter — utan sökfråga.

    Gör fasetterna användbara som fristående filter: välj ett ämne (och
    valfritt en eller flera källor) och få resultat, ordnade på namn.
    Ren SQL-filtrering, ingen embedding. Kräver minst ett filter.
    """
    _kraver_postgres()
    fasetter = {
        k: v
        for k, v in (("amne", amne), ("skolform", skolform), ("huvudman", huvudman))
        if v
    }
    fclause, params = _fasett_villkor(fasetter, start_idx=1)
    params = list(params)
    kclause = ""
    if kallor:
        params.append(kallor)
        kclause = f" AND si.kalla = ANY(${len(params)})"
    if not fclause and not kclause:
        return []

    p = db.prefix()
    sql = (
        f"SELECT si.kalla, si.typ, si.kalla_id AS id, si.namn, si.beskrivning, "
        f"si.hamta_api, si.extern_url, si.fasetter "
        f"FROM {p}sok_index si WHERE TRUE{fclause}{kclause} "
        f"ORDER BY si.namn LIMIT {int(n)} OFFSET {int((sida - 1) * n)}"
    )
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql, *params)

    traffar: list[dict[str, Any]] = []
    for rad in rader:
        rf = rad.get("fasetter")
        if isinstance(rf, str):
            try:
                rf = json.loads(rf)
            except (ValueError, TypeError):
                rf = None
        traffar.append({
            "kalla": rad["kalla"],
            "typ": rad["typ"],
            "id": rad["id"],
            "namn": rad["namn"],
            "beskrivning": rad["beskrivning"],
            "hamta_api": rad["hamta_api"],
            "extern_url": rad["extern_url"],
            "fasetter": rf,
        })
    return traffar


# ---------------------------------------------------------------------------
# Samfiltrerade fasettval
# ---------------------------------------------------------------------------
# Ett filterval som ger noll träffar ska inte gå att göra. Väljer man ämnet
# "Utbildning" ska källistan krympa till de myndigheter som har utbildningsdata,
# och väljer man Riksbanken ska ämneslistan krympa till det Riksbanken har.
#
# Varje fasett räknas med de ÖVRIGA fasetterna tillämpade, men inte sin egen.
# Utan det undantaget skulle den valda källan bli den enda som syns, och det
# gick då inte att byta källa utan att först nollställa filtret.

# Fasetter som kan väljas, och hur de hämtas ur fasetter-kolumnen.
_FASETTKALLOR: dict[str, str] = {
    "amne": "fasetter->>'amne'",
    "skolform": "jsonb_array_elements_text(fasetter->'skolform')",
    "huvudman": "fasetter->>'huvudman'",
    "niva": "jsonb_array_elements_text(fasetter->'geo'->'nivaer')",
}


def _urval_villkor(
    val: dict[str, Any], hoppa_over: str | None, start_idx: int
) -> tuple[str, list[Any]]:
    """Villkor för allt utom den fasett som räknas."""
    delar: list[str] = []
    params: list[Any] = []
    idx = start_idx
    for nyckel, varde in val.items():
        if nyckel == hoppa_over or varde in (None, "", [], False):
            continue
        if nyckel == "kallor":
            delar.append(f"kalla = ANY(${idx})")
            params.append(list(varde))
        elif nyckel == "geografisk":
            delar.append("fasetter ? 'geo'")
            continue
        elif nyckel == "niva":
            delar.append(f"(fasetter->'geo'->'nivaer') ? ${idx}")
            params.append(str(varde))
        elif nyckel == "skolform":
            delar.append(f"(fasetter->'skolform') ? ${idx}")
            params.append(str(varde))
        elif nyckel in ("amne", "huvudman"):
            delar.append(f"fasetter->>'{nyckel}' = ${idx}")
            params.append(str(varde))
        else:
            continue
        idx += 1
    return ("".join(f" AND {d}" for d in delar), params)


async def fasettval(val: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Vad som fortfarande är valbart, givet det som redan valts.

    Returnerar per fasett en lista `{varde, namn, antal}` — aldrig ett
    alternativ med noll träffar. `namn` är visningsformen; för källor
    myndighetens namn, för övriga samma som värdet.
    """
    val = dict(val or {})
    p = db.prefix()
    svar: dict[str, list[dict[str, Any]]] = {}

    async with db.hamta_db() as anslutning:
        # Källor
        villkor, params = _urval_villkor(val, "kallor", 1)
        rader = await anslutning.fetch(
            f"SELECT kalla AS v, count(*) AS antal FROM {p}sok_index "
            f"WHERE TRUE{villkor} GROUP BY kalla ORDER BY count(*) DESC",
            *params,
        )
        svar["kallor"] = [
            {"varde": r["v"], "namn": visningsnamn(r["v"]), "antal": r["antal"]}
            for r in rader if r["v"]
        ]
        # Källistan sorteras på namn, inte på antal: den läses som en lista
        # över myndigheter, och då är bokstavsordning det som går att hitta i.
        svar["kallor"].sort(key=lambda x: x["namn"].lower())

        # Övriga fasetter
        for fasett, uttryck in _FASETTKALLOR.items():
            villkor, params = _urval_villkor(val, fasett, 1)
            if "jsonb_array_elements_text" in uttryck:
                sql = (
                    f"SELECT x.v AS v, count(*) AS antal FROM {p}sok_index, "
                    f"LATERAL {uttryck} AS x(v) WHERE TRUE{villkor} "
                    f"GROUP BY x.v ORDER BY count(*) DESC"
                )
            else:
                sql = (
                    f"SELECT {uttryck} AS v, count(*) AS antal FROM {p}sok_index "
                    f"WHERE {uttryck} IS NOT NULL{villkor} "
                    f"GROUP BY 1 ORDER BY count(*) DESC"
                )
            rader = await anslutning.fetch(sql, *params)
            svar[fasett] = [
                {"varde": r["v"], "namn": r["v"], "antal": r["antal"]}
                for r in rader if r["v"]
            ]
    return svar


async def geo_nivaer(kallor: list[str] | None = None) -> list[dict[str, Any]]:
    """Räknar poster per indelningsnivå.

    Nivålistan är inte hårdkodad någonstans i klientlagret — den kommer
    härifrån, så en nivå som ingen källa levererar visas inte som ett val
    som sedan ger noll träffar.
    """
    p = db.prefix()
    villkor = "fasetter ? 'geo'"
    params: list[Any] = []
    if kallor:
        villkor += " AND kalla = ANY($1)"
        params.append(kallor)
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            f"SELECT n.niva, COUNT(*) AS antal FROM {p}sok_index si, "
            f"LATERAL jsonb_array_elements_text(si.fasetter->'geo'->'nivaer') AS n(niva) "
            f"WHERE {villkor} GROUP BY n.niva ORDER BY antal DESC",
            *params,
        )
    return [{"niva": r["niva"], "antal": r["antal"]} for r in rader]


async def amnen() -> list[dict[str, Any]]:
    """Distinkta ämnen i indexet med antal — tvärs över alla källor.

    Driver ämnesfasetten i UI:t: bläddra på kategori i stället för att gissa
    söktermer. Returnerar `[{amne, antal}]` sorterat på antal.
    """
    p = db.prefix()
    sql = (
        f"SELECT fasetter->>'amne' AS amne, COUNT(*) AS antal "
        f"FROM {p}sok_index WHERE fasetter ? 'amne' "
        f"AND fasetter->>'amne' IS NOT NULL GROUP BY amne ORDER BY antal DESC"
    )
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql)
    return [{"amne": r["amne"], "antal": r["antal"]} for r in rader]


async def antal_indexerat() -> dict[str, int]:
    """Returnerar antal indexerade objekt per källa (för status/health)."""
    sql = f"SELECT kalla, COUNT(*) AS antal FROM {db.prefix()}sok_index GROUP BY kalla"
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(sql)
    return {r["kalla"]: r["antal"] for r in rader}
