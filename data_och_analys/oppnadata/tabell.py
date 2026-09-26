# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Gör en hämtningsväg till en tabell — och beskriver, filtrerar och aggregerar den.

Öppna data är sällan små. En kommuns leverantörsreskontra är tiotusentals
rader per månad, och ett urval från flera kommuner får aldrig plats i en
modells kontext. Aggregeringen görs därför här, på serversidan, och bara
resultatet skickas vidare.

Filer hämtas via mellanlagret (`fil_mellanlager_original`) och tolkas vid
varje användning. En distribution med flera filer — rekommendation 4 i
DCAT-AP-SE, ofta en fil per månad — slås ihop till en tabell, och filerna
kan väljas på sin titel så att ett kvartal blir tre filer i stället för
tolv.

Två svenska drag hanteras uttryckligen: decimalkomma med mellanslag som
tusentalsavgränsare (`2 377,90`), och Excelblad där tabellen börjar under
några rader rubrik och förklaring.

Ingångspunkter:
    Tabell
    hamta_tabell(vag, filurval=None, blad=None, max_rader=...) -> Tabell
    beskriv(tabell) -> dict
    filtrera(df, villkor) -> DataFrame
    aggregera(tabell, gruppera, matt, funktion, villkor, topp) -> dict
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import warnings
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx
import pandas as pd

from data_och_analys.infra import mellanlager
from data_och_analys.katalog_klienter import arcgis_features, ckan, entryscape, huwise, wfs
from data_och_analys.katalog_klienter import ogc_api_features as oafeat
from data_och_analys.oppnadata.hamtningsvag import Hamtningsvag

logger = logging.getLogger(__name__)

USER_AGENT = "svensk-data-och-analys/Oppnadata (DCAT-AP-SE; +https://github.com/)"
MAX_FILSTORLEK = 300 * 1024 * 1024
# Rowstore ger hundra rader per anrop. Över taket är filerna rätt väg;
# de finns nästan alltid som en annan distribution av samma datamängd.
ROWSTORE_MAX_RADER = 20_000
TABELLFORMAT = ("csv", "xlsx", "xls", "ods", "json", "geojson", "zip", "gpkg", "kml", "shp", "txt")


@dataclass
class Tabell:
    df: pd.DataFrame
    kallor: list[str] = field(default_factory=list)
    varningar: list[str] = field(default_factory=list)
    blad: list[str] = field(default_factory=list, metadata={"doc": "Blad eller filer att välja mellan"})


# ---------------------------------------------------------------------------
# Hämtning
# ---------------------------------------------------------------------------

async def hamta_fil(url: str) -> tuple[bytes, str]:
    """Filens innehåll och innehållstyp — ur mellanlagret om den finns där."""
    lagrad = await mellanlager.las_original(url)
    if lagrad:
        return lagrad
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT}) as klient:
        async with klient.stream("GET", url) as svar:
            if svar.status_code >= 400:
                raise ValueError(f"{url} svarade {svar.status_code}")
            delar, storlek = [], 0
            async for bit in svar.aiter_bytes():
                storlek += len(bit)
                if storlek > MAX_FILSTORLEK:
                    raise ValueError(f"{url} är större än {MAX_FILSTORLEK // 2**20} MB")
                delar.append(bit)
            innehallstyp = svar.headers.get("content-type", "")
    innehall = b"".join(delar)
    await mellanlager.skriv_original(url, innehall, innehallstyp)
    return innehall, innehallstyp


# ---------------------------------------------------------------------------
# Tolkning
# ---------------------------------------------------------------------------

def _avkoda(innehall: bytes) -> str:
    """UTF-8 är vanligast. Exporter ur ekonomisystem är ofta UTF-16 — Göteborgs
    leverantörsreskontra är det — och äldre kommunala filer Windows-1252.
    En BOM avgör; utan den röjer nollbytena UTF-16."""
    if innehall[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return innehall.decode("utf-16")
    prov = innehall[:4000]
    if prov and prov.count(b"\x00") > len(prov) // 4:
        return innehall.decode("utf-16-le" if prov[1:2] == b"\x00" else "utf-16-be",
                               errors="replace")
    for kodning in ("utf-8-sig", "cp1252"):
        try:
            return innehall.decode(kodning)
        except UnicodeDecodeError:
            continue
    return innehall.decode("latin-1")


def _stada_kolumner(df: pd.DataFrame) -> pd.DataFrame:
    """BOM, citattecken och blanktecken i kolumnnamn gör kolumnerna omöjliga att
    skriva. Tomma namn får sitt ordningsnummer."""
    namn = []
    for i, k in enumerate(df.columns):
        k = str(k).replace("﻿", "").strip().strip('"').strip()
        namn.append(k if k and not k.startswith("Unnamed") else f"kolumn_{i + 1}")
    df.columns = namn
    return df


def _tolka_csv(text: str) -> tuple[pd.DataFrame, int]:
    """Tabellen och antalet rader som inte gick att tolka.

    Felaktiga rader hoppas över men räknas — ett tyst bortfall i en
    summering syns annars inte förrän totalen jämförs med källan.
    """
    prov = text[:20000]
    try:
        avgransare = csv.Sniffer().sniff(prov, delimiters=";,\t|").delimiter
    except csv.Error:
        avgransare = ";" if prov.count(";") > prov.count(",") else ","
    with warnings.catch_warnings(record=True) as fangade:
        warnings.simplefilter("always")
        df = pd.read_csv(io.StringIO(text), sep=avgransare, dtype=str,
                         keep_default_na=False, on_bad_lines="warn")
    overhoppade = sum(str(w.message).count("Skipping line") for w in fangade)
    if _saknar_rubrikrad(list(df.columns)):
        df = pd.read_csv(io.StringIO(text), sep=avgransare, dtype=str, keep_default_na=False,
                         header=None, on_bad_lines="skip")
        df.columns = [f"kolumn_{i + 1}" for i in range(len(df.columns))]
        df.attrs["utan_rubrikrad"] = True
    return df, overhoppade


def _saknar_rubrikrad(namn: list[str]) -> bool:
    """Ser "kolumnnamnen" ut som data — tal, datum, GUID — saknar filen
    rubrikrad, och första dataraden har blivit rubrik. Eskilstunas
    badvattentemperaturer är sådana."""
    if len(namn) < 2:
        return False
    data = re.compile(r"^-?[\d\s.,:+-]+$|^[0-9a-fA-F-]{32,36}$|^\d{4}-\d{2}-\d{2}")
    return sum(bool(data.match(str(n).strip())) for n in namn) / len(namn) >= 0.5


def _rubrikrad(rader: pd.DataFrame) -> int:
    """Första raden som är nästan lika bred som tabellen — raderna ovanför är
    rubrik och förklaring, inte kolumnnamn."""
    fyllda = rader.head(30).notna().sum(axis=1)
    bredast = fyllda.max() if len(fyllda) else 0
    for i, n in enumerate(fyllda):
        if bredast and n >= max(2, 0.6 * bredast):
            return i
    return 0


def _tolka_kalkylark(innehall: bytes, format_: str, blad: str | None) -> tuple[pd.DataFrame, list[str]]:
    motor = {"xls": "xlrd", "ods": "odf"}.get(format_, "openpyxl")
    alla = pd.read_excel(io.BytesIO(innehall), sheet_name=None, header=None, dtype=str, engine=motor)
    namn = list(alla)
    valt = blad if blad in alla else namn[0]
    rader = alla[valt].dropna(how="all")
    i = _rubrikrad(rader)
    df = rader.iloc[i + 1:].copy()
    df.columns = rader.iloc[i].tolist()
    return df.dropna(how="all").fillna(""), namn


def _tolka_json(innehall: bytes) -> pd.DataFrame:
    d = json.loads(_avkoda(innehall))
    if isinstance(d, dict) and d.get("type") == "FeatureCollection":
        rader = []
        for f in d.get("features", []):
            rad = dict(f.get("properties") or {})
            geom = f.get("geometry") or {}
            rad["geometrityp"] = geom.get("type", "")
            if geom.get("type") == "Point":
                rad["lon"], rad["lat"] = (geom.get("coordinates") or [None, None])[:2]
            rader.append(rad)
        return pd.DataFrame(rader)
    if isinstance(d, dict):
        # Poster ligger ofta under en nyckel: results, data, records, items, value.
        listor = [v for v in d.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        d = max(listor, key=len) if listor else [d]
    if isinstance(d, list):
        return pd.json_normalize(d, max_level=1)
    raise ValueError("JSON-filen innehåller ingen tabell")


def _tolka_geofil(innehall: bytes) -> pd.DataFrame:
    import geopandas as gpd
    gdf = gpd.read_file(io.BytesIO(innehall))
    if gdf.crs is not None:
        gdf = gdf.to_crs(4326)
    punkt = gdf.geometry.representative_point()
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    df["geometrityp"] = gdf.geometry.geom_type
    df["lon"], df["lat"] = punkt.x.round(6), punkt.y.round(6)
    return df


def _format_ur(innehall: bytes, innehallstyp: str, hint: str, url: str) -> str:
    if hint in TABELLFORMAT:
        return hint
    t = innehallstyp.lower()
    for nyckel, fmt in (("csv", "csv"), ("spreadsheetml", "xlsx"), ("ms-excel", "xls"),
                        ("opendocument.spreadsheet", "ods"), ("geo+json", "geojson"),
                        ("json", "json"), ("zip", "zip"), ("geopackage", "gpkg")):
        if nyckel in t:
            return fmt
    m = re.search(r"\.(csv|xlsx|xls|ods|json|geojson|zip|gpkg|kml)(?:$|\?)", url.lower())
    if m:
        return m.group(1)
    if innehall[:4] == b"PK\x03\x04":
        return "zip"
    if innehall.lstrip()[:1] in (b"{", b"["):
        return "json"
    if b"<html" in innehall[:500].lower():
        raise ValueError("Adressen gav en webbsida, inte en datafil")
    return "csv"


def tolka(innehall: bytes, innehallstyp: str = "", format_: str = "", url: str = "",
          blad: str | None = None) -> tuple[pd.DataFrame, list[str], int]:
    """En tabell ur filinnehåll, namnen på blad eller filer att välja mellan,
    och antalet rader som inte gick att tolka."""
    fmt = _format_ur(innehall, innehallstyp, format_, url)
    if fmt in ("csv", "txt"):
        df, overhoppade = _tolka_csv(_avkoda(innehall))
        return df, [], overhoppade
    if fmt in ("xlsx", "xls", "ods"):
        return (*_tolka_kalkylark(innehall, fmt, blad), 0)
    if fmt in ("json", "geojson"):
        return _tolka_json(innehall), [], 0
    if fmt in ("gpkg", "kml"):
        return _tolka_geofil(innehall), [], 0
    if fmt in ("zip", "shp"):
        with zipfile.ZipFile(io.BytesIO(innehall)) as z:
            namn = [n for n in z.namelist() if not n.endswith("/")]
            if any(n.lower().endswith(".shp") for n in namn):
                return _tolka_geofil(innehall), [], 0
            tabeller = [n for n in namn if re.search(r"\.(csv|xlsx|xls|ods|json|geojson|txt)$", n, re.I)]
            if not tabeller:
                raise ValueError(f"ZIP-filen innehåller ingen tabell: {', '.join(namn[:10])}")
            valt = blad if blad in tabeller else tabeller[0]
            df, _, overhoppade = tolka(z.read(valt), "", valt.rsplit(".", 1)[-1].lower(), valt)
            return df, tabeller, overhoppade
    if fmt == "parquet":
        raise ValueError("Parquet kräver pyarrow, som inte är installerat")
    raise ValueError(f"Formatet {fmt} tolkas inte som tabell")


# ---------------------------------------------------------------------------
# Hämtningsväg till tabell
# ---------------------------------------------------------------------------

async def hamta_tabell(vag: Hamtningsvag, filurval: str | None = None, blad: str | None = None,
                       max_rader: int = ROWSTORE_MAX_RADER) -> Tabell:
    """Tabellen bakom en hämtningsväg.

    `filurval` är ett reguljärt uttryck mot filernas titlar och adresser
    — `2026-0[1-3]` ger första kvartalet ur en distribution med en fil per
    månad. Flera filer slås ihop; kolumnen `_fil` säger var raden kom ifrån.
    """
    if vag.typ in ("fil", "okand"):
        filer = vag.filer or [type("F", (), {"url": vag.url, "titel": ""})()]
        if filurval:
            m = re.compile(filurval, re.I)
            filer = [f for f in filer if m.search(f.titel or "") or m.search(f.url)]
            if not filer:
                raise ValueError(f"Ingen fil matchar {filurval!r}. Filerna: "
                                 + "; ".join((f.titel or f.url) for f in vag.filer[:20]))
        delar, varningar, alla_blad, overhoppade = [], [], [], 0
        for f in filer:
            innehall, typ = await hamta_fil(f.url)
            df, blad_, n = tolka(innehall, typ, vag.format, f.url, blad)
            overhoppade += n
            if df.attrs.get("utan_rubrikrad") and not any("rubrikrad" in v for v in varningar):
                varningar.append("Filen saknar rubrikrad; kolumnerna har fått nummer (kolumn_1 …). "
                                 "Se datamängdens beskrivning för vad de innehåller.")
            df = _stada_kolumner(df)
            if len(filer) > 1:
                df["_fil"] = f.titel or f.url.rsplit("/", 1)[-1]
            delar.append(df)
            alla_blad = alla_blad or blad_
        df = pd.concat(delar, ignore_index=True, sort=False).fillna("") if len(delar) > 1 else delar[0]
        if len(delar) > 1 and len({tuple(d.columns) for d in delar}) > 1:
            varningar.append("Filerna har olika kolumner; saknade värden är tomma.")
        if overhoppade:
            varningar.append(f"{overhoppade} rader i källfilerna gick inte att tolka och ingår inte.")
        return Tabell(df, [f.url for f in filer], varningar, alla_blad)

    if vag.typ == "rowstore":
        info = await entryscape.rowstore_info(vag.url)
        rader, offset = [], 0
        while offset < min(info["antal_rader"] or 0, max_rader):
            svar = await entryscape.rowstore_rader(vag.url, limit=100, offset=offset)
            if not svar["rader"]:
                break
            rader += svar["rader"]
            offset += len(svar["rader"])
        varningar = []
        if (info["antal_rader"] or 0) > max_rader:
            varningar.append(
                f"Rowstore-tabellen har {info['antal_rader']} rader; bara de första {max_rader} "
                "hämtades. Välj datamängdens fildistribution för hela tabellen.")
        return Tabell(_stada_kolumner(pd.DataFrame(rader)), [vag.url], varningar)

    if vag.typ == "huwise":
        m = re.search(r"(https?://[^/]+).*?/(?:explore/dataset|catalog/datasets)/([^/?#]+)", vag.url)
        if not m:
            raise ValueError(f"Går inte att läsa ut Huwise-datamängden ur {vag.url}")
        innehall = await huwise.exportera(m.group(1), m.group(2), format="csv")
        df, overhoppade = _tolka_csv(_avkoda(innehall))
        varningar = [f"{overhoppade} rader gick inte att tolka."] if overhoppade else []
        return Tabell(_stada_kolumner(df), [vag.url], varningar)

    if vag.typ == "ckan":
        return await _ckan_tabell(vag, max_rader)
    if vag.typ in ("wfs", "ogc_api_features", "arcgis_rest"):
        return await _geotabell(vag, max_rader)

    if vag.typ in ("pxweb", "kolada", "researchdata"):
        # Suiten har egna servrar för dem, med uttag som respekterar källans
        # celltak och dimensioner. Att läsa dem som platt tabell här vore sämre.
        from data_och_analys.oppnadata.hamtningsvag import TYPER
        raise ValueError(f"{TYPER[vag.typ][1]}. Adress: {vag.url}")
    raise ValueError(f"Hämtningsvägen {vag.typ} läses inte som tabell här. {vag.forklaring}")


def _avkortad(antal: int | None, max_rader: int, forslag: str) -> list[str]:
    if antal is not None and antal > max_rader:
        return [f"Källan har {antal} rader; de första {max_rader} hämtades. {forslag}"]
    return []


async def _ckan_tabell(vag: Hamtningsvag, max_rader: int) -> Tabell:
    """Datastore-tabellen om resursen har en, annars resursens fil."""
    m = (re.search(r"^(https?://.+?)/api/3/action/datastore_search\?resource_id=([\w-]+)", vag.url)
         or re.search(r"^(https?://.+?)/dataset/[^/]+/resource/([\w-]+)", vag.url))
    if not m:
        raise ValueError(f"Går inte att läsa ut CKAN-resursen ur {vag.url}")
    bas, resurs_id = m.group(1), m.group(2)
    resurs = await ckan.hamta_resurs(bas, resurs_id)
    if not resurs.get("datastore_active"):
        fil = Hamtningsvag(typ="fil", hamtbar=True, url=resurs["url"],
                           format=(resurs.get("format") or "").lower())
        return await hamta_tabell(fil)
    rader, offset, antal = [], 0, None
    while offset < max_rader:
        svar = await ckan.datastore_rader(bas, resurs_id, limit=1000, offset=offset)
        antal = svar["antal_totalt"]
        rader += svar["rader"]
        offset += len(svar["rader"])
        if not svar["rader"] or offset >= (antal or 0):
            break
    return Tabell(_stada_kolumner(pd.DataFrame(rader)), [vag.url],
                  _avkortad(antal, max_rader, "Filtrera i källan eller välj resursens fil."))


async def _arcgis_lager(url: str) -> list[tuple[int, str]]:
    tjanst = re.sub(r"/\d+/?$", "", url.split("?")[0])
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as klient:
            d = (await klient.get(tjanst, params={"f": "json"})).json()
    except (httpx.HTTPError, ValueError):
        return []
    return [(l["id"], l.get("name", "")) for l in d.get("layers", [])]


def _features_till_df(features: list[dict]) -> pd.DataFrame:
    """Egenskaper plus geometrityp och en representativ punkt i WGS84 —
    geometrin själv är för tung för en tabell som ska beskrivas och aggregeras."""
    if not features:
        return pd.DataFrame()
    import geopandas as gpd
    gdf = gpd.GeoDataFrame.from_features(features, crs=4326)
    punkt = gdf.geometry.representative_point()
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    df["geometrityp"] = gdf.geometry.geom_type
    df["lon"], df["lat"] = punkt.x.round(6), punkt.y.round(6)
    return df


async def _geotabell(vag: Hamtningsvag, max_rader: int) -> Tabell:
    from urllib.parse import parse_qs, urlparse
    features: list[dict] = []
    antal = None
    if vag.typ == "wfs":
        fraga = {k.lower(): v[0] for k, v in parse_qs(urlparse(vag.url).query).items()}
        lager = fraga.get("typenames") or fraga.get("typename") or fraga.get("layers")
        if not lager:
            alla = await wfs.lista_lager(vag.url)
            if len(alla) != 1:
                raise ValueError("WFS-tjänsten har flera lager; ange ett av: "
                                 + ", ".join(l.get("namn") or l.get("name", "") for l in alla[:30]))
            lager = alla[0].get("namn") or alla[0].get("name")
        start = 0
        while start < max_rader:
            fc = await wfs.hamta_features(vag.url, lager, count=1000, start_index=start)
            sida = fc.get("features", [])
            features += sida
            antal = fc.get("numberMatched") or antal
            if len(sida) < 1000:
                break
            start += len(sida)
    elif vag.typ == "ogc_api_features":
        m = re.search(r"/collections/([^/?#]+)", vag.url)
        if not m:
            raise ValueError(f"Adressen anger ingen collection: {vag.url}")
        offset = 0
        while offset < max_rader:
            fc = await oafeat.hamta_items(vag.url, m.group(1), limit=1000, offset=offset)
            sida = fc.get("features", [])
            features += sida
            antal = fc.get("numberMatched") or antal
            if len(sida) < 1000:
                break
            offset += len(sida)
    else:
        m = re.search(r"/rest/services/(.+?)/(MapServer|FeatureServer)/(\d+)", vag.url, re.I)
        if not m:
            raise ValueError(f"Adressen pekar inte ut ett ArcGIS-lager: {vag.url}")
        offset = 0
        while offset < max_rader:
            try:
                fc = await arcgis_features.hamta_features(
                    vag.url, m.group(1), int(m.group(3)), offset=offset, count=1000,
                    out_sr=4326, server_typ=m.group(2))
            except RuntimeError as e:
                # Katalogposter pekar ibland på ett lager som tagits bort ur
                # tjänsten. Då ska svaret säga det och visa vad som finns.
                lager = await _arcgis_lager(vag.url)
                if lager and int(m.group(3)) not in {l[0] for l in lager}:
                    raise ValueError(
                        f"Lager {m.group(3)} finns inte i tjänsten — katalogposten pekar på ett "
                        f"lager som tagits bort. Lager som finns: "
                        + "; ".join(f"{i} {n}" for i, n in lager[:40])) from e
                raise
            sida = fc.get("features", [])
            features += sida
            if len(sida) < 1000 and not fc.get("exceededTransferLimit"):
                break
            offset += len(sida)
    df = _stada_kolumner(_features_till_df(features[:max_rader]))
    avkortad = (len(features) >= max_rader) or (antal is not None and antal > max_rader)
    return Tabell(df, [vag.url], _avkortad(antal or (max_rader + 1 if avkortad else None),
                                           max_rader, "Avgränsa geografiskt i källan."))


# ---------------------------------------------------------------------------
# Tal och datum
# ---------------------------------------------------------------------------

def till_tal(s: pd.Series) -> pd.Series:
    """Tal i svensk eller engelsk form: `2 377,90`, `2377.90`, `1,234.50`, `−12`."""
    t = (s.astype(str).str.strip()
         .str.replace(r"[\s  ']", "", regex=True)
         .str.replace("−", "-", regex=False))
    # Både komma och punkt: det sista tecknet av dem är decimaltecknet.
    bada = t.str.contains(",") & t.str.contains(r"\.")
    komma_sist = t.str.rfind(",") > t.str.rfind(".")
    t = t.where(~(bada & komma_sist), t.str.replace(".", "", regex=False))
    t = t.where(~(bada & ~komma_sist), t.str.replace(",", "", regex=False))
    t = t.str.replace(",", ".", regex=False)
    return pd.to_numeric(t, errors="coerce")


_AAMMDD = r"\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])"
_DATUMNAMN = re.compile(r"datum|date|dag|dt$", re.I)


def till_datum(s: pd.Series) -> pd.Series:
    """ISO-datum, ÅÅÅÅMMDD och — i kolumner som heter något med datum —
    ÅÅMMDD, som svenska ekonomisystem ofta skriver (Härryda: `260113`)."""
    t = s.astype(str).str.strip()
    atta = t.str.fullmatch(r"\d{8}")
    sex = t.str.fullmatch(_AAMMDD) & bool(_DATUMNAMN.search(str(s.name or "")))
    ut = pd.to_datetime(t.where(~atta & ~sex), errors="coerce", format="ISO8601")
    ut = ut.fillna(pd.to_datetime(t.where(atta), errors="coerce", format="%Y%m%d"))
    ut = ut.fillna(pd.to_datetime(t.where(sex), errors="coerce", format="%y%m%d"))
    return ut.fillna(pd.to_datetime(t.where(ut.isna() & ~sex), errors="coerce", dayfirst=True))


def _ar_datum(prov: pd.Series) -> bool:
    """ÅÅÅÅ-MM-DD, eller ÅÅÅÅMMDD som går att läsa som rimliga datum.

    Åtta siffror är också ett tal — Härryda skriver fakturadatum så — och
    avgörs därför på att värdena faktiskt är datum mellan 1900 och 2100.
    """
    t = prov.astype(str).str.strip()
    if t.str.match(r"^\d{4}-\d{2}-\d{2}").mean() > 0.9:
        return till_datum(prov).notna().mean() > 0.9
    if t.str.fullmatch(r"(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])").mean() > 0.9:
        return till_datum(prov).notna().mean() > 0.9
    # Sex siffror är ett datum bara när kolumnen säger det — annars är det
    # lika gärna en kod.
    if _DATUMNAMN.search(str(prov.name or "")) and t.str.fullmatch(_AAMMDD).mean() > 0.9:
        return till_datum(prov).notna().mean() > 0.9
    return False


def _kolumntyp(s: pd.Series) -> str:
    prov = s[s.astype(str).str.strip() != ""].head(500)
    if prov.empty:
        return "tom"
    if _ar_datum(prov):
        return "datum"
    if till_tal(prov).notna().mean() > 0.9:
        return "tal"
    return "text"


# ---------------------------------------------------------------------------
# Beskrivning, filter och aggregering
# ---------------------------------------------------------------------------

def beskriv(tabell: Tabell, exempel: int = 3) -> dict[str, Any]:
    """Kolumner med typ, exempel och — för få olika värden — värdefördelning."""
    df = tabell.df
    kolumner = []
    for k in df.columns:
        s = df[k]
        typ = _kolumntyp(s)
        post: dict[str, Any] = {"namn": k, "typ": typ,
                                "tomma": int((s.astype(str).str.strip() == "").sum()),
                                "exempel": [str(v) for v in s[s.astype(str) != ""].head(exempel)]}
        unika = s.nunique()
        post["olika_varden"] = int(unika)
        if typ == "text" and unika <= 30:
            post["varden"] = {str(k2): int(v) for k2, v in s.value_counts().head(30).items()}
        if typ == "tal":
            t = till_tal(s)
            post.update({"min": float(t.min()), "max": float(t.max()), "summa": float(t.sum())})
        if typ == "datum":
            d = till_datum(s)
            post.update({"fran": str(d.min().date()), "till": str(d.max().date())})
        kolumner.append(post)
    return {"antal_rader": len(df), "kolumner": kolumner, "kallor": tabell.kallor,
            "varningar": tabell.varningar, "blad": tabell.blad}


OPERATORER = ("=", "!=", ">", ">=", "<", "<=", "innehaller", "borjar_med", "i", "mellan")


def filtrera(df: pd.DataFrame, villkor: list[dict[str, Any]] | None) -> pd.DataFrame:
    """`[{kolumn, operator, varde}]`. Jämförelser görs som tal när båda sidor är
    tal, som datum när kolumnen är datum, annars som text."""
    for v in villkor or []:
        k, op, varde = v["kolumn"], v.get("operator", "="), v.get("varde")
        if k not in df.columns:
            raise ValueError(f"Kolumnen {k!r} finns inte. Kolumner: {', '.join(df.columns)}")
        if op not in OPERATORER:
            raise ValueError(f"Operatorn {op!r} finns inte. Välj bland: {', '.join(OPERATORER)}")
        s = df[k]
        if op == "innehaller":
            mask = s.astype(str).str.contains(str(varde), case=False, regex=False)
        elif op == "borjar_med":
            mask = s.astype(str).str.lower().str.startswith(str(varde).lower())
        elif op == "i":
            mask = s.astype(str).isin([str(x) for x in (varde or [])])
        else:
            typ = _kolumntyp(s)
            if typ == "tal":
                jfr = till_tal(s)
                v1 = till_tal(pd.Series([varde] if op != "mellan" else list(varde)))
            elif typ == "datum":
                jfr = till_datum(s)
                v1 = till_datum(pd.Series([varde] if op != "mellan" else list(varde)))
            else:
                jfr = s.astype(str)
                v1 = pd.Series([str(varde)] if op != "mellan" else [str(x) for x in varde])
            if op == "mellan":
                mask = (jfr >= v1.iloc[0]) & (jfr <= v1.iloc[1])
            else:
                mask = {"=": jfr == v1.iloc[0], "!=": jfr != v1.iloc[0],
                        ">": jfr > v1.iloc[0], ">=": jfr >= v1.iloc[0],
                        "<": jfr < v1.iloc[0], "<=": jfr <= v1.iloc[0]}[op]
        df = df[mask.fillna(False)]
    return df


def _grupperingskolumn(df: pd.DataFrame, uttryck: str) -> tuple[str, pd.Series]:
    """`kolumn`, eller `ar:`, `kvartal:`, `manad:` följt av en datumkolumn."""
    if ":" in uttryck and uttryck.split(":", 1)[0] in ("ar", "kvartal", "manad"):
        period, k = uttryck.split(":", 1)
        if k not in df.columns:
            raise ValueError(f"Kolumnen {k!r} finns inte")
        d = till_datum(df[k])
        s = {"ar": d.dt.year.astype("Int64").astype(str),
             "kvartal": d.dt.year.astype("Int64").astype(str) + "-K" + d.dt.quarter.astype("Int64").astype(str),
             "manad": d.dt.strftime("%Y-%m")}[period]
        return uttryck, s
    if uttryck not in df.columns:
        raise ValueError(f"Kolumnen {uttryck!r} finns inte. Kolumner: {', '.join(df.columns)}")
    return uttryck, df[uttryck].astype(str)


FUNKTIONER = ("summa", "medel", "median", "min", "max", "antal")


def aggregera(tabell: Tabell, gruppera: list[str], matt: str | None = None,
              funktion: str = "summa", villkor: list[dict[str, Any]] | None = None,
              topp: int = 50) -> dict[str, Any]:
    """Grupperad aggregering. Returnerar de `topp` största grupperna, summan över
    alla och hur många grupper som utelämnades — så att svaret är litet och
    ändå går att kontrollera."""
    if funktion not in FUNKTIONER:
        raise ValueError(f"Funktionen {funktion!r} finns inte. Välj bland: {', '.join(FUNKTIONER)}")
    df = filtrera(tabell.df, villkor)
    if funktion != "antal" and (not matt or matt not in df.columns):
        raise ValueError(f"Måttkolumnen {matt!r} finns inte. Kolumner: {', '.join(tabell.df.columns)}")
    ram = pd.DataFrame({namn: s for namn, s in (_grupperingskolumn(df, g) for g in gruppera)},
                       index=df.index)
    varden = till_tal(df[matt]) if funktion != "antal" else pd.Series(1, index=df.index)
    ej_tal = int(varden.isna().sum()) if funktion != "antal" else 0
    ram["_v"] = varden
    agg = {"summa": "sum", "medel": "mean", "median": "median", "min": "min", "max": "max",
           "antal": "count"}[funktion]
    res = (ram.groupby(list(gruppera), dropna=False)["_v"].agg(agg).reset_index()
           if gruppera else pd.DataFrame({"_v": [getattr(ram["_v"], agg)()]}))
    res = res.sort_values("_v", ascending=False)
    total = float(getattr(varden, "sum" if funktion in ("summa", "antal") else agg)())
    rader = [{**{g: r[g] for g in gruppera}, funktion: round(float(r["_v"]), 2)}
             for _, r in res.head(topp).iterrows()]
    ut = {"rader_i_urvalet": len(df), "grupper": len(res), "utelamnade_grupper": max(0, len(res) - topp),
          "resultat": rader, f"{funktion}_totalt": round(total, 2), "kallor": tabell.kallor,
          "varningar": list(tabell.varningar)}
    if ej_tal:
        ut["varningar"].append(f"{ej_tal} rader har ett värde i {matt!r} som inte är ett tal och räknades inte.")
    ut["varningar"] += _periodvarningar(tabell.df, villkor)
    return ut


def _periodvarningar(df: pd.DataFrame, villkor: list[dict[str, Any]] | None) -> list[str]:
    """Varnar när en datumavgränsning sträcker sig utanför det datat täcker.

    En kommun som ännu inte publicerat mars ger annars en "kvartalssumma"
    som i själva verket är två månader — och den ser lika trovärdig ut.
    """
    ut = []
    for v in villkor or []:
        k, op, varde = v.get("kolumn"), v.get("operator"), v.get("varde")
        if k not in df.columns or op not in ("mellan", "<", "<=", ">", ">=") or _kolumntyp(df[k]) != "datum":
            continue
        d = till_datum(df[k])
        forsta, sista = d.min(), d.max()
        granser = list(varde) if op == "mellan" else [varde]
        ovre = till_datum(pd.Series([granser[-1]])).iloc[0] if op in ("mellan", "<", "<=") else None
        undre = till_datum(pd.Series([granser[0]])).iloc[0] if op in ("mellan", ">", ">=") else None
        if ovre is not None and pd.notna(ovre) and pd.notna(sista) and ovre > sista:
            ut.append(f"Datat i {k!r} slutar {sista.date()}, före urvalets slut {ovre.date()}. "
                      "Perioden är inte komplett i källan.")
        if undre is not None and pd.notna(undre) and pd.notna(forsta) and undre < forsta:
            ut.append(f"Datat i {k!r} börjar {forsta.date()}, efter urvalets början {undre.date()}.")
    return ut
