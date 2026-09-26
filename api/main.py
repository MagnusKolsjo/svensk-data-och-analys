# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""FastAPI-app (localhost:8000) för Excel/Power Query.

Lokal REST-server som låter Excel hämta data från projektets datakällor
via Power Query (`Web.Contents` + `Json.Document`). Körs som en
launchd-tjänst på `127.0.0.1:8000` — utan publik webbdel, utan auth,
för enskilt skrivbordsbruk.

Endpoints är grupperade i routrar — en per datakälla, t.ex.:

    /geo/*           — lokala indelningar och klassificeringar (Postgres)
    /kolada/*        — proxy mot Koladas v3 REST-API
    /pxweb-1, /pxweb-2 — PxWeb-myndigheterna (inkl. Tillväxtanalys)
    /wfs/*, /arcgis/* — geodata-discovery
    /skolverket, /trafa, /socialstyrelsen, /bra, /fk, /bibstat,
    /riksbanken      — myndighetsstatistik (samma klienter som doa-special)
    /dcat, /researchdata — katalog/discovery (samma som doa-katalog)

Hela listan finns på `/` och i Swagger (`/docs`).

Svar är JSON. Listor returneras som JSON-arrays — Power Query
konverterar dem direkt till tabeller via `Table.FromRecords`.

Starta från repots rot:
    .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000

Ska tjänsten starta vid inloggning läggs kommandot i en launchd-tjänst
(macOS) eller en systemd-tjänst (Linux).
"""

from __future__ import annotations

# --- PROJEKT_ROT_AUTODISCOVER ----------------------------------------------
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
# --------------------------------------------------------------------------

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from data_och_analys.infra import konfig

logger = logging.getLogger(__name__)


@asynccontextmanager
async def livstid(app: FastAPI):
    """Startup/shutdown — läser .env och förvärmer embedding-modellen.

    Modellen förvärms i en bakgrundstråd så att den första semantiska
    sökningen inte betalar laddningstiden (~10 s). Servern börjar svara
    direkt; kommer en sökning innan modellen är varm lazy-laddas den ändå
    (trådsäkert), så förvärmningen är en optimering, inte ett krav.
    """
    import asyncio

    konfig.las_env()

    async def _forvarm() -> None:
        try:
            from data_och_analys.sok import embeddings
            await asyncio.to_thread(embeddings.forvarm)
            logger.info("Embedding-modell förvärmd")
        except Exception as fel:  # noqa: BLE001
            logger.warning("Förvärmning av embedding-modell misslyckades: %s", fel)

    uppgift = asyncio.create_task(_forvarm())
    logger.info("doa-api startad")
    yield
    uppgift.cancel()
    logger.info("doa-api stängs")


VERSION = "1.0.0"

app = FastAPI(
    title="svensk-data-och-analys — Excel/Power Query API",
    description=(
        "Lokal REST-server för Excel-integration. Bortkopplad från "
        "internet, ingen auth — endast för enskilt skrivbord."
    ),
    version=VERSION,
    lifespan=livstid,
)


# Office-tillägget körs i en webbvy och kan göra anrop från localhost-origins.
# Servern är lokal och utan auth, så en tillåtande localhost-CORS är ofarlig.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://localhost(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Statiska filer för Office-tillägget (manifest, sidopanel, funktioner).
# Serveras över HTTPS på :8443; samma app betjänar även HTTP :8000.
# Excels webbvy cachar tilläggets filer heuristiskt när svaret saknar
# Cache-Control. En ny version av panelen syns då inte förrän användaren
# rensar WebKit-cachen — och får under tiden intryck av att ändringen inte
# gjordes. Tjänsten är lokal och filerna små, så det finns inget att vinna
# på att cacha dem.
@app.middleware("http")
async def _ingen_cache_for_tillagget(request, call_next):
    svar = await call_next(request)
    if request.url.path.startswith("/tillagg"):
        svar.headers["Cache-Control"] = "no-store, must-revalidate"
    return svar


_TILLAGG = Path(__file__).resolve().parents[1] / "excel-tillagg"
if _TILLAGG.is_dir():
    app.mount("/tillagg", StaticFiles(directory=str(_TILLAGG), html=True), name="tillagg")


@app.get("/", tags=["root"])
async def root() -> dict[str, str | list[str]]:
    """Listar tillgängliga endpoints."""
    return {
        "namn": "svensk-data-och-analys api",
        "version": VERSION,
        "routrar": [
            "/sok",
            "/geo", "/kolada", "/pxweb-1", "/pxweb-2", "/wfs", "/arcgis",
            "/skolverket", "/trafa", "/socialstyrelsen", "/bra", "/fk",
            "/bibstat", "/riksbanken", "/dcat", "/researchdata",
        ],
        "docs": "/docs (Swagger UI)",
        "openapi": "/openapi.json",
    }


@app.get("/halsa", tags=["root"])
async def halsa() -> dict[str, str]:
    """Hälsokoll — Power Query kan polla denna för att verifiera servern lever."""
    return {"status": "ok"}


# Routrar
from api.routers import (  # noqa: E402
    arcgis,
    bra,
    dcat,
    forsakringskassan,
    geo,
    karta,
    kb_bibstat,
    kolada,
    pxweb_1,
    pxweb_2,
    researchdata,
    riksbanken,
    skolverket,
    socialstyrelsen,
    sok,
    trafa,
    wfs,
)

# Federerat sök-nav — ingång för "vad finns och var hämtar jag det?"
app.include_router(sok.router, prefix="/sok", tags=["sok"])

app.include_router(geo.router, prefix="/geo", tags=["geo"])
app.include_router(karta.router, prefix="/karta", tags=["karta"])
app.include_router(kolada.router, prefix="/kolada", tags=["kolada"])
app.include_router(pxweb_1.router, prefix="/pxweb-1", tags=["pxweb-1"])
app.include_router(pxweb_2.router, prefix="/pxweb-2", tags=["pxweb-2"])
app.include_router(wfs.router, prefix="/wfs", tags=["wfs"])
app.include_router(arcgis.router, prefix="/arcgis", tags=["arcgis"])

# Myndighetskällor (samma klienter som doa-special / doa-katalog)
app.include_router(skolverket.router, prefix="/skolverket", tags=["skolverket"])
app.include_router(trafa.router, prefix="/trafa", tags=["trafa"])
app.include_router(
    socialstyrelsen.router, prefix="/socialstyrelsen", tags=["socialstyrelsen"]
)
app.include_router(bra.router, prefix="/bra", tags=["bra"])
app.include_router(forsakringskassan.router, prefix="/fk", tags=["forsakringskassan"])
app.include_router(kb_bibstat.router, prefix="/bibstat", tags=["kb-bibstat"])
app.include_router(riksbanken.router, prefix="/riksbanken", tags=["riksbanken"])

# Katalog/discovery
app.include_router(dcat.router, prefix="/dcat", tags=["dcat"])
app.include_router(researchdata.router, prefix="/researchdata", tags=["researchdata"])
