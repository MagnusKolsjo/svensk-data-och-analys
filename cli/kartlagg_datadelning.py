# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Kartlägger vilka datadelningstjänster kommuner, regioner och myndigheter använder.

Två källor vägs samman, eftersom ingen av dem ensam ger hela bilden.

Federationen. Dataportal.se federerar kataloger som uppfyller DCAT-AP och
skördar dem med en pipeline per katalog. Pipelinens fetch-steg bär exakt
den URL som skördas, så den är det säkraste beskedet om var en
organisation delar data. Den visar däremot bara det som är anmält.

Spindlingen. För varje organisations domän prövas vanliga värdnamn
(oppnadata., data., catalog. med flera) och sökvägar (/oppnadata,
/oppna-data med flera). Den hittar också tjänster som inte federeras —
en Huwise-portal, en PxWeb-databas eller en webbsida med filer.

Varje värd som svarar fingeravtrycks mot plattformarnas egna API:er
(EntryScape, CKAN, Huwise, DKAN, ArcGIS Hub, PxWeb). Ett
API som svarar väger tyngre än ett ord i HTML-koden, och rapporten säger
vilket som gällde.

Organisationslistor, alla ur öppna källor:
    kommuner, regioner  SKR:s förteckningar, kompletterade med Wikidatas
                        webbadresser där de skiljer sig
    myndigheter         SCB:s myndighetsregister, med tidigare namn ur
                        Statskontorets myndighetsinformation. Tidigare namn
                        behövs: många kataloger är anmälda under ett namn
                        myndigheten inte längre har.

En egen myndighetslista kan ges som CSV med kolumnerna namn, kategori,
orgnr, webbadresser och valfritt alias (tidigare namn, skilda med |).

    python cli/kartlagg_datadelning.py
    python cli/kartlagg_datadelning.py --bara kommun
    python cli/kartlagg_datadelning.py --myndigheter <FIL>.csv

Utdata hamnar i --ut (standard .cache/datadelning/): rapport.md,
fynd.csv med varje prövad kandidat som svarat, och organisationer.csv.

Ingångspunkter:
    main() -> int
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import re
import secrets
import socket
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_och_analys.geodata import synkstatus
from data_och_analys.infra import db, konfig
from data_och_analys.klienter import myndighetsregister
from data_och_analys.oppnadata import register

# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

# HTTP-huvuden måste vara ASCII.
USER_AGENT = (
    "svensk-data-och-analys/kartlaggning-datadelning "
    "(kartlaggning av oppna datakataloger; +https://github.com/MagnusKolsjo)"
)

# Prefixen är användarens lista plus de som faktiskt förekommer bland
# värdarna som dataportal.se skördar (catalog., metadatakatalog. m.fl.).
SUBDOMANER = (
    "oppnadata", "data", "opendata", "psidata", "oppendata",
    "catalog", "katalog", "datakatalog", "metadata", "metadatakatalog",
)
SOKVAGAR = (
    "oppnadata", "oppna-data", "data", "opendata", "open-data",
    "psidata", "psi-data", "oppendata",
)

SKR_KOMMUNER = "https://skr.se/skr/tjanster/kommunerochregioner/kommunerlista.8288.html"
SKR_REGIONER = "https://skr.se/skr/tjanster/kommunerochregioner/regionerlista.8289.html"
WIKIDATA = "https://query.wikidata.org/sparql"
DATAPORTAL_STORE = "https://admin.dataportal.se/store"

# Diggs EntryScape-instans där organisationer utan egen katalog för in sina
# dataset. Tekniskt EntryScape, men ingen egen tjänst hos organisationen.
REGISTRERINGSTJANST = {"editera.dataportal.se", "admin.dataportal.se", "registrera.oppnadata.se"}

MAX_SAMTIDIGA = 40
MAX_PER_VARD = 2
MAX_BYTE = 400_000
STORE_MAX_BYTE = 50_000_000

FILANDELSER = (".csv", ".xlsx", ".xls", ".json", ".geojson", ".zip", ".xml", ".rdf", ".ods")
OPPNA_DATA_RE = re.compile(r"öppna\s*data|oppna\s*data|open\s*data|psi-?data|vidareutnyttjande", re.I)

# Ord i HTML som pekar på en plattform. Svagare belägg än ett API-svar och
# redovisas därför som "omnämnande".
HTML_MARKORER = (
    ("EntryScape", re.compile(r"entryscape|metasolutions", re.I)),
    # opendatasoft är Huwise tidigare namn och finns kvar i äldre portalers HTML.
    ("Huwise", re.compile(r"huwise|opendatasoft|ods-app", re.I)),
    ("CKAN", re.compile(r"\bckan\b", re.I)),
    ("ArcGIS Hub", re.compile(r"hub\.arcgis|arcgis hub|arcgishub", re.I)),
    ("DKAN", re.compile(r"\bdkan\b", re.I)),
    ("PxWeb", re.compile(r"px-?web", re.I)),
    ("GeoNetwork", re.compile(r"geonetwork", re.I)),
)


# ---------------------------------------------------------------------------
# Datamodell
# ---------------------------------------------------------------------------

@dataclass
class Organisation:
    kategori: str            # kommun, region, myndighet
    namn: str
    kod: str = ""            # kommunkod, länsbokstav eller orgnr
    underkategori: str = ""
    webbadresser: list[str] = field(default_factory=list)
    domaner: list[str] = field(default_factory=list)
    vard_hos: str = ""       # värdmyndighet vars webbplats organisationen ligger på
    lan_kod: str = ""        # tvåsiffrig länskod för kommuner och regioner
    alias: list[str] = field(default_factory=list)  # tidigare namn, kortnamn
    tidigare_orgnr: list[str] = field(default_factory=list)  # föregångares organisationsnummer

    @property
    def nyckel(self) -> str:
        return f"{self.kategori}:{self.kod or self.namn}"


@dataclass
class Svar:
    url: str
    status: int = 0
    slutlig_url: str = ""
    innehallstyp: str = ""
    text: str = ""
    huvuden: dict = field(default_factory=dict)
    fel: str = ""
    ogiltigt_cert: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.fel

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None


@dataclass
class Avtryck:
    typ: str                 # plattform, "webbsida" eller "okänd"
    api: str = ""
    antal: int | None = None
    belagg: str = ""         # "API svarar" eller "omnämnande i HTML"


@dataclass
class Fynd:
    org: str                 # Organisation.nyckel
    kalla: str               # federation, spindel, länk
    kandidat: str
    status: int
    slutlig_url: str
    typ: str
    api: str = ""
    antal: int | None = None
    belagg: str = ""
    ogiltigt_cert: bool = False
    anteckning: str = ""


@dataclass
class Federerad:
    kontext: str
    titel: str
    kalla_url: str           # pipelinens fetch-källa; tom när katalogen förs direkt i dataportalen
    pipelinetitel: str = ""  # skiljer sig ofta från katalogens titel
    utgivare_uri: str = ""
    hemsida: str = ""
    antal_dataset: int | None = None  # ur senaste skördningen
    status: str = ""         # senaste skördningens utfall: Success, Failed
    senast: str = ""         # datum för senaste skördningen
    valideringsfel: int | None = None
    org: str = ""            # matchad Organisation.nyckel
    kopplad_via: str = ""    # orgnr, namn, tidigare namn, domän

    @property
    def skordas(self) -> bool:
        return self.status == "Success"


# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def registrerbar(vard: str) -> str:
    """Sista två leden av ett värdnamn: www.kommun.vetlanda.se -> vetlanda.se."""
    led = (vard or "").lower().strip(".").split(".")
    return ".".join(led[-2:]) if len(led) >= 2 else ""


def vard_ur(url: str) -> str:
    if not re.match(r"^[a-z]+://", url or "", re.I):
        url = "https://" + (url or "")
    return (urlparse(url).hostname or "").lower()


def normalisera_namn(namn: str) -> str:
    """Jämförbar form av ett organisationsnamn.

    "Stockholms stad", "Stockholm" och "STOCKHOLMS KOMMUN" ska mötas, liksom
    myndighetsnamn med och utan förkortning efter tankstreck.
    """
    n = unicodedata.normalize("NFKC", namn or "").lower()
    n = re.split(r"\s+[-–]\s+", n)[0]
    # Katalogtitlar lägger till ord som inte hör till namnet:
    # "Statens konstråds öppna data", "Huddinge kommun datakatalog".
    n = re.sub(r"\b(öppna data|oppna data|open data|datakatalog|metadatakatalog|katalog|"
               r"psi-data|psidata)\b", " ", n)
    n = re.sub(r"s\s*$", "", n.strip()) if n.strip().endswith("s") and len(n.strip()) > 4 else n
    # Landstingen blev regioner. "Landstinget i Kalmar län", "Norrbottens
    # läns landsting" och "Region Kalmar län" ska mötas.
    n = re.sub(r"\b(kommun|stad|kommunkoncern|region|landstinget|landsting|läns|län|i)\b",
               " ", n)
    n = re.sub(r"[^\wåäö ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return re.sub(r"s$", "", n) if len(n) > 4 else n


STATLIG_TITEL = re.compile(
    r"rätt(en)?\b|domstol|nämnd|verket\b|myndighet|institut|universitet|högskola"
    r"|museum|museer|länsstyrelse|inspektion", re.I)


def kategori_ur_namn(namn: str) -> str | None:
    n = (namn or "").lower()
    if re.search(r"\bkommun\b|\bstad\b|kommunkoncern", n):
        return "kommun"
    if re.search(r"^region\b|landsting|regionen\b", n):
        return "region"
    return None


def titel_ur(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()[:120] if m else ""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Hamtare:
    """Artig hämtare: globalt tak, högst två samtidiga anrop per värd.

    Ett ogiltigt certifikat är vanligt på kommunala underdomäner. Då görs ett
    nytt försök utan verifiering så att tjänsten ändå kan identifieras, och
    bristen redovisas i stället för att värden tyst räknas som död.
    """

    def __init__(self) -> None:
        begr = httpx.Limits(max_connections=MAX_SAMTIDIGA * 2)
        tid = httpx.Timeout(15.0, connect=7.0)
        huvud = {"User-Agent": USER_AGENT, "Accept-Language": "sv,en;q=0.5"}
        self._klient = httpx.AsyncClient(
            follow_redirects=True, timeout=tid, limits=begr, headers=huvud)
        self._osaker = httpx.AsyncClient(
            follow_redirects=True, timeout=tid, limits=begr, headers=huvud, verify=False)
        self._global = asyncio.Semaphore(MAX_SAMTIDIGA)
        self._per_vard: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(MAX_PER_VARD))
        self._dns: dict[tuple, asyncio.Future] = {}
        # getaddrinfo körs i en trådpool. Tusentals samtidiga uppslag köar
        # där, och ett uppslag som väntat ut sin tidsgräns i kön säger
        # ingenting om värden — därför en egen gräns för DNS.
        self._dns_grans = asyncio.Semaphore(16)
        # Organisationer som delar domän (länsstyrelserna) prövar samma
        # adresser; en adress hämtas bara en gång.
        self._minne: dict[tuple, asyncio.Future] = {}
        self.antal_anrop = 0
        self.antal_dns = 0

    async def stang(self) -> None:
        await self._klient.aclose()
        await self._osaker.aclose()

    async def finns(self, vard: str, bekrafta: bool = False) -> bool:
        """DNS-uppslag först — de flesta kandidatvärdar finns inte alls.

        Bara EAI_NONAME är ett nej. Ett tillfälligt resolverfel under last
        gav annars olika svar mellan två körningar. Med bekrafta prövas även
        ett nej en gång till — för värdar organisationen bevisligen använt,
        där ett felaktigt nej vore ett påstående om en död tjänst.
        """
        nyckel = (vard, bekrafta)
        if nyckel not in self._dns:
            self._dns[nyckel] = asyncio.ensure_future(self._sla_upp(vard, bekrafta))
        return await self._dns[nyckel]

    async def _sla_upp(self, vard: str, bekrafta: bool) -> bool:
        for forsok in range(3):
            async with self._dns_grans:
                self.antal_dns += 1
                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().getaddrinfo(vard, 443, type=socket.SOCK_STREAM),
                        timeout=10)
                    return True
                except socket.gaierror as e:
                    if e.errno == socket.EAI_NONAME and not (bekrafta and forsok == 0):
                        return False
                except (OSError, asyncio.TimeoutError):
                    pass
            await asyncio.sleep(2 + 3 * forsok)
        # Inget säkert besked efter tre försök — låt HTTP-anropet avgöra.
        return True

    async def hamta(self, url: str, accept: str = "*/*", max_byte: int = MAX_BYTE,
                    tidsgrans: float | None = None) -> Svar:
        """Läser högst max_byte. Webbsidor behöver bara början för att
        identifieras; ett JSON-svar som kapas går däremot inte att tolka alls,
        så API-anrop med stora svar måste höja taket."""
        if max_byte == MAX_BYTE and tidsgrans is None:
            nyckel = (url, accept)
            if nyckel not in self._minne:
                self._minne[nyckel] = asyncio.ensure_future(self._hamta_begransat(url, accept))
            return await self._minne[nyckel]
        return await self._hamta_begransat(url, accept, max_byte, tidsgrans)

    async def _hamta_begransat(self, url: str, accept: str, max_byte: int = MAX_BYTE,
                               tidsgrans: float | None = None) -> Svar:
        vard = vard_ur(url)
        async with self._global, self._per_vard[vard]:
            svar = await self._hamta(self._klient, url, accept, max_byte, tidsgrans)
            if svar.fel and re.search(r"certificate|ssl", svar.fel, re.I):
                svar = await self._hamta(self._osaker, url, accept, max_byte, tidsgrans)
                svar.ogiltigt_cert = True
            return svar

    async def _hamta(self, klient: httpx.AsyncClient, url: str, accept: str,
                     max_byte: int, tidsgrans: float | None) -> Svar:
        svar = await self._hamta_en_gang(klient, url, accept, max_byte, tidsgrans)
        # Ett anslutningsfel eller en timeout prövas en gång till; enstaka
        # nätfel ska inte bli påståenden om att en tjänst saknas.
        if svar.fel and re.match(r"(ConnectError|ConnectTimeout|ReadTimeout|RemoteProtocolError)",
                                 svar.fel) and not re.search(r"certificate|ssl", svar.fel, re.I):
            await asyncio.sleep(3)
            svar = await self._hamta_en_gang(klient, url, accept, max_byte, tidsgrans)
        return svar

    async def _hamta_en_gang(self, klient: httpx.AsyncClient, url: str, accept: str,
                             max_byte: int, tidsgrans: float | None) -> Svar:
        self.antal_anrop += 1
        extra = {"timeout": httpx.Timeout(tidsgrans, connect=10.0)} if tidsgrans else {}
        try:
            async with klient.stream("GET", url, headers={"Accept": accept}, **extra) as r:
                delar, storlek = [], 0
                async for bit in r.aiter_bytes():
                    delar.append(bit)
                    storlek += len(bit)
                    if storlek >= max_byte:
                        break
                kodning = r.encoding or "utf-8"
                return Svar(
                    url=url, status=r.status_code, slutlig_url=str(r.url),
                    innehallstyp=r.headers.get("content-type", ""),
                    text=b"".join(delar).decode(kodning, errors="replace"),
                    huvuden={k.lower(): v for k, v in r.headers.items()})
        except httpx.HTTPError as e:
            return Svar(url=url, fel=f"{type(e).__name__}: {e}"[:200])
        except Exception as e:  # noqa: BLE001 — en trasig värd får aldrig stoppa svepet
            return Svar(url=url, fel=f"{type(e).__name__}: {e}"[:200])


# ---------------------------------------------------------------------------
# Organisationslistor
# ---------------------------------------------------------------------------

def _skr_lankar(text: str) -> list[tuple[str, str]]:
    """Namn och webbplats ur SKR:s förteckningar.

    Förteckningarnas egna länkar bär texten "Länk till annan webbplats";
    sidfotens och navigeringens gör det inte, och sorteras därmed bort.
    """
    ut = []
    for u, n in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', text, re.S):
        n = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", n))).strip()
        if "Länk till annan webbplats" in n and "LinkedIn" not in n:
            ut.append((n.replace("Länk till annan webbplats.", "").strip(), u))
    return list(dict.fromkeys(ut))


async def las_kommuner_och_regioner(h: Hamtare) -> list[Organisation]:
    svar_k, svar_r = await asyncio.gather(h.hamta(SKR_KOMMUNER), h.hamta(SKR_REGIONER))
    if not (svar_k.ok and svar_r.ok):
        raise RuntimeError("SKR:s förteckningar gick inte att hämta — "
                           f"{svar_k.status or svar_k.fel} / {svar_r.status or svar_r.fel}")

    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(f"SELECT kod, namn, lan_kod FROM {db.prefix()}kommun")
        lan = await anslutning.fetch(f"SELECT kod, bokstav FROM {db.prefix()}lan")
    kod_for = {r["namn"]: r["kod"] for r in rader}
    lan_for_kommun = {r["kod"]: r["lan_kod"] for r in rader}
    lan_for_bokstav = {r["bokstav"]: r["kod"] for r in lan if r["bokstav"]}

    # Wikidata kompletterar: där den anger en annan domän än SKR prövas
    # båda, eftersom kommuner ofta har kvar en äldre domän i bruk.
    fraga = ('SELECT ?kod ?webb WHERE { ?k wdt:P31 wd:Q127448; wdt:P525 ?kod; '
             'wdt:P856 ?webb . }')
    wd = await h.hamta(f"{WIKIDATA}?query={quote(fraga)}",
                       accept="application/sparql-results+json")
    extra: dict[str, set[str]] = defaultdict(set)
    for b in ((wd.json() or {}).get("results", {}).get("bindings", [])):
        extra[b["kod"]["value"]].add(b["webb"]["value"])

    orgs: list[Organisation] = []
    saknade = []
    for namn, webb in _skr_lankar(svar_k.text):
        kod = kod_for.get(namn, "")
        if not kod:
            saknade.append(namn)
        webbar = [webb, *sorted(extra.get(kod, ()))]
        orgs.append(Organisation("kommun", namn, kod, webbadresser=webbar,
                                 lan_kod=lan_for_kommun.get(kod, "")))
    for namn, webb in _skr_lankar(svar_r.text):
        m = re.match(r"(.*?)\s*\(länsbokstav\s+([A-ZÅÄÖ]+)\)", namn)
        bokstav = m.group(2) if m else ""
        orgs.append(Organisation("region", m.group(1) if m else namn, bokstav,
                                 webbadresser=[webb], lan_kod=lan_for_bokstav.get(bokstav, "")))
    if saknade:
        print(f"  Obs: {len(saknade)} SKR-namn saknar kommunkod: {', '.join(saknade)}")
    return orgs


def las_myndigheter(sokvag: Path) -> list[Organisation]:
    orgs = []
    with sokvag.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            orgs.append(Organisation(
                "myndighet", r["namn"].strip(), (r.get("orgnr") or "").strip(),
                underkategori=(r.get("kategori") or "").strip(),
                webbadresser=(r.get("webbadresser") or "").split(),
                alias=[a.strip() for a in (r.get("alias") or "").split("|") if a.strip()]))
    return orgs


async def las_myndigheter_oppet() -> list[Organisation]:
    """Myndigheterna ur SCB:s register och Statskontorets historik.

    Domstolarna delar organisationsnummer med Domstolsverket. Ett delat
    nummer kan inte vara nyckel — då skulle domstolarna skriva över
    varandra — så de nycklas på namn.
    """
    myndigheter = await myndighetsregister.hamta_myndigheter()
    antal = defaultdict(int)
    for m in myndigheter:
        antal[m.orgnr] += 1
    # Föregångarnas domäner prövas också: en myndighet som gått upp i en annan
    # har ofta kvar sin webbplats och sin katalog där.
    return [Organisation("myndighet", m.namn, m.orgnr if antal[m.orgnr] == 1 else "",
                         underkategori=m.kategori,
                         webbadresser=m.webbadresser + [f"https://{d}" for d in m.tidigare_domaner],
                         alias=m.alias, tidigare_orgnr=m.tidigare_orgnr)
            for m in myndigheter]


def satt_domaner(orgs: list[Organisation]) -> None:
    for o in orgs:
        o.domaner = sorted({d for d in (registrerbar(vard_ur(w)) for w in o.webbadresser) if d})

    # En nämnd vars enda adress är en sökväg på en annan myndighets webbplats
    # (domstol.se/notarienamnden) har ingen egen webbplats att spindla. Det
    # som finns på domänen hör till värdmyndigheten. Länsstyrelserna delar
    # däremot domän som jämlikar — ingen av dem har domänens rot — och
    # prövas var för sig mot den gemensamma tjänsten.
    def rot(w: str) -> bool:
        return urlparse(w if "://" in w else "https://" + w).path.strip("/") == ""
    rotagare: dict[str, Organisation] = {}
    for o in orgs:
        for w in o.webbadresser:
            if rot(w):
                rotagare.setdefault(registrerbar(vard_ur(w)), o)
    for o in orgs:
        if o.kategori != "myndighet" or not o.webbadresser or any(rot(w) for w in o.webbadresser):
            continue
        # Statens tjänstepensionsverk har en sökväg som adress men är
        # värd, inte gäst. Bara nämnder, delegationer och domstolarna (på
        # domstol.se) brukar ligga hos någon annan.
        if not (re.search(r"nämnd|delegation", o.namn, re.I) or o.underkategori == "domstol"):
            continue
        agare = rotagare.get(o.domaner[0]) if o.domaner else None
        if agare and agare is not o:
            o.vard_hos = agare.namn


# ---------------------------------------------------------------------------
# Federationen — vad dataportal.se skördar
# ---------------------------------------------------------------------------

async def _sok_store(h: Hamtare, fraga: str) -> list[dict]:
    ut, forskjutning = [], 0
    while True:
        u = (f"{DATAPORTAL_STORE}/search?type=solr&limit=50&offset={forskjutning}"
             f"&query={quote(fraga)}")
        # Katalogposterna bär hela datasetlistan och är tunga att ta fram.
        svar = await h.hamta(u, accept="application/json", max_byte=STORE_MAX_BYTE,
                             tidsgrans=120)
        d = svar.json()
        if d is None:
            raise RuntimeError(f"Dataportalens store gav inget tolkbart svar på {fraga!r} "
                               f"(offset {forskjutning}): {svar.fel or svar.status}")
        barn = d.get("resource", {}).get("children", [])
        ut += barn
        forskjutning += len(barn)
        if not barn or forskjutning >= d.get("results", 0):
            return ut


def _forsta(po: dict, pred: str) -> str:
    v = po.get(pred) or []
    return v[0].get("value", "") if v else ""


async def las_federationen(h: Hamtare) -> list[Federerad]:
    """Kataloger och skördepipelines ur dataportalens EntryScape-store.

    Katalogposten ger utgivare och hemsida, pipelinen ger källan. De möts
    i kontexten: en kontext per katalog.
    """
    kataloger = await _sok_store(h, r"rdfType:http\://www.w3.org/ns/dcat#Catalog")
    pipelines = await _sok_store(h, "graphType:Pipeline")

    # Sökningen ger inte pipelinens resurs; den hämtas per pipeline.
    async def kalla(e: dict) -> tuple[str, str, str]:
        u = f"{DATAPORTAL_STORE}/{e['contextId']}/resource/{e['entryId']}"
        r = (await h.hamta(u, accept="application/json", max_byte=STORE_MAX_BYTE)).json() or {}
        arg = {}
        for s, po in r.items():
            if _forsta(po, "http://entrystore.org/terms/transformArgumentKey") == "source":
                arg[s] = _forsta(po, "http://entrystore.org/terms/transformArgumentValue")
        kallor = [arg.get(a["value"], "")
                  for po in r.values()
                  if _forsta(po, "http://entrystore.org/terms/transformType") == "fetch"
                  for a in po.get("http://entrystore.org/terms/transformArgument", [])]
        titel = ""
        for po in (e.get("metadata") or {}).values():
            titel = titel or _forsta(po, "http://purl.org/dc/terms/title")
        return e["contextId"], next((k for k in kallor if k), ""), titel

    kallor = dict((k, (u, t)) for k, u, t in await asyncio.gather(*(kalla(e) for e in pipelines)))

    # Senaste körningen per kontext. Den bär utfallet, antal skördade
    # dataset och valideringsfel — skillnaden mellan en levande federation
    # och en anmälan som pekar på en adress som slutat svara.
    senaste: dict[str, dict] = {}
    for e in await _sok_store(h, "graphType:PipelineResult AND tag.literal:latest"):
        info = {}
        for po in (e.get("info") or {}).values():
            info.update(po)
        pr = "http://entrystore.org/terms/pipelineresult#"
        fel = next((_forsta(po, pr + "validateErrors") for po in (e.get("metadata") or {}).values()
                    if _forsta(po, pr + "validateErrors")), "")
        rad = {
            "status": _forsta(info, "http://entrystore.org/terms/status").rsplit("/", 1)[-1],
            "senast": _forsta(info, "http://purl.org/dc/terms/modified")[:10],
            "fel": fel,
        }
        tidigare = senaste.get(e["contextId"])
        if not tidigare or rad["senast"] > tidigare["senast"]:
            senaste[e["contextId"]] = rad

    ut: dict[str, Federerad] = {}
    for e in kataloger:
        for po in (e.get("metadata") or {}).values():
            if "http://www.w3.org/ns/dcat#Catalog" not in [
                    x.get("value") for x in po.get("http://www.w3.org/1999/02/22-rdf-syntax-ns#type", [])]:
                continue
            ktx = e["contextId"]
            u, pt = kallor.get(ktx, ("", ""))
            ut[ktx] = Federerad(
                kontext=ktx,
                titel=_forsta(po, "http://purl.org/dc/terms/title") or pt,
                kalla_url=u,
                pipelinetitel=pt,
                utgivare_uri=_forsta(po, "http://purl.org/dc/terms/publisher"),
                hemsida=_forsta(po, "http://xmlns.com/foaf/0.1/homepage"))
    # En pipeline utan katalogpost — skördningen har aldrig gett någon katalog.
    for ktx, (u, t) in kallor.items():
        ut.setdefault(ktx, Federerad(kontext=ktx, titel=t, kalla_url=u, pipelinetitel=t))
    for ktx, f in ut.items():
        s = senaste.get(ktx)
        if s:
            f.status, f.senast = s["status"], s["senast"]
            f.valideringsfel = int(s["fel"]) if s["fel"].isdigit() else None

    # Körresultatet räknar bara det som ändrats i en körning; en oförändrad
    # katalog bär inga siffror alls. Antalet räknas därför i kontexten.
    async def rakna(f: Federerad) -> None:
        fraga = (r"rdfType:http\://www.w3.org/ns/dcat#Dataset AND "
                 rf"context:{DATAPORTAL_STORE.replace(':', chr(92) + ':')}/{f.kontext}")
        d = (await h.hamta(f"{DATAPORTAL_STORE}/search?type=solr&limit=1&query={quote(fraga)}",
                           "application/json", tidsgrans=60)).json()
        if isinstance(d, dict):
            f.antal_dataset = d.get("results")
    await asyncio.gather(*(rakna(f) for f in ut.values()))
    return list(ut.values())


def matcha_federationen(fed: list[Federerad], orgs: list[Organisation]) -> None:
    """Knyter varje federerad katalog till en organisation.

    Domänen väger tyngst: utgivar-URI:n är oftast organisationens webbadress
    och källvärden en underdomän till den. Namnet används bara när ingen
    domän matchar, och *.entryscape.net matchas på första ledet.
    """
    per_doman: dict[str, list[Organisation]] = defaultdict(list)
    per_led: dict[str, list[Organisation]] = defaultdict(list)
    per_namn: dict[str, list[Organisation]] = defaultdict(list)
    per_alias: dict[str, list[Organisation]] = defaultdict(list)
    for o in orgs:
        for d in o.domaner:
            per_doman[d].append(o)
            per_led[d.split(".")[0]].append(o)
        per_namn[normalisera_namn(o.namn)].append(o)
        for a in o.alias:
            per_alias[normalisera_namn(a)].append(o)

    def entydig(kandidater: list[Organisation]) -> str:
        unika = {o.nyckel for o in kandidater}
        return unika.pop() if len(unika) == 1 else ""

    per_orgnr = {re.sub(r"\D", "", o.kod): o.nyckel for o in orgs
                 if o.kategori == "myndighet" and len(re.sub(r"\D", "", o.kod)) == 10}
    # En katalog anmäld av en myndighet som sedan gått upp i en annan bär
    # föregångarens nummer; den hör i dag till efterträdaren.
    for o in orgs:
        for nr in o.tidigare_orgnr:
            per_orgnr.setdefault(nr, o.nyckel)

    for f in fed:
        # 1. Organisationsnumret i utgivar-URI:n (…/organisation/SE2021000084).
        m = re.search(r"/organisation/SE(\d{10})", f.utgivare_uri or "")
        if m:
            f.org = per_orgnr.get(m.group(1), "")
            f.kopplad_via = "orgnr" if f.org else ""
        # 2. Exakt namn. Väger tyngre än domänen: Kammarrätten i Göteborg har
        # haft en adress under goteborg.se utan att vara kommunen. "Uppsala
        # kommun" och "Region Uppsala" normaliseras till samma ord, så namnet
        # matchar bara inom den kategori titeln anger.
        for index, via in ((per_namn, "namn"), (per_alias, "tidigare namn")):
            for titel in (f.titel, f.pipelinetitel):
                if f.org or not titel:
                    continue
                hint = kategori_ur_namn(titel)
                f.org = entydig([o for o in index.get(normalisera_namn(titel), [])
                                 if hint is None or o.kategori == hint])
                f.kopplad_via = via if f.org else ""
        # 3. Domänen, och *.entryscape.net på första ledet.
        for u in (f.utgivare_uri, f.kalla_url, f.hemsida):
            vard = vard_ur(u) if u else ""
            if f.org or not vard or vard.endswith("dataportal.se"):
                continue
            if vard.endswith((".entryscape.net", ".entryscape.com")):
                f.org = entydig(per_led.get(vard.split(".")[0], []))
            else:
                f.org = entydig(per_doman.get(registrerbar(vard), []))
            # Kammarrätten i Göteborg har haft en adress under goteborg.se.
            # En katalog som heter som en statlig organisation är inte kommunens.
            if f.org and f.org.split(":")[0] in ("kommun", "region") and \
                    STATLIG_TITEL.search(f"{f.titel} {f.pipelinetitel}"):
                f.org = ""
            f.kopplad_via = "domän" if f.org else ""


# ---------------------------------------------------------------------------
# Fingeravtryck
# ---------------------------------------------------------------------------

async def _prova_api(h: Hamtare, bas: str) -> Avtryck | None:
    """Plattformarnas egna API:er, i tur och ordning. Första som svarar vinner."""
    dataset = quote(r"rdfType:http\://www.w3.org/ns/dcat#Dataset")
    s = await h.hamta(f"{bas}/store/search?type=solr&limit=1&query={dataset}", "application/json")
    d = s.json() if s.ok else None
    if isinstance(d, dict) and "results" in d and "resource" in d:
        return Avtryck("EntryScape", f"{bas}/store/", d.get("results"), "API svarar")

    s = await h.hamta(f"{bas}/api/3/action/package_search?rows=0", "application/json")
    d = s.json() if s.ok else None
    if isinstance(d, dict) and d.get("success") is True and "result" in d:
        return Avtryck("CKAN", f"{bas}/api/3/action/", d["result"].get("count"), "API svarar")

    s = await h.hamta(f"{bas}/api/explore/v2.1/catalog/datasets?limit=0", "application/json")
    d = s.json() if s.ok else None
    if isinstance(d, dict) and "total_count" in d:
        return Avtryck("Huwise", f"{bas}/api/explore/v2.1/", d.get("total_count"),
                       "API svarar")

    s = await h.hamta(f"{bas}/api/1/metastore/schemas/dataset/items", "application/json")
    d = s.json() if s.ok else None
    if isinstance(d, list) and (not d or isinstance(d[0], (dict, str))):
        return Avtryck("DKAN", f"{bas}/api/1/metastore/", len(d), "API svarar")

    s = await h.hamta(f"{bas}/api/v1/sv/", "application/json")
    d = s.json() if s.ok else None
    if isinstance(d, list) and d and isinstance(d[0], dict) and "dbid" in d[0]:
        return Avtryck("PxWeb", f"{bas}/api/v1/sv/", len(d), "API svarar")

    s = await h.hamta(f"{bas}/data.json", "application/json")
    d = s.json() if s.ok else None
    if isinstance(d, dict) and isinstance(d.get("dataset"), list):
        typ = "ArcGIS Hub" if "arcgis" in s.text[:20000].lower() else "DCAT-US (data.json)"
        return Avtryck(typ, f"{bas}/data.json", len(d["dataset"]), "API svarar")
    return None


class Fingeravtryckare:
    def __init__(self, h: Hamtare) -> None:
        self.h = h
        self._cache: dict[str, asyncio.Task] = {}

    def av(self, url: str) -> "asyncio.Task[Avtryck]":
        p = urlparse(url)
        bas = f"{p.scheme or 'https'}://{p.hostname}"
        if bas not in self._cache:
            self._cache[bas] = asyncio.ensure_future(self._av(bas))
        return self._cache[bas]

    async def _av(self, bas: str) -> Avtryck:
        api = await _prova_api(self.h, bas)
        if api and vard_ur(bas) in REGISTRERINGSTJANST:
            api.typ = "Dataportalens registreringstjänst"
        if api and vard_ur(bas) == "free.entryscape.com":
            # Gratistjänsten delas av många utgivare; storens antal gäller
            # alla, inte organisationen. Antalet står i federationen.
            api.typ, api.antal = "EntryScape Free", None
        if api:
            return api
        start = await self.h.hamta(bas + "/", "text/html")
        if start.ok:
            for namn, monster in HTML_MARKORER:
                if monster.search(start.text):
                    return Avtryck(namn, bas + "/", None, "omnämnande i HTML")
            return Avtryck("webbsida", bas + "/")
        return Avtryck("okänd", "", None, start.fel or f"HTTP {start.status}")


def kalltyp(url: str) -> str:
    """Plattform ur skördeadressens form — ett indicium, inte ett belägg."""
    u = url.lower()
    if vard_ur(url) in REGISTRERINGSTJANST:
        return "Dataportalens registreringstjänst"
    if "/store/" in u or "entryscape" in u:
        return "EntryScape"
    if "catalog.rdf" in u or "catalog.ttl" in u or "/catalog.xml" in u:
        return "CKAN"
    # opendatasoft.com är Huwise domän från tiden före namnbytet; äldre
    # skördeadresser pekar fortfarande dit.
    if "/api/explore/" in u or "/api/v2/catalog/exports" in u or "opendatasoft" in u:
        return "Huwise"
    if "arcgis" in u or "/api/feed/dcat" in u:
        return "ArcGIS Hub"
    if "csw" in u or "geonetwork" in u:
        return "CSW/GeoNetwork"
    if u.endswith((".rdf", ".ttl", ".xml", ".jsonld", ".json")):
        return "DCAT-fil"
    return ""


def ar_plattformsvard(vard: str) -> bool:
    return vard.split(".")[0] in SUBDOMANER or vard.endswith((
        # .opendatasoft.com: Huwise domän från före namnbytet, som portaler
        # på bolagets egna värdnamn fortfarande ligger under.
        ".entryscape.net", ".entryscape.com", ".huwise.com", ".opendatasoft.com",
        ".opendata.arcgis.com", ".hub.arcgis.com"))


def sidlankar(svar: Svar) -> dict:
    """Vad en webbsida om öppna data länkar vidare till."""
    lankar = re.findall(r'href="([^"#]+)"', svar.text, re.I)
    abs_ = [urljoin(svar.slutlig_url, html.unescape(l)) for l in lankar]
    filer = [u for u in abs_ if urlparse(u).path.lower().endswith(FILANDELSER)]
    vardar = Counter(vard_ur(u) for u in abs_ if u.startswith("http"))
    egen = registrerbar(vard_ur(svar.slutlig_url))
    plattformar = sorted(v for v in vardar if v and (
        "dataportal.se" in v or "entryscape" in v or "huwise" in v
        or "opendatasoft" in v  # Huwise domän från före namnbytet
        # storymaps.arcgis.com och liknande är inga datakataloger
        or v.endswith((".opendata.arcgis.com", ".hub.arcgis.com"))
        or (registrerbar(v) == egen and v.split(".")[0] in SUBDOMANER)))
    return {"filer": len(filer), "plattformar": plattformar}


# ---------------------------------------------------------------------------
# Spindling
# ---------------------------------------------------------------------------

async def _mjuk_404(h: Hamtare, bas: str, cache: dict) -> Svar:
    """En sökväg som inte kan finnas. Svarar den 200 är webbplatsens 404 mjuk."""
    if bas not in cache:
        cache[bas] = asyncio.ensure_future(
            h.hamta(f"{bas}/doa-kartlaggning-finns-inte-{secrets.token_hex(4)}", "text/html"))
    return await cache[bas]


def _ar_mjuk(svar: Svar, baslinje: Svar) -> bool:
    if not baslinje.ok:
        return False
    if svar.slutlig_url.rstrip("/") == baslinje.slutlig_url.rstrip("/"):
        return True
    return bool(titel_ur(svar.text)) and titel_ur(svar.text) == titel_ur(baslinje.text)


async def spindla(o: Organisation, h: Hamtare, fa: Fingeravtryckare,
                  extra_vardar: set[str]) -> list[Fynd]:
    fynd: list[Fynd] = []
    mjuk: dict = {}
    provade: set[str] = set()
    lankade: set[str] = set()

    def notera_lankar(info: dict) -> None:
        # Plattformsvärdar som en sida om öppna data länkar till prövas också —
        # en portal på *.entryscape.net eller *.huwise.com har inget
        # värdnamn under organisationens domän som spindeln kunde gissa.
        lankade.update(v for v in info["plattformar"] if "dataportal.se" not in v)

    async def prova_vard(vard: str, kalla: str) -> None:
        if vard in provade:
            return
        provade.add(vard)
        if not await h.finns(vard, bekrafta=kalla == "federation"):
            # Dataportalen kan rapportera en lyckad skördning trots att källan
            # inte längre går att nå — då ska det synas.
            if kalla == "federation":
                fynd.append(Fynd(o.nyckel, kalla, vard, 0, "", "värden finns inte i DNS"))
            return
        svar = await h.hamta(f"https://{vard}/", "text/html")
        if svar.fel and not svar.status:
            svar = await h.hamta(f"http://{vard}/", "text/html")
        if not svar.status:
            return
        if svar.status >= 400:
            # En federerad källa som svarar med fel är ett fynd i sig; en
            # gissad värd som gör det är det inte.
            if kalla == "federation":
                fynd.append(Fynd(o.nyckel, kalla, vard, svar.status, svar.slutlig_url,
                                 f"svarar med HTTP {svar.status}",
                                 ogiltigt_cert=svar.ogiltigt_cert))
            return
        slutvard = vard_ur(svar.slutlig_url)
        if vard.endswith(".entryscape.net") and slutvard != vard:
            # En gissad instans som inte finns omdirigeras till EntryScapes
            # marknadsföringssida — där står ordet EntryScape, men ingen katalog.
            return
        if registrerbar(slutvard) != registrerbar(vard) and not ar_plattformsvard(slutvard) \
                and registrerbar(slutvard) not in o.domaner:
            # hsan.se leder till socialstyrelsen.se. Det som finns där är
            # Socialstyrelsens, inte nämndens.
            fynd.append(Fynd(o.nyckel, kalla, vard, svar.status, svar.slutlig_url,
                             f"hänvisar till {slutvard}", ogiltigt_cert=svar.ogiltigt_cert))
            return
        huvudwebb = slutvard in {d for d in o.domaner} | {f"www.{d}" for d in o.domaner}
        if huvudwebb and urlparse(svar.slutlig_url).path.strip("/") == "":
            fynd.append(Fynd(o.nyckel, kalla, vard, svar.status, svar.slutlig_url,
                             "hänvisar till startsidan", ogiltigt_cert=svar.ogiltigt_cert))
            return
        if huvudwebb:
            info = sidlankar(svar)
            notera_lankar(info)
            fynd.append(Fynd(o.nyckel, kalla, vard, svar.status, svar.slutlig_url,
                             "webbsida", ogiltigt_cert=svar.ogiltigt_cert,
                             anteckning=_lankanteckning(info)))
            return
        # Källvärden först: free.entryscape.com omdirigerar sin rot till en
        # marknadsföringssida, men dess API svarar.
        av = await fa.av(f"https://{vard}/")
        if av.belagg != "API svarar":
            efter = await fa.av(svar.slutlig_url)
            if efter.belagg == "API svarar" or av.typ in ("okänd", "webbsida"):
                av = efter
        not_ = ""
        if av.typ == "webbsida":
            info = sidlankar(svar)
            notera_lankar(info)
            not_ = _lankanteckning(info)
        fynd.append(Fynd(o.nyckel, kalla, vard, svar.status, svar.slutlig_url, av.typ,
                         av.api, av.antal, av.belagg, svar.ogiltigt_cert, not_))

    async def prova_sokvag(doman: str, sokvag: str) -> None:
        bas = (f"https://www.{doman}" if await h.finns(f"www.{doman}", bekrafta=True)
               else f"https://{doman}")
        svar = await h.hamta(f"{bas}/{sokvag}", "text/html")
        if not svar.ok:
            return
        if _ar_mjuk(svar, await _mjuk_404(h, bas, mjuk)):
            return
        if urlparse(svar.slutlig_url).path.strip("/") == "":
            return
        slutvard = vard_ur(svar.slutlig_url)
        annan_doman = registrerbar(slutvard) not in o.domaner
        if annan_doman and not ar_plattformsvard(slutvard):
            # Hänvisar till en annan organisations webbplats — en nämnd vars
            # adress leder till värdmyndigheten. Det som finns där hör till den.
            return
        if annan_doman or slutvard.split(".")[0] in SUBDOMANER:
            av = await fa.av(svar.slutlig_url)
            fynd.append(Fynd(o.nyckel, "spindel", f"{bas}/{sokvag}", svar.status,
                             svar.slutlig_url, av.typ, av.api, av.antal, av.belagg,
                             svar.ogiltigt_cert))
            return
        if not OPPNA_DATA_RE.search(svar.text):
            return
        info = sidlankar(svar)
        notera_lankar(info)
        fynd.append(Fynd(o.nyckel, "spindel", f"{bas}/{sokvag}", svar.status,
                         svar.slutlig_url, "webbsida", ogiltigt_cert=svar.ogiltigt_cert,
                         anteckning=_lankanteckning(info)))

    uppgifter = []
    for d in ([] if o.vard_hos else o.domaner):
        for p in SUBDOMANER:
            uppgifter.append(prova_vard(f"{p}.{d}", "spindel"))
        uppgifter.append(prova_vard(f"{d.split('.')[0]}.entryscape.net", "spindel"))
        for s in SOKVAGAR:
            uppgifter.append(prova_sokvag(d, s))
    for v in extra_vardar:
        uppgifter.append(prova_vard(v, "federation"))
    await asyncio.gather(*uppgifter)
    await asyncio.gather(*(prova_vard(v, "länk") for v in sorted(lankade)))
    return _unika(fynd)


def _lankanteckning(info: dict) -> str:
    delar = []
    if info["plattformar"]:
        delar.append("länkar till " + ", ".join(info["plattformar"][:5]))
    if info["filer"]:
        delar.append(f"{info['filer']} datafiler")
    return "; ".join(delar)


def _unika(fynd: list[Fynd]) -> list[Fynd]:
    """Flera kandidater landar ofta på samma sida — en rad per slutadress."""
    ut: dict[tuple, Fynd] = {}
    for f in fynd:
        nyckel = (f.org, f.slutlig_url.rstrip("/").lower())
        if nyckel not in ut or (ut[nyckel].kalla != "federation" and f.kalla == "federation"):
            ut[nyckel] = f
    return list(ut.values())


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

REGISTERPLATTFORM = {"EntryScape": "entryscape", "CKAN": "ckan",
                     "Huwise": "huwise", "ArcGIS Hub": "arcgis_hub"}


def registerrader(orgs: list[Organisation], fynd: list[Fynd],
                  fed: list[Federerad]) -> list[register.Katalog]:
    """Det som ska sökas i: kataloger som svarade, inte det som en gång anmälts.

    Egna plattformar kommer ur fynden och kräver att API:et svarade. De delade
    instanserna — EntryScape Free och dataportalens registreringstjänst — och
    levande DCAT-filer kommer ur federationen, och bara när senaste
    skördningen lyckades och katalogen inte var tom.
    """
    per_nyckel = {o.nyckel: o for o in orgs}
    ut: dict[tuple, register.Katalog] = {}

    def lagg_till(o: Organisation, plattform: str, bas: str, kontext: str = "",
                  katalog_url: str = "", antal: int | None = None, belagg: str = "") -> None:
        nyckel = (o.nyckel, plattform, bas, kontext)
        if nyckel in ut:
            ut[nyckel].katalog_url = ut[nyckel].katalog_url or katalog_url
            return
        ut[nyckel] = register.Katalog(
            org_nyckel=o.nyckel, kategori=o.kategori, kod=o.kod, namn=o.namn,
            lan_kod=o.lan_kod, plattform=plattform, bas_url=bas, kontext=kontext,
            katalog_url=katalog_url, antal_dataset=antal, belagg=belagg)

    for f in fynd:
        o = per_nyckel.get(f.org)
        if o and f.typ in REGISTERPLATTFORM and f.belagg == "API svarar":
            p = urlparse(f.api or f.slutlig_url)
            lagg_till(o, REGISTERPLATTFORM[f.typ], f"{p.scheme}://{p.hostname}",
                      antal=f.antal, belagg=f"API svarar ({f.kalla})")

    for f in fed:
        o = per_nyckel.get(f.org)
        if not o or not f.skordas or not f.antal_dataset:
            continue
        vard = vard_ur(f.kalla_url) if f.kalla_url else ""
        kontext = re.search(r"/store/(\d+)/", f.kalla_url or "")
        if not f.kalla_url or vard in REGISTRERINGSTJANST:
            # Förs direkt i dataportalen — den är då organisationens katalog.
            bas = f"https://{vard}" if vard else "https://admin.dataportal.se"
            lagg_till(o, "entryscape", bas, kontext.group(1) if kontext else f.kontext,
                      f.kalla_url, f.antal_dataset, "dataportalens registreringstjänst")
        elif vard == "free.entryscape.com" and kontext:
            lagg_till(o, "entryscape", "https://free.entryscape.com", kontext.group(1),
                      f.kalla_url, f.antal_dataset, "EntryScape Free")
        elif kalltyp(f.kalla_url) == "DCAT-fil":
            lagg_till(o, "dcat_fil", f"{urlparse(f.kalla_url).scheme}://{vard}", "",
                      f.kalla_url, f.antal_dataset, "DCAT-fil som dataportal.se skördar")
        else:
            # Samma värd som en egen plattform: skördeadressen är dess DCAT-källa.
            for k in ut.values():
                if k.org_nyckel == o.nyckel and vard_ur(k.bas_url) == vard:
                    k.katalog_url = k.katalog_url or f.kalla_url
    return list(ut.values())


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

SAMMA_ORGANISATION = (("kommun:0980", "region:I"),)

EGEN_TJANST = {"Dataportalens registreringstjänst", "EntryScape", "EntryScape Free", "CKAN", "Huwise", "DKAN",
               "ArcGIS Hub", "PxWeb", "DCAT-US (data.json)", "GeoNetwork", "DCAT-fil"}


def _md(v) -> str:
    return str(v if v not in (None, "") else "").replace("|", "\\|")


def skriv_rapport(ut: Path, orgs: list[Organisation], fed: list[Federerad],
                  fynd: list[Fynd], anrop: str) -> None:
    per_org: dict[str, list[Fynd]] = defaultdict(list)
    for f in fynd:
        per_org[f.org].append(f)
    fed_per_org: dict[str, list[Federerad]] = defaultdict(list)
    for f in fed:
        if f.org:
            fed_per_org[f.org].append(f)
    # Region Gotland är både kommun och region — en organisation, en tjänst.
    for a, b in SAMMA_ORGANISATION:
        for lista in (per_org, fed_per_org):
            gemensam = lista[a] + lista[b]
            lista[a], lista[b] = gemensam, list(gemensam)
    namn = {o.nyckel: o for o in orgs}

    def tjanster(nyckel: str) -> list[Fynd]:
        return [f for f in per_org.get(nyckel, []) if f.typ in EGEN_TJANST]

    def huvudtyp(nyckel: str) -> str:
        typer = {f.typ for f in tjanster(nyckel)}
        federerade = fed_per_org.get(nyckel, [])
        # Kataloger som förs direkt i dataportalen har ingen värd att spindla,
        # så registreringstjänsten syns bara i federationen.
        med_data = [f for f in federerade if f.skordas and (f.antal_dataset or 0) > 0]
        typer |= {"Dataportalens registreringstjänst" for f in med_data
                  if not f.kalla_url
                  or kalltyp(f.kalla_url) == "Dataportalens registreringstjänst"}
        if typer:
            return " + ".join(sorted(typer))
        # Går källvärden inte att nå återstår skördeadressens form som indicium.
        indicier = {kalltyp(f.kalla_url) for f in med_data if kalltyp(f.kalla_url)}
        if indicier:
            return " + ".join(sorted(f"{t} (enligt skördeadressen)" for t in indicier))
        if med_data:
            return "federerad, plattform ej identifierad"
        if any(f.skordas for f in federerade):
            return "anmäld till dataportal.se, katalogen är tom"
        if federerade:
            return "anmäld till dataportal.se, skördningen misslyckas"
        if any(f.typ == "webbsida" for f in per_org.get(nyckel, [])):
            return "webbsida om öppna data"
        if namn[nyckel].vard_hos:
            return "webbplats hos värdmyndighet"
        return "inget funnet"

    r: list[str] = []
    r.append(f"# Datadelningstjänster hos kommuner, regioner och myndigheter\n")
    r.append(f"Kartläggning {date.today().isoformat()}. "
             f"{len(orgs)} organisationer, {len(fed)} kataloger i dataportal.se, "
             f"{anrop}.\n")
    r.append("## Metod\n")
    r.append(
        "- **Federationen:** dataportal.se:s skördepipelines. Källadressen är den "
        "URL dataportalen hämtar DCAT-AP från. Kataloger utan pipeline förs direkt "
        "i dataportalens egen katalogtjänst.\n"
        f"- **Spindling:** värdnamnen {', '.join(p + '.' for p in SUBDOMANER)} "
        f"och `<namn>.entryscape.net`, sökvägarna "
        f"{', '.join('/' + s for s in SOKVAGAR)} på huvudwebben, samt varje "
        "värd som dataportal.se skördar från för organisationen.\n"
        "- **Fingeravtryck:** plattformens eget API anropas. *API svarar* är ett "
        "belägg; *omnämnande i HTML* är ett indicium.\n"
        "- **(enligt skördeadressen)** betyder att källvärden inte gick att nå och "
        "att plattformen bara är utläst ur adressens form.\n"
        "- **Webbsida** betyder en sida om öppna data utan identifierad "
        "katalogplattform. Sidor på huvudwebben räknas bara om de nämner öppna data "
        "och inte är en mjuk 404.\n")

    r.append("## Översikt\n")
    kat = ("kommun", "region", "myndighet")
    typer = Counter()
    for o in orgs:
        for t in huvudtyp(o.nyckel).split(" + "):
            typer[(t, o.kategori)] += 1
    rader_typ = sorted({t for t, _ in typer}, key=lambda t: -sum(typer[(t, k)] for k in kat))
    r.append("| Tjänst | Kommuner | Regioner | Myndigheter |\n|---|---:|---:|---:|")
    for t in rader_typ:
        r.append(f"| {t} | " + " | ".join(str(typer[(t, k)]) for k in kat) + " |")
    r.append(f"| **Antal organisationer** | " + " | ".join(
        str(sum(1 for o in orgs if o.kategori == k)) for k in kat) + " |\n")
    r.append("En organisation med flera tjänster räknas en gång per tjänst.\n")

    r.append("## Identifierade katalogtjänster\n")
    for typ in sorted(EGEN_TJANST):
        rader = [(namn[f.org], f) for f in fynd if f.typ == typ and f.org in namn]
        if not rader:
            continue
        r.append(f"### {typ}\n")
        r.append("| Organisation | Kategori | Adress | API | Dataset | Belägg | Hittad via |")
        r.append("|---|---|---|---|---:|---|---|")
        for o, f in sorted(rader, key=lambda x: (x[0].kategori, x[0].namn)):
            cert = " (ogiltigt cert)" if f.ogiltigt_cert else ""
            adress, antal = f.slutlig_url, f.antal
            if f.typ == "EntryScape Free":
                # Gratistjänstens rot är en marknadsföringssida och dess antal
                # gäller alla kunder. Organisationens katalog står i federationen.
                egen = [x for x in fed_per_org.get(o.nyckel, [])
                        if "free.entryscape.com" in x.kalla_url]
                if egen:
                    adress, antal = egen[0].kalla_url, egen[0].antal_dataset
            r.append(f"| {_md(o.namn)} | {o.kategori} | {_md(adress)}{cert} | "
                     f"{_md(f.api)} | {_md(antal)} | {_md(f.belagg)} | {f.kalla} |")
        r.append("")

    r.append("## Federation till dataportal.se\n")
    lyckas = sum(1 for f in fed if f.org in namn and f.skordas)
    r.append(f"{sum(1 for f in fed if f.org in namn)} kataloger hos organisationerna i "
             f"underlaget, varav {lyckas} skördades utan fel senast. *Status* är utfallet "
             "av dataportalens senaste skördning; en katalog som misslyckas pekar ofta på "
             "en adress som slutat svara.\n")
    r.append("| Organisation | Kategori | Katalog | Skördas från | Plattform enligt adress "
             "| Status | Senast | Dataset | Valideringsfel | Kopplad via |")
    r.append("|---|---|---|---|---|---|---|---:|---:|---|")
    for f in sorted((f for f in fed if f.org in namn),
                    key=lambda f: (namn[f.org].kategori, namn[f.org].namn)):
        o = namn[f.org]
        kalla = f.kalla_url or "*förs direkt i dataportalen*"
        r.append(f"| {_md(o.namn)} | {o.kategori} | {_md(f.titel)} | {_md(kalla)} | "
                 f"{_md(kalltyp(f.kalla_url) or ('Dataportalens registreringstjänst' if not f.kalla_url else ''))} | "
                 f"{_md(f.status or 'aldrig skördad')} | {_md(f.senast)} | "
                 f"{_md(f.antal_dataset)} | {_md(f.valideringsfel)} | {f.kopplad_via} |")
    r.append("")

    webbsidor = [(namn[f.org], f) for f in fynd if f.typ == "webbsida" and f.org in namn
                 and not tjanster(f.org)]
    if webbsidor:
        r.append("## Webbsidor om öppna data utan identifierad katalog\n")
        r.append("| Organisation | Kategori | Sida | Innehåll |\n|---|---|---|---|")
        for o, f in sorted(webbsidor, key=lambda x: (x[0].kategori, x[0].namn)):
            r.append(f"| {_md(o.namn)} | {o.kategori} | {_md(f.slutlig_url)} | {_md(f.anteckning)} |")
        r.append("")

    omatchade = [f for f in fed if not f.org]
    r.append("## Kataloger i dataportal.se utanför listan\n")
    r.append(f"{len(omatchade)} kataloger skördas från utgivare som inte är kommun, "
             "region eller myndighet i underlaget — universitet, bolag, föreningar — "
             "eller som inte gick att knyta entydigt.\n")
    r.append("| Katalog | Utgivare | Skördas från | Status | Dataset |\n|---|---|---|---|---:|")
    for f in sorted(omatchade, key=lambda f: -(f.antal_dataset or 0)):
        r.append(f"| {_md(f.titel)} | {_md(f.utgivare_uri)} | {_md(f.kalla_url)} | "
                 f"{_md(f.status or 'aldrig skördad')} | {_md(f.antal_dataset)} |")
    r.append("")

    hos = [o for o in orgs if o.vard_hos]
    if hos:
        r.append("## Webbplats hos en värdmyndighet\n")
        r.append("Adressen i underlaget är en sida på en annan myndighets webbplats. Det som "
                 "finns på domänen redovisas hos värdmyndigheten; här kan bara federationen "
                 "ge träff.\n")
        for o in sorted(hos, key=lambda o: o.namn):
            r.append(f"- {o.namn} — hos {o.vard_hos}: {huvudtyp(o.nyckel)}")
        r.append("")

    utan = [o for o in orgs if not o.domaner]
    if utan:
        r.append("## Organisationer utan webbadress i underlaget\n")
        r.append("Spindlingen kan inte pröva dem; bara federationen kan ge träff.\n")
        for o in sorted(utan, key=lambda o: o.namn):
            r.append(f"- {o.namn} ({o.kategori}{', ' + o.underkategori if o.underkategori else ''})"
                     f" — {huvudtyp(o.nyckel)}")
        r.append("")

    r.append("## Samtliga organisationer\n")
    r.append("| Organisation | Kategori | Kod | Domäner | Resultat |\n|---|---|---|---|---|")
    for o in sorted(orgs, key=lambda o: (kat.index(o.kategori), o.namn)):
        r.append(f"| {_md(o.namn)} | {o.kategori} | {_md(o.kod)} | "
                 f"{_md(' '.join(o.domaner))} | {_md(huvudtyp(o.nyckel))} |")
    (ut / "rapport.md").write_text("\n".join(r) + "\n", encoding="utf-8")


def skriv_csv(ut: Path, orgs: list[Organisation], fynd: list[Fynd],
              fed: list[Federerad]) -> None:
    namn = {o.nyckel: o for o in orgs}
    with (ut / "fynd.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kategori", "kod", "organisation", "kalla", "kandidat", "status",
                    "slutlig_url", "typ", "api", "antal_dataset", "belagg",
                    "ogiltigt_cert", "anteckning"])
        for x in fynd:
            o = namn.get(x.org)
            w.writerow([o.kategori if o else "", o.kod if o else "", o.namn if o else x.org,
                        x.kalla, x.kandidat, x.status, x.slutlig_url, x.typ, x.api,
                        "" if x.antal is None else x.antal, x.belagg,
                        "ja" if x.ogiltigt_cert else "", x.anteckning])
    with (ut / "federation.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kontext", "katalog", "organisation", "kategori", "utgivare_uri",
                    "hemsida", "kalla_url", "plattform_enligt_adress", "status",
                    "senast_skordad", "antal_dataset", "valideringsfel", "kopplad_via"])
        for x in fed:
            o = namn.get(x.org)
            w.writerow([x.kontext, x.titel, o.namn if o else "", o.kategori if o else "",
                        x.utgivare_uri, x.hemsida, x.kalla_url, kalltyp(x.kalla_url),
                        x.status, x.senast,
                        "" if x.antal_dataset is None else x.antal_dataset,
                        "" if x.valideringsfel is None else x.valideringsfel,
                        x.kopplad_via])
    with (ut / "organisationer.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kategori", "underkategori", "kod", "namn", "webbadresser", "domaner"])
        for o in orgs:
            w.writerow([o.kategori, o.underkategori, o.kod, o.namn,
                        " ".join(o.webbadresser), " ".join(o.domaner)])


# ---------------------------------------------------------------------------
# Körning
# ---------------------------------------------------------------------------

async def _kor(args: argparse.Namespace) -> int:
    h = Hamtare()
    try:
        orgs: list[Organisation] = []
        if args.bara in (None, "kommun", "region"):
            orgs += [o for o in await las_kommuner_och_regioner(h)
                     if args.bara in (None, o.kategori)]
        if args.bara in (None, "myndighet"):
            orgs += (las_myndigheter(Path(args.myndigheter)) if args.myndigheter
                     else await las_myndigheter_oppet())
        satt_domaner(orgs)
        print(f"  {len(orgs)} organisationer, {sum(1 for o in orgs if not o.domaner)} utan webbadress")

        fed = await las_federationen(h)
        matcha_federationen(fed, orgs)
        print(f"  {len(fed)} kataloger i dataportal.se, "
              f"{sum(1 for f in fed if f.org)} knutna till en organisation")

        extra: dict[str, set[str]] = defaultdict(set)
        for f in fed:
            for u in (f.kalla_url, f.hemsida):
                v = vard_ur(u) if u else ""
                if f.org and v and v not in REGISTRERINGSTJANST:
                    extra[f.org].add(v)

        fa = Fingeravtryckare(h)
        klara = 0

        async def en(o: Organisation) -> list[Fynd]:
            nonlocal klara
            try:
                return await spindla(o, h, fa, extra.get(o.nyckel, set()))
            finally:
                klara += 1
                if klara % 25 == 0 or klara == len(orgs):
                    print(f"  {klara}/{len(orgs)} organisationer, {h.antal_anrop} anrop", flush=True)

        fynd = [f for lista in await asyncio.gather(*(en(o) for o in orgs)) for f in lista]

        if not args.utan_register:
            fore = {(k.org_nyckel, k.plattform, k.bas_url, k.kontext): k.antal_dataset
                    for k in await register.las()}
            nya_rader = registerrader(orgs, fynd, fed)
            antal = await register.spara(nya_rader)
            print(f"  {antal} kataloger skrivna till registret")
            # Organisationslistan är fullständig för de kategorier som körts;
            # med en egen myndighetslista är den det inte för myndigheterna.
            for kategori in {o.kategori for o in orgs}:
                if kategori == "myndighet" and args.myndigheter:
                    continue
                borta = await register.ta_bort_utom(
                    kategori, {o.nyckel for o in orgs if o.kategori == kategori})
                if borta:
                    print(f"  {borta} organisationer som inte längre finns togs bort ur registret ({kategori})")
            # Journalen bär förändringen, inte bara antalet — det är den som
            # visar hur ofta kartläggningen behöver göras.
            efter = {(k.org_nyckel, k.plattform, k.bas_url, k.kontext): k.antal_dataset for k in nya_rader}
            tillkomna = len(efter.keys() - fore.keys())
            ej_bekraftade = len(fore.keys() - efter.keys())
            andrade = sum(1 for n in efter.keys() & fore.keys() if efter[n] != fore[n])
            kommentar = (f"{tillkomna} nya kataloger, {ej_bekraftade} ej bekräftade, "
                         f"{andrade} med ändrat antal datamängder")
            await synkstatus.notera_synk("datakatalog", antal, kommentar)
            print(f"  {kommentar}")

        ut = Path(args.ut)
        ut.mkdir(parents=True, exist_ok=True)
        skriv_csv(ut, orgs, fynd, fed)
        skriv_rapport(ut, orgs, fed, fynd,
                      f"{h.antal_anrop} HTTP-anrop och {h.antal_dns} DNS-uppslag")
        print(f"  Klart: {ut / 'rapport.md'}")
        return 0
    finally:
        await h.stang()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--myndigheter",
                   help="Egen myndighetslista som CSV (namn, kategori, orgnr, webbadresser, alias) "
                        "i stället för SCB:s register")
    p.add_argument("--bara", choices=("kommun", "region", "myndighet"),
                   help="Kartlägg bara en kategori")
    p.add_argument("--utan-register", action="store_true",
                   help="Skriv bara rapporten, inte registret som sökverktygen läser")
    p.add_argument("--ut", default=str(Path(__file__).resolve().parents[1] / ".cache" / "datadelning"),
                   help="Katalog för rapport och CSV-filer")
    args = p.parse_args()
    konfig.las_env()
    if not args.utan_register:
        # Synkron och före event-loopen, som i MCP-servrarnas uppstart.
        db.initiera_schema()
    return asyncio.run(_kor(args))


if __name__ == "__main__":
    sys.exit(main())
