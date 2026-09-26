# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Migrationsverkets statistik — Excelfiler utan API.

Migrationsverket publicerar asyl-, tillstånds- och mottagningsstatistik som
Excelfiler på sin öppna data-sida. Det finns inget API, och det som ligger
om verket på dataportal.se kommer från RKA via Kolada, inte från verket
självt.

**URL:erna går inte att hårdkoda.** Nedladdningslänkarna har formen
`/download/18.<id>/<tidsstämpel>/<filnamn>.xlsx`, där tidsstämpeln är
publiceringstillfället i millisekunder och byts varje gång en fil
uppdateras. Länkarna läses därför ur öppna data-sidan vid varje körning.

**`..` betyder maskerat, inte noll.** Migrationsverket döljer små tal av
sekretesskäl. Läses de som nollor blir summor för låga och kommuner ser
tomma ut. Klienten returnerar `None` för dem och räknar dem separat, så att
maskeringen syns i svaret.

**Geografin är kommunnamn, inte koder.** Bladet "Län, kommun och månad"
bär namn, som måste översättas mot `kommun`-tabellen för att gå att joina
mot geo-stacken. Namn är tvetydiga — översättningen redovisar vad som inte
kunde matchas i stället för att tyst tappa rader.

Ingångspunkter:
    lista_dataset() -> list[dict]
    lista_blad(nyckel) -> list[str]
    hamta_tabell(nyckel, blad) -> dict
"""

from __future__ import annotations

import html
import io
import logging
import re
import time
from typing import Any

import httpx
import pandas as pd

from data_och_analys.infra import db, mellanlager
from data_och_analys.infra.rate_limit import hamta_grind

logger = logging.getLogger(__name__)

MYNDIGHET = "migrationsverket"
BAS_URL = "https://www.migrationsverket.se"
OPPNA_DATA = f"{BAS_URL}/om-migrationsverket/statistik/oppna-data.html"

# Webbplatsen avvisar anrop utan webbläsarlik User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
    "svensk-data-och-analys/MigrationsverketClient"
)
_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

# Tolkade tabeller läggs i `fil_mellanlager` med hållbarhet ur
# DOA_MELLANLAGRING_DAGAR. Filerna går att hämta igen och hör därför inte
# hemma i det permanenta beståndet — men att tolka om en 190 kB-arbetsbok
# vid varje fråga är onödigt tryck på källan.
#
# Länklistan cachas bara i processen: den är liten, och en ny tidsstämpel i
# en URL ska slå igenom direkt.
_LANK_TTL = 900.0
_cache: dict[str, tuple[float, Any]] = {}

# Migrationsverkets sekretessmarkering. Två punkter, ibland med blanksteg.
_MASKERAT = {"..", ". .", "…", "-", "–"}


def _fran_cache(nyckel: str) -> Any | None:
    post = _cache.get(nyckel)
    if post and time.monotonic() - post[0] < _LANK_TTL:
        return post[1]
    return None


def _till_cache(nyckel: str, varde: Any) -> None:
    _cache[nyckel] = (time.monotonic(), varde)


async def _hamta(url: str) -> bytes:
    await hamta_grind(MYNDIGHET).vanta()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as klient:
        svar = await klient.get(url)
    if svar.status_code != 200:
        raise httpx.HTTPStatusError(
            f"Migrationsverket svarade {svar.status_code} mot {url}",
            request=svar.request, response=svar,
        )
    return svar.content


def _nyckel(titel: str) -> str:
    """Stabil nyckel ur länktexten.

    Länktexten bär filstorlek och en skärmläsartext — "Avgjorda asylärenden
    xlsx, 62.9 kB, öppnas i nytt fönster." — som varierar när filen växer.
    Bara den beskrivande delen får bli nyckel, annars byter datasetet
    identitet varje månad.
    """
    ren = re.split(r"\s+(?:xlsx?|csv)\s*,", titel, maxsplit=1)[0].strip()
    ren = ren.lower().replace("å", "a").replace("ä", "a").replace("ö", "o")
    ren = re.sub(r"[^a-z0-9]+", "_", ren).strip("_")
    return ren or "okand"


def _titel(rå: str) -> str:
    return re.split(r"\s+(?:xlsx?|csv)\s*,", rå, maxsplit=1)[0].strip()


async def lista_dataset() -> list[dict[str, Any]]:
    """Datasetten på öppna data-sidan, med nyckel och aktuell URL."""
    cachad = _fran_cache("lista")
    if cachad is not None:
        return cachad

    sidan = (await _hamta(OPPNA_DATA)).decode("utf-8", "replace")
    par = re.findall(
        r'<a[^>]+href="(/download/[^"]+\.xlsx?)"[^>]*>(.*?)</a>', sidan, re.S | re.I
    )
    sedda: set[str] = set()
    dataset: list[dict[str, Any]] = []
    for href, rå in par:
        titel = _titel(html.unescape(re.sub(r"<[^>]+>", "", rå)).strip())
        nyckel = _nyckel(titel)
        if not titel or nyckel in sedda:
            continue
        sedda.add(nyckel)
        dataset.append({
            "nyckel": nyckel,
            "titel": titel,
            "url": BAS_URL + html.unescape(href),
        })
    _till_cache("lista", dataset)
    return dataset


async def _arbetsbok(nyckel: str) -> pd.ExcelFile:
    cachad = _fran_cache(f"bok:{nyckel}")
    if cachad is not None:
        return cachad
    for d in await lista_dataset():
        if d["nyckel"] == nyckel:
            bok = pd.ExcelFile(io.BytesIO(await _hamta(d["url"])))
            _till_cache(f"bok:{nyckel}", bok)
            return bok
    kanda = ", ".join(d["nyckel"] for d in await lista_dataset())
    raise ValueError(f"okänt dataset {nyckel!r} — kända: {kanda}")


async def lista_blad(nyckel: str) -> list[str]:
    """Bladen i ett dataset. Ett blad är en tabell."""
    return list((await _arbetsbok(nyckel)).sheet_names)


def _varde(x: Any) -> tuple[Any, bool]:
    """Returnerar (värde, maskerat)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None, False
    if isinstance(x, str) and x.strip() in _MASKERAT:
        return None, True
    return x, False


async def hamta_tabell(
    nyckel: str, blad: str, rubrikrad: int | None = None, max_rader: int = 5000
) -> dict[str, Any]:
    """Läser ett blad som rader.

    `rubrikrad` är nollindexerad. Utelämnad letar den upp första raden som
    har minst tre ifyllda celler — bladen inleds med en eller två rader
    beskrivande text som annars blir kolumnnamn.

    Svaret bär `maskerade`, antalet celler där Migrationsverket dolt ett
    litet tal. Det är inte noll och ska inte summeras som noll.
    """
    lagrad = await mellanlager.las(MYNDIGHET, nyckel, blad)
    if lagrad is not None:
        return {"dataset": nyckel, "blad": blad, "antal": len(lagrad["datapunkter"]),
                **lagrad}

    bok = await _arbetsbok(nyckel)
    if blad not in bok.sheet_names:
        raise ValueError(
            f"okänt blad {blad!r} i {nyckel!r} — finns: {', '.join(bok.sheet_names)}"
        )

    if rubrikrad is None:
        prov = bok.parse(blad, header=None, nrows=12)
        rubrikrad = 0
        for i, rad in prov.iterrows():
            if sum(1 for c in rad.tolist() if str(c) != "nan") >= 3:
                rubrikrad = int(i)
                break

    d = bok.parse(blad, header=rubrikrad, nrows=max_rader)
    kolumner = [str(c).strip() for c in d.columns]
    rader: list[dict[str, Any]] = []
    maskerade = 0
    for _, rad in d.iterrows():
        post: dict[str, Any] = {}
        tom = True
        for kol, x in zip(kolumner, rad.tolist()):
            v, m = _varde(x)
            if m:
                maskerade += 1
            if v is not None:
                tom = False
            post[kol] = v
        if not tom:
            rader.append(post)

    url = next((d["url"] for d in await lista_dataset() if d["nyckel"] == nyckel), None)
    await mellanlager.skriv(
        MYNDIGHET, nyckel, blad, kolumner, rader,
        maskerade=maskerade, kalla_url=url,
    )
    return {
        "dataset": nyckel,
        "blad": blad,
        "kolumner": kolumner,
        "maskerade": maskerade,
        "antal": len(rader),
        "kalla_url": url,
        "ur_mellanlager": False,
        "datapunkter": rader,
    }


# ---------------------------------------------------------------------------
# Koppling till kommunkoder
# ---------------------------------------------------------------------------
# Bladen bär kommunnamn, inte koder. För att gå att joina mot geo-stacken
# måste de översättas — och namn är tvetydiga. Källan skriver dessutom
# "Totalt" som kommunrad per län, vilket är en summa och inte en kommun.

_TOTALRADER = {"totalt", "total", "summa", "okänd kommun", "okänt"}


def _namnnyckel(namn: str) -> str:
    """Normaliserar ett kommunnamn för jämförelse.

    Myndigheter stavar inte lika: Migrationsverket skriver "Upplands-Väsby"
    med bindestreck där SCB skriver "Upplands Väsby" med mellanslag. Samma
    sak gäller "Malung-Sälen" och "Hedemora". Skiljetecken och blanksteg tas
    därför bort före jämförelsen — bokstäverna får avgöra.

    Normaliseringen är avsiktligt smal. Den jämnar ut skrivsätt, inte namn:
    två olika kommuner får aldrig samma nyckel av den här.
    """
    return re.sub(r"[\s\-–—'’.]", "", (namn or "").strip().lower())


async def hamta_per_kommun(
    nyckel: str, blad: str, kommunkolumn: str = "Kommun"
) -> dict[str, Any]:
    """Som `hamta_tabell`, men med kommunkod påsatt.

    Länsvisa totalrader ("Totalt") filtreras bort — de är summor och skulle
    dubbelräknas i en kartvy.

    Namn som inte går att matcha redovisas i `omatchade` i stället för att
    tyst försvinna. En kommun som byter namn, eller stavas annorlunda hos
    Migrationsverket än hos SCB, ska synas som ett problem att lösa.
    """
    tabell = await hamta_tabell(nyckel, blad)
    if kommunkolumn not in tabell["kolumner"]:
        raise ValueError(
            f"bladet {blad!r} har ingen kolumn {kommunkolumn!r} — "
            f"finns: {', '.join(tabell['kolumner'][:8])}"
        )

    async with db.hamta_db() as anslutning:
        koder = {
            _namnnyckel(r["namn"]): r["kod"]
            for r in await anslutning.fetch(
                f"SELECT kod, namn FROM {db.prefix()}kommun"
            )
        }

    rader: list[dict[str, Any]] = []
    omatchade: set[str] = set()
    for rad in tabell["datapunkter"]:
        namn = str(rad.get(kommunkolumn) or "").strip()
        if not namn or namn.lower() in _TOTALRADER:
            continue
        kod = koder.get(_namnnyckel(namn))
        if kod is None:
            omatchade.add(namn)
            continue
        rader.append({"kommun_kod": kod, **rad})

    return {
        **{k: v for k, v in tabell.items() if k != "datapunkter"},
        "antal": len(rader),
        "omatchade": sorted(omatchade),
        "datapunkter": rader,
    }
