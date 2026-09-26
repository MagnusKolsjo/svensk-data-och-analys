# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""PxWebApi 2-klient — gemensam för alla myndigheter som rullat över till v2.

Myndighet väljs via en `myndighet`-parameter; bas-URL och celltak slås
upp i det gemensamma registret (`klienter.myndigheter`). Klienten
avvisar myndigheter som registret säger fortfarande talar v1.

PxWebApi 2 är PxWeb-konsortiets RESTful-efterföljare till v1. Skillnader
mot v1:

    - GET istället för POST för datafrågor; selektorer kodas som
      upprepade query-parametrar
    - URL-strukturen är platt: /tables, /tables/{id}, /tables/{id}/data
    - Tabell-ID är globala (inga stigsegment behöver navigeras)
    - JSON-svar följer JSON-stat 2.0

SCB rullade över först; övriga myndigheter följer stegvis. När en
installation går över ändras dess post i registret (eller en .env-rad
sätts) och samma myndighet betjänas av denna klient istället för v1.

Celltaket (SCB: 150 000) räknas mot i förväg om metadata följer med
datafrågan, så att stora urval upptäcks innan API-anropet.

Ingångspunkter:
    lista_myndigheter() -> list[str]
    lista_tabeller(myndighet, query=None, sprak="sv") -> list[Tabell]
    hamta_metadata(myndighet, tabell_id, sprak="sv") -> Tabellmetadata
    hamta_data(myndighet, tabell_id, urval, sprak="sv") -> dict
    uppskatta_celler(metadata, urval) -> int
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from data_och_analys.infra.rate_limit import hamta_grind
from data_och_analys.klienter import myndigheter

logger = logging.getLogger(__name__)

VERSION = 2


def _hamta_konfig(namn: str) -> myndigheter.Myndighet:
    """Hämtar myndighetskonfig och vägrar om versionen inte är 2."""
    m = myndigheter.hamta(namn)
    if m.version != VERSION:
        raise ValueError(
            f"myndigheten {namn!r} talar PxWeb {m.version}, inte {VERSION}. "
            f"Använd pxweb_{m.version}-klienten."
        )
    return m


def lista_myndigheter() -> list[str]:
    """Returnerar ID:n för alla myndigheter som för närvarande talar v2."""
    return myndigheter.myndigheter_med_version(VERSION)


# ============================================================================
# Wire-modeller (PxWebApi 2)
# ============================================================================


class Tabell(BaseModel):
    """En tabell i tabell-listan."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    label: str | None = None
    description: str | None = None
    updated: str | None = None
    category: str | None = None
    source: str | None = None


class Variabel(BaseModel):
    """En dimension i tabellens metadata.

    `values` och `value_texts` är parallella listor — index N i den ena
    matchar index N i den andra. Tidsvariabeln markeras med `time=True`.

    Högkardinala dimensioner (Region kan ha 19 000+ värden i SCB:s
    geografi-tabeller) trunkeras i metadatasvaret för att inte spränga
    MCP-kanalens 1 MB-cap. När så sker:

        truncated = True
        total_values = faktiskt antal värden i källan
        values / value_texts = samplad delmängd (de första N)

    Den fullständiga värdelistan hämtas då via
    `hamta_dimensionsvarden(...)` med prefix- eller substring-filter.
    Cellberäkningen (`uppskatta_celler`) använder `total_values` när det
    är satt, så förhandskontrollen mot celltaket fortsätter att stämma.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    label: str | None = None
    type: str | None = None
    values: list[str] = Field(default_factory=list)
    value_texts: list[str] = Field(default_factory=list, alias="valueTexts")
    time: bool = False
    elimination: bool = False
    truncated: bool = False
    total_values: int | None = None
    # Alternativa indelningstyper (t.ex. Region: Kommuner/Län/NUTS2). Varje
    # post: {id, label, type} där type är "Valueset" eller "Aggregation".
    codelists: list[dict[str, Any]] = Field(default_factory=list)


class DimensionsVarde(BaseModel):
    """Ett enskilt värde i en dimension — kod plus etikett."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    kod: str
    etikett: str


class DimensionsSida(BaseModel):
    """En filtrerad och paginerad sida av en dimensions värden."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dimension_id: str
    label: str | None = None
    totalt_i_kallan: int
    matchande: int
    sida: int
    sida_storlek: int
    varden: list[DimensionsVarde] = Field(default_factory=list)


class Tabellmetadata(BaseModel):
    """Metadata för en tabell — variabler, källa, uppdateringstid."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str
    label: str | None = None
    description: str | None = None
    source: str | None = None
    updated: str | None = None
    variables: list[Variabel] = Field(default_factory=list)


# ============================================================================
# HTTP-anrop
# ============================================================================

USER_AGENT = "svensk-data-och-analys/PxWeb2Client (+https://github.com/)"
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


async def _utfor_get(
    myndighet_namn: str,
    bas_url: str,
    sokvag: str,
    parametrar: dict[str, Any] | None = None,
) -> httpx.Response:
    """Utför ett GET-anrop, väntar på rate-grinden och kontrollerar status.

    Returnerar det råa svaret så att anroparen själv avgör hur kroppen
    ska tolkas — metadata- och tabellistningar vill ha JSON, medan
    datafrågor kan begära csv/px/html via `outputFormat`.
    """
    await hamta_grind(myndighet_namn).vanta()
    url = f"{bas_url}/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.get(url, params=parametrar)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"PxWeb v2-anrop misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar


async def _hamta_json(
    myndighet_namn: str,
    bas_url: str,
    sokvag: str,
    parametrar: dict[str, Any] | None = None,
) -> Any:
    svar = await _utfor_get(myndighet_namn, bas_url, sokvag, parametrar)
    return svar.json()


async def _utfor_post(
    myndighet_namn: str,
    bas_url: str,
    sokvag: str,
    kropp: dict[str, Any],
    parametrar: dict[str, Any] | None = None,
) -> httpx.Response:
    """POST med rate-limit och statuskontroll — returnerar råsvaret.

    Datafrågor går som POST i stället för GET: stora urval blir annars
    en URL som överskrider serverns längdgräns (404/414). Selektorerna
    bärs i JSON-kroppen medan `lang` och `outputFormat` är query-
    parametrar precis som vid GET.
    """
    await hamta_grind(myndighet_namn).vanta()
    url = f"{bas_url}/{sokvag.lstrip('/')}"
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as klient:
        svar = await klient.post(url, params=parametrar, json=kropp)
    if svar.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"PxWeb v2-datafråga misslyckades ({svar.status_code}) mot {url}: "
            f"{svar.text[:200]}",
            request=svar.request,
            response=svar,
        )
    return svar


# ============================================================================
# Publikt API
# ============================================================================


async def lista_tabeller(
    myndighet: str,
    query: str | None = None,
    sprak: str | None = None,
    sida_storlek: int = 100,
    sida: int = 1,
) -> list[Tabell]:
    """Söker eller listar tabeller hos en myndighet. `query` är fritext.

    Sidstorlek 100 är ett rimligt avvägt urval — API:t returnerar fält
    som `pageSize` och `totalElements` i svaret men de exponeras inte
    här. Större träffmängder hämtas genom att öka `sida`.
    """
    m = _hamta_konfig(myndighet)
    parametrar: dict[str, Any] = {
        "lang": sprak or m.sprak,
        "pageSize": sida_storlek,
        "pageNumber": sida,
    }
    if query:
        parametrar["query"] = query
    data = await _hamta_json(myndighet, m.bas_url, "tables", parametrar)
    # Endpoints returnerar antingen {"tables": [...]} eller en bar lista
    # beroende på version — hantera bägge.
    rader = data.get("tables", data) if isinstance(data, dict) else data
    return [Tabell.model_validate(x) for x in rader]


def _ids_i_ordning(kat: dict[str, Any]) -> list[str]:
    """Hämtar dimensionens värde-ID:n i deklarationsordning.

    JSON-stat tillåter `category.index` att vara en dict (id→position)
    eller en list (position→id) — bägge former förekommer hos SCB
    beroende på dimensionens storlek.
    """
    index = kat.get("index", {})
    if isinstance(index, dict):
        ordered = sorted(index.items(), key=lambda kv: kv[1])
        return [k for k, _ in ordered]
    return list(index)


def _packa_upp_jsonstat2(
    raw: dict[str, Any],
    tabell_id: str,
    max_varden_per_dim: int | None = None,
) -> dict[str, Any]:
    """Packar upp PxWebApi 2:s JSON-stat2-metadata till vår internform.

    SCB:s metadata-svar följer JSON-stat 2.0:

        {
          "id":         ["Region", "UtlBakgrund", "Kon", "ContentsCode", "Tid"],
          "size":       [19182, 3, 3, 1, 16],
          "dimension":  {"Region": {"label": "...", "category": {...}}, ...},
          "role":       {"time": ["Tid"], "metric": ["ContentsCode"]},
          "label":      "Folkmängden per region efter ...",
          "source":     "SCB",
          "updated":    "2026-03-24T07:00:00Z"
        }

    Vi packar upp `dimension`-dicten till en lista av Variabel-objekt
    så att resten av koden (`uppskatta_celler`, `hamta_data`) arbetar
    mot samma representation som v1 ger.

    Geo-tabeller kan ha tiotusentals värden i en enskild dimension
    (TAB6571.Region har 19 182 värden — riket + län + kommuner + DeSO
    + RegSO). Hela värdelistan får då inte plats i MCP-kanalens 1 MB-
    cap. När `max_varden_per_dim` sätts trunkeras värdelistan och
    `truncated`/`total_values` flaggas. Den fullständiga listan hämtas
    via `hamta_dimensionsvarden`. Cellberäkningen använder
    `total_values` så förhandskontrollen mot celltaket fortsätter att
    stämma även mot trunkerad metadata.
    """
    role = raw.get("role") or {}
    tids_dims = set(role.get("time") or [])

    dim_ids: list[str] = raw.get("id") or []
    dim_meta: dict[str, Any] = raw.get("dimension") or {}

    variables: list[dict[str, Any]] = []
    for did in dim_ids:
        d = dim_meta.get(did, {}) or {}
        kat = d.get("category") or {}
        label = kat.get("label", {}) or {}
        ids = _ids_i_ordning(kat)
        antal = len(ids)
        truncated = (
            max_varden_per_dim is not None and antal > max_varden_per_dim
        )
        synliga = ids[:max_varden_per_dim] if truncated else ids
        value_texts = [label.get(k, k) for k in synliga]
        # Kodlistor = alternativa indelningstyper för dimensionen (SCB:s Region
        # har t.ex. Kommuner, Län, Riket, NUTS2…). Default-värdelistan blandar
        # ofta nivåerna; med kodlistan kan klienten visa en typ i taget.
        ext = d.get("extension") or {}
        kodlistor = [
            {"id": cl.get("id"), "label": cl.get("label"), "type": cl.get("type")}
            for cl in (ext.get("codelists") or [])
            if cl.get("id")
        ]
        # `extension.elimination` säger om dimensionen kan utelämnas ur
        # urvalet. False = obligatorisk; SCB returnerar 400 ("Missing
        # selection for mandantory variable") om den saknas. Säker
        # fallback är False — vi vill hellre flagga något som
        # obligatoriskt än släppa genom en saknad mandatory.
        elimination = bool(ext.get("elimination", False))
        variables.append({
            "id": did,
            "label": d.get("label"),
            "values": synliga,
            "valueTexts": value_texts,
            "time": did in tids_dims,
            "elimination": elimination,
            "truncated": truncated,
            "total_values": antal,
            "codelists": kodlistor,
        })

    return {
        "id": tabell_id,
        "label": raw.get("label"),
        "source": raw.get("source"),
        "updated": raw.get("updated"),
        "variables": variables,
    }


async def hamta_metadata(
    myndighet: str,
    tabell_id: str,
    sprak: str | None = None,
    max_varden_per_dim: int | None = None,
) -> Tabellmetadata:
    """Metadata för en tabell.

    Alltid anropad innan en datafråga byggs — variabel-ID:n och
    värdelistor får aldrig antas från träningsdata. Returvärdet matas
    till `uppskatta_celler` (eller direkt till `hamta_data`) för att
    avgöra om frågan ryms inom myndighetens celltak.

    SCB returnerar JSON-stat 2.0 där `id` är en lista med dimensions-
    namn (inte tabell-id) och `dimension` är en dict med kategorier.
    Vi packar upp det till en `variables`-lista som matchar v1-formatet
    — så att `uppskatta_celler` och `hamta_data` ser samma struktur
    oavsett version.

    `max_varden_per_dim` trunkerar dimensioner med fler värden än taket
    och flaggar dem som `truncated=True, total_values=N`. Geo-tabeller
    som TAB6571 (Region=19 182) ryms annars inte i MCP-kanalens 1 MB-cap.
    När en dimension är trunkerad fortsätter `uppskatta_celler` att räkna
    rätt — `total_values` används i stället för `len(values)`.
    """
    m = _hamta_konfig(myndighet)
    data = await _hamta_json(
        myndighet,
        m.bas_url,
        f"tables/{tabell_id}/metadata",
        {"lang": sprak or m.sprak},
    )
    # Vissa installationer kapslar metadatan under `.metadata`
    if isinstance(data, dict) and "dimension" not in data and "metadata" in data:
        data = data["metadata"]
    rensad = _packa_upp_jsonstat2(
        data, tabell_id, max_varden_per_dim=max_varden_per_dim
    )
    return Tabellmetadata.model_validate(rensad)


async def hamta_kodlista(
    myndighet: str, codelist_id: str, sprak: str | None = None
) -> list[DimensionsVarde]:
    """Hämtar värdena för en specifik kodlista (indelningstyp).

    SCB:s Region-dimension har flera kodlistor — Kommuner, Län, Riket,
    NUTS2 osv. Default-värdelistan blandar nivåerna; den här hämtar bara
    en typ via `/codeLists/{id}` så att UI:t kan visa "välj typ, sedan
    regioner". Returnerar `{kod, etikett}` per värde.
    """
    m = _hamta_konfig(myndighet)
    data = await _hamta_json(
        myndighet, m.bas_url, f"codeLists/{codelist_id}", {"lang": sprak or m.sprak}
    )
    varden = data.get("values", []) if isinstance(data, dict) else []
    return [
        DimensionsVarde(kod=v.get("code", ""), etikett=v.get("label", v.get("code", "")))
        for v in varden
    ]


async def hamta_dimensionsvarden(
    myndighet: str,
    tabell_id: str,
    dimension_id: str,
    sprak: str | None = None,
    prefix: str | None = None,
    innehaller: str | None = None,
    sida: int = 1,
    sida_storlek: int = 500,
) -> DimensionsSida:
    """Slår upp en enskild dimensions värden med filter och paginering.

    Komplement till `hamta_metadata` när en dimension är så stor att den
    trunkerats. `prefix` matchar mot värdets kod (t.ex. `0123` plockar
    alla geo-värden under Järfälla kommun — kommun, DeSO och RegSO med
    ledande 0123). `innehaller` matchar mot etiketten case-insensitivt.
    Filtren är AND.

    Pagineringen är 1-baserad. `totalt_i_kallan` är dimensionens fulla
    storlek hos myndigheten; `matchande` är antalet efter filtrering.
    """
    m = _hamta_konfig(myndighet)
    data = await _hamta_json(
        myndighet,
        m.bas_url,
        f"tables/{tabell_id}/metadata",
        {"lang": sprak or m.sprak},
    )
    if isinstance(data, dict) and "dimension" not in data and "metadata" in data:
        data = data["metadata"]

    dim_meta = (data.get("dimension") or {}).get(dimension_id)
    if dim_meta is None:
        kanda = list((data.get("dimension") or {}).keys())
        raise ValueError(
            f"dimensionen {dimension_id!r} finns inte i {tabell_id}. "
            f"Tillgängliga: {kanda}"
        )
    kat = dim_meta.get("category") or {}
    label = kat.get("label", {}) or {}
    alla_ids = _ids_i_ordning(kat)

    # Filtrering på kod och etikett.
    innehaller_norm = innehaller.casefold() if innehaller else None
    filtrerade: list[tuple[str, str]] = []
    for k in alla_ids:
        if prefix and not k.startswith(prefix):
            continue
        etikett = label.get(k, k)
        if innehaller_norm and innehaller_norm not in etikett.casefold():
            continue
        filtrerade.append((k, etikett))

    # Paginering — 1-baserad sida.
    if sida_storlek < 1:
        sida_storlek = 500
    if sida < 1:
        sida = 1
    start = (sida - 1) * sida_storlek
    slut = start + sida_storlek
    sida_varden = [
        {"kod": k, "etikett": e} for k, e in filtrerade[start:slut]
    ]

    return DimensionsSida.model_validate({
        "dimension_id": dimension_id,
        "label": dim_meta.get("label"),
        "totalt_i_kallan": len(alla_ids),
        "matchande": len(filtrerade),
        "sida": sida,
        "sida_storlek": sida_storlek,
        "varden": sida_varden,
    })


def _fullt_antal(v: Variabel) -> int:
    """Antal värden i dimensionen — full källa, inte den synliga delmängden.

    Trunkerade dimensioner bär det faktiska antalet i `total_values`;
    icke-trunkerade har `len(values)` som källa.
    """
    if v.total_values is not None:
        return v.total_values
    return len(v.values)


def uppskatta_celler(
    metadata: Tabellmetadata, urval: dict[str, list[str]]
) -> int:
    """Multiplicerar urvalsstorlekar för att uppskatta antalet dataceller.

    Saknad variabel i `urval` (eller `["*"]`) tolkas som "alla värden" —
    det är det säkraste antagandet och stämmer med v2:s beteende. För
    trunkerade dimensioner används `total_values` så uppskattningen
    speglar källan, inte den synliga delmängden.
    """
    celler = 1
    for v in metadata.variables:
        valda = urval.get(v.id)
        if valda and valda != ["*"]:
            antal = len(valda)
        else:
            antal = _fullt_antal(v)
        celler *= max(antal, 1)
    return celler


async def hamta_data(
    myndighet: str,
    tabell_id: str,
    urval: dict[str, list[str]],
    sprak: str | None = None,
    format: str = "json-stat2",
    metadata: Tabellmetadata | None = None,
) -> dict[str, Any]:
    """Hämtar data från en tabell.

    `urval` mappar variabel-ID till lista av värden:
        {"Region": ["00", "01"], "Tid": ["2024"]}
    Värdet `["*"]` betyder "alla värden" enligt v2:s wildcard.

    Frågan skickas som POST, inte GET: stora urval (t.ex. 50+ regioner)
    blir annars en URL som överskrider serverns längdgräns och ger 404.
    POST:en kräver dock — till skillnad från GET — ett explicit urval för
    *varje* obligatorisk variabel; en utelämnad variabel defaultar inte
    till "alla värden". Därför fylls variabler som anroparen inte angav i
    med wildcard `["*"]`, vilket kräver tabellens variabellista. Den tas
    ur `metadata` om den skickas in, annars hämtas en lättviktig metadata
    (värdelistorna trunkeras bort — bara variabel-ID:na behövs).

    Skickas `metadata` in räknas cellantalet dessutom upp i förväg och en
    `ValueError` kastas om frågan överskrider myndighetens celltak
    (registret bär taket per myndighet; standard 150 000).

    `format` styr `outputFormat` i anropet. JSON-format (json-stat2,
    json-stat m.fl.) returneras som tolkat objekt; textformat (csv, px,
    html) kan inte tolkas som JSON och bärs i stället som text i ett
    omslag `{"format", "content_type", "innehall"}`. Vilket svaret är
    avgörs av svarets Content-Type, inte av formatnamnet — så vi slipper
    hårdkoda PxWeb:s formatlista. Binära format (xlsx, parquet) kan inte
    transporteras över MCP-kanalen och avvisas med ett tydligt fel.
    """
    m = _hamta_konfig(myndighet)

    if metadata is not None:
        # `uppskatta_celler` hanterar `["*"]` och saknade variabler som
        # "alla värden" direkt — wildcard behöver inte expanderas här
        # (vilket är viktigt: med trunkerade dimensioner finns inte hela
        # värdelistan i metadatat utan måste hämtas via
        # `hamta_dimensionsvarden`, men `total_values` räcker för
        # förhandskontrollen mot celltaket).
        antal = uppskatta_celler(metadata, urval)
        if antal > m.max_celler:
            raise ValueError(
                f"frågan ger ca {antal} celler — {myndighet}:s tak är "
                f"{m.max_celler}. Smalna urvalet eller chunka över en variabel."
            )
    else:
        # POST behöver hela variabellistan för att fylla i wildcard för
        # de variabler anroparen utelämnade. Trunkera värdelistorna bort
        # (max_varden_per_dim=0) — bara variabel-ID:na behövs här.
        metadata = await hamta_metadata(
            myndighet, tabell_id, sprak=sprak, max_varden_per_dim=0
        )

    # Bygg en fullständig selektorlista: anroparens värden där de finns,
    # annars wildcard. POST avvisar annars med "Missing selection for
    # mandatory variable".
    selektorer = [
        {"variableCode": v.id, "valueCodes": urval.get(v.id) or ["*"]}
        for v in metadata.variables
    ]
    kropp = {"selection": selektorer}
    parametrar: dict[str, Any] = {
        "lang": sprak or m.sprak,
        "outputFormat": format,
    }

    svar = await _utfor_post(
        myndighet, m.bas_url, f"tables/{tabell_id}/data", kropp, parametrar
    )
    innehallstyp = svar.headers.get("content-type", "").lower()

    if "json" in innehallstyp:
        return svar.json()

    # Binära format (xlsx, parquet, octet-stream) går inte att skicka
    # över MCP-kanalen — avvisa dem innan texttolkning. Allt-listan av
    # textformat (csv, px, html) är öppen, så vi blockerar binärt i stället
    # för att försöka räkna upp alla textvarianter. Notera att
    # "spreadsheetml" innehåller delsträngen "xml" — därför matchar vi på
    # binära markörer, inte på en bred xml-nyckel.
    binara_markorer = (
        "spreadsheet",
        "officedocument",
        "octet-stream",
        "parquet",
        "excel",
        "zip",
        "pdf",
    )
    if any(markor in innehallstyp for markor in binara_markorer):
        raise ValueError(
            f"formatet {format!r} gav ett binärt svar ({innehallstyp!r}) som "
            f"inte kan bäras över MCP-kanalen. Välj json-stat2 för "
            f"strukturerad data eller csv för text."
        )

    # Textformat (csv, px, html) bärs som text — MCP-kanalen är JSON,
    # så texten läggs i ett omslag i stället för att tolkas som JSON.
    return {
        "format": format,
        "content_type": innehallstyp,
        "innehall": svar.text,
    }
