# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""API-router för kartvisualisering — geometri och choropleth-GeoJSON.

Levererar geodata i GeoJSON (WGS84) direkt ur PostGIS via `ST_AsGeoJSON`, så
att QGIS-pluginet (eller vilken klient som helst) kan lägga upp lager utan
geopandas. Två byggstenar:

    /karta/geometri    — polygonerna för en områdestyp (baslager)
    /karta/join        — en tabell {kod, värde} joinad till geometri →
                         choropleth. Generisk: samma maskineri för
                         källstatistik och för egna analysresultat.

Kommun- och länspolygoner härleds ur DeSO (materialiserade vyer
`mv_kommun_geom`/`mv_lan_geom`); DeSO/RegSO/valdistrikt kommer direkt ur sina
geom-tabeller.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response

from data_och_analys.geodata.geometrier import LICENS_ATTRIBUTION
from data_och_analys.infra import db

router = APIRouter()

# Följer med varje geometrisvar. CC-BY kräver att källan anges där kartan
# visas — en rad i en README når inte den som får en GeoJSON i handen.
def _sql_str(s: str) -> str:
    """Citerar en konstant sträng för inbäddning i SQL-uttrycket.

    Värdet är en modulkonstant, aldrig användarindata — men escapningen
    görs ändå så att en framtida redigering av texten inte kan bli en
    injektionsyta.
    """
    return "'" + s.replace("'", "''") + "'"


LICENS_ATTRIBUTION = (
    "Kommunindelning © SCB (CC0 1.0). "
    "Landmask © European Environment Agency (CC-BY 4.0)."
)

# Vitlista områdestyp → geometrikälla (tabell/vy). Namnen injiceras i SQL.
#
# kommun/lan pekar på de LANDKLIPPTA geometrierna — strandlinje i
# stället för administrativ gräns genom vattenområden, vilket ger korrekt
# kartbild. De DeSO-unierade varianterna (inkluderar vatten) finns kvar
# som *_medvatten för punkt-i-polygon-frågor där hela den administrativa
# ytan behövs.
#
# Geometrin byggs av två fria källor och ärver båda licenserna:
#   SCB DeSO             CC0 1.0        (kommunindelningen)
#   EEA coastline v2.0   CC-BY 4.0      (landmasken)
#
# ATTRIBUTION KRÄVS i publicerade kartor: se LICENS_ATTRIBUTION nedan.
# Tidigare användes Eurostat GISCO (© EuroGeographics), men dess villkor
# förbjuder kommersiell användning och nyttjanderätten är inte överlåtbar
# — det gör suitens kartutdata ofritt för mottagaren och går inte ihop
# med AGPL. EEA:s kustlinje är dessutom finare (1:100 000 mot 1:1 miljon)
# och träffar SCB:s officiella landarealer bättre.
_GEOM_KALLA: dict[str, str] = {
    "kommun": "kommun_land_v",
    "lan": "lan_land_v",
    "kommun_medvatten": "mv_kommun_geom",
    "lan_medvatten": "mv_lan_geom",
    "deso": "deso_geom",
    "regso": "regso_geom",
    "valdistrikt": "valdistrikt_geom",
}
# Vilka källor som har en namn-kolumn (annars används koden som namn).
_HAR_NAMN = {"kommun_land_v", "lan_land_v", "mv_kommun_geom", "mv_lan_geom"}

_GEOJSON = "application/geo+json"


def _kalla(typ: str) -> str:
    tab = _GEOM_KALLA.get(typ)
    if tab is None:
        raise HTTPException(
            400, f"okänd områdestyp {typ!r}. Giltiga: {', '.join(_GEOM_KALLA)}"
        )
    return tab


def _namn_uttryck(tab: str) -> str:
    return "namn" if tab in _HAR_NAMN else "kod"


@router.get("/omraden")
async def lista_omraden() -> list[dict[str, Any]]:
    """Listar tillgängliga områdestyper med antal."""
    ut: list[dict[str, Any]] = []
    p = db.prefix()
    async with db.hamta_db() as c:
        for typ, tab in _GEOM_KALLA.items():
            try:
                n = await c.fetchval(f"SELECT COUNT(*) FROM {p}{tab}")
            except Exception:  # noqa: BLE001 — vy kanske inte byggd
                n = 0
            ut.append({"typ": typ, "antal": n})
    return ut


@router.get("/geometri")
async def hamta_geometri(
    typ: str = Query("kommun", description="kommun, lan, deso, regso, valdistrikt"),
    kod_prefix: str | None = Query(
        None, description="Begränsa till koder som börjar med detta (t.ex. 0180 för en kommuns DeSO)."
    ),
) -> Response:
    """Returnerar en områdestyps polygoner som GeoJSON FeatureCollection.

    Baslager för kartan. `kod_prefix` filtrerar (användbart för de stora
    DeSO/RegSO-mängderna). Egenskaper per feature: `kod`, `namn`.
    """
    tab = _kalla(typ)
    namn = _namn_uttryck(tab)
    p = db.prefix()
    villkor = ""
    params: list[Any] = []
    if kod_prefix:
        params.append(kod_prefix + "%")
        villkor = f" WHERE kod LIKE ${len(params)}"
    sql = (
        f"SELECT jsonb_build_object('type','FeatureCollection',"
        f" 'attribution', {_sql_str(LICENS_ATTRIBUTION)},"
        f" 'features',"
        f" COALESCE(jsonb_agg(jsonb_build_object("
        f"  'type','Feature','geometry', ST_AsGeoJSON(geom)::jsonb,"
        f"  'properties', jsonb_build_object('kod', kod, 'namn', {namn})"
        f" )), '[]'::jsonb))::text "
        f"FROM {p}{tab}{villkor}"
    )
    async with db.hamta_db() as c:
        gj = await c.fetchval(sql, *params)
    return Response(content=gj or '{"type":"FeatureCollection","features":[]}',
                    media_type=_GEOJSON)


@router.post("/join")
async def join_choropleth(
    typ: str = Query("kommun", description="Områdestyp att joina mot"),
    rader: list[dict[str, Any]] = Body(
        ...,
        description=(
            "Lista av {kod, varde}. `kod` matchas mot områdets kod "
            "(t.ex. 4-siffrig kommunkod). Funkar för källstatistik och "
            "för egna analysresultat."
        ),
    ),
) -> Response:
    """Joinar en värdetabell till geometri → choropleth-GeoJSON.

    Områden utan värde får `varde=null` (ritas som saknat). Det här är navet
    för att visa vad som helst på karta — välj statistik i pluginet, eller
    klistra in resultatet av en analys.
    """
    tab = _kalla(typ)
    namn = _namn_uttryck(tab)
    p = db.prefix()
    koder = [str(r.get("kod")) for r in rader if r.get("kod") is not None]
    varden = [
        float(r["varde"]) if r.get("varde") not in (None, "") else None
        for r in rader
        if r.get("kod") is not None
    ]
    sql = (
        f"WITH v(kod, varde) AS (SELECT * FROM unnest($1::text[], $2::float8[])) "
        f"SELECT jsonb_build_object('type','FeatureCollection',"
        f" 'attribution', {_sql_str(LICENS_ATTRIBUTION)},"
        f" 'features',"
        f" COALESCE(jsonb_agg(jsonb_build_object("
        f"  'type','Feature','geometry', ST_AsGeoJSON(g.geom)::jsonb,"
        f"  'properties', jsonb_build_object('kod', g.kod, 'namn', g.{namn}, 'varde', v.varde)"
        f" )), '[]'::jsonb))::text "
        f"FROM {p}{tab} g LEFT JOIN v ON v.kod = g.kod"
    )
    async with db.hamta_db() as c:
        gj = await c.fetchval(sql, koder, varden)
    return Response(content=gj or '{"type":"FeatureCollection","features":[]}',
                    media_type=_GEOJSON)


@router.post("/refresh-geometri")
async def refresh_geometri() -> dict[str, str]:
    """Bygger om de härledda kommun-/länsvyerna (efter DeSO-uppdatering)."""
    p = db.prefix()
    async with db.hamta_db() as c:
        await c.execute(f"REFRESH MATERIALIZED VIEW {p}mv_kommun_geom")
        await c.execute(f"REFRESH MATERIALIZED VIEW {p}mv_lan_geom")
    return {"status": "kommun- och länsgeometri ombyggd"}


@router.get("/skolenheter")
async def skolenheter(
    kommun_kod: str | None = Query(
        None, description="Fyrsiffrig kommunkod; utelämnad ger hela riket"
    ),
    lan_kod: str | None = Query(None, description="Tvåsiffrig länskod"),
    status: str | None = Query("Aktiv", description="Aktiv, Vilande, Planerad; tom = alla"),
    skolform: str | None = Query(None, description="Delsträng, t.ex. Grundskola"),
    huvudman: str | None = Query(None, description="Kommun, Enskild, Region, Stat"),
) -> dict[str, Any]:
    """Skolenheter som GeoJSON-punkter — ett punktlager, inte en choropleth.

    Läser spegeltabellen som `cli/synka_skolenheter.py` fyller; inga anrop
    går mot Skolverket här. Enheter utan koordinat utelämnas, eftersom en
    punkt utan läge inte går att rita — antalet står i `utan_koordinat` så
    att bortfallet syns.
    """
    villkor = ["geom IS NOT NULL"]
    params: list[Any] = []

    def lagg(uttryck: str, varde: Any) -> None:
        params.append(varde)
        villkor.append(uttryck.format(n=len(params)))

    if kommun_kod:
        lagg("kommun_kod = ${n}", kommun_kod)
    if lan_kod:
        lagg("left(kommun_kod, 2) = ${n}", lan_kod)
    if status:
        lagg("status = ${n}", status)
    if skolform:
        lagg("skolformer ILIKE '%%' || ${n} || '%%'", skolform)
    if huvudman:
        lagg("huvudman = ${n}", huvudman)

    where = " AND ".join(villkor)
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            f"SELECT kod, namn, kommun_kod, status, huvudman, skolformer, "
            f"lage_kalla, lage_precision, "
            f"ST_X(geom) AS lon, ST_Y(geom) AS lat "
            f"FROM {db.prefix()}skolenhet_punkt WHERE {where} ORDER BY kod",
            *params,
        )
        utan = await anslutning.fetchval(
            f"SELECT count(*) FROM {db.prefix()}skolenhet_punkt WHERE geom IS NULL"
        )
        # Enheter som saknar gatuadress i källan går inte att placera alls.
        # Det är en kvalitetsbrist i grundregistreringen — gatuadress krävs —
        # och den hör hemma i svaret som ett mätvärde om registret, inte som
        # ett tyst bortfall.
        utan_gatuadress = await anslutning.fetchval(
            f"SELECT count(*) FROM {db.prefix()}skolenhet_punkt "
            f"WHERE lage_kalla = 'saknar_gatuadress'"
        )
        # Spegeln är inte sanningen. Registret ändras uppmätt ~10 % per
        # månad, så åldern hör till svaret — annars går en tre månader
        # gammal kopia inte att skilja från en färsk.
        aldst = await anslutning.fetchval(
            f"SELECT min(senast_synkad) FROM {db.prefix()}skolenhet_punkt"
        )

    return {
        "type": "FeatureCollection",
        "attribution": (
            "Skolenhetsregistret © Skolverket. Geokodade lägen "
            "© OpenStreetMap-bidragsgivare, ODbL 1.0."
        ),
        "utan_koordinat": utan,
        "utan_gatuadress_i_kallan": utan_gatuadress,
        "speglad_senast": aldst.isoformat() if aldst else None,
        "kadens_dagar": 30,
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {
                    "kod": r["kod"], "namn": r["namn"],
                    "kommun_kod": r["kommun_kod"], "status": r["status"],
                    "huvudman": r["huvudman"], "skolformer": r["skolformer"],
                    # Varifrån punkten kommer. 'kalla' är Skolverkets egen
                    # koordinat; 'nominatim' är en geokodad besöksadress, där
                    # precisionen skiljer en byggnadsträff från en gatuträff.
                    "lage_kalla": r["lage_kalla"],
                    "lage_precision": r["lage_precision"],
                },
            }
            for r in rader
        ],
    }
