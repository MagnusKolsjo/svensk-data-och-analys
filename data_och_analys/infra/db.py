# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Backend-val mellan PostgreSQL och SQLite — symmetriska val.

Klienter och MCP-verktyg använder samma per-anrops-mönster oavsett backend::

    from data_och_analys.infra import db

    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            f"SELECT * FROM {db.prefix()}kommun WHERE kod = {db.ph(1)}",
            "0180",
        )

`ph(n)` returnerar rätt platshållare per backend ($N för Postgres, ? för
SQLite). `prefix()` returnerar schema-prefix för Postgres om DOA_DB_SCHEMA
är satt, och tom sträng för SQLite. `ar_postgres()` används när en operation
inte kan skrivas symmetriskt.

I http-läget hålls en connection pool öppen och delas mellan anrop. I stdio-
läget skapas en anslutning per anrop — det räcker eftersom MCP-klienten är
en process som serialiserar verktygsanropen.

Schemafiler ligger i projektets `db/`-katalog (filnamn `schema_*.sql`).
`initiera_schema()` läser dem alfabetiskt och kör dem idempotent.

Ingångspunkter:
    initiera_schema()                    -> None     (synkron — körs vid uppstart)
    hamta_db()                           -> async cm
    ar_postgres() / ph(n) / prefix()     -> hjälpare
    stang_pool()                         -> None     (för graceful shutdown i http)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ============================================================================
# Backend-val
# ============================================================================


def ar_postgres() -> bool:
    """True om vald backend är PostgreSQL.

    Läser DOA_DB_BACKEND. Tomt eller okänt värde tolkas som sqlite,
    eftersom det är det installationsvalet som inte kräver extern tjänst.
    """
    return os.getenv("DOA_DB_BACKEND", "sqlite").strip().lower() == "postgres"


def ph(n: int) -> str:
    """Returnerar parametrar-platshållare nummer `n` för aktuell backend."""
    return f"${n}" if ar_postgres() else "?"


def prefix() -> str:
    """Schema-prefix för tabellnamn — t.ex. 'statistik.' eller ''.

    Postgres-användare som vill samla projektets tabeller i ett eget schema
    sätter DOA_DB_SCHEMA=statistik. SQLite har inga scheman och
    returnerar alltid tom sträng.
    """
    if not ar_postgres():
        return ""
    schema = os.getenv("DOA_DB_SCHEMA", "").strip()
    return f"{schema}." if schema else ""


# ============================================================================
# Anslutningsadapter — gemensamt gränssnitt över asyncpg och aiosqlite
# ============================================================================
#
# asyncpg och aiosqlite har likartade men olika API:er. Adapterklassen
# nedan exponerar fyra metoder som båda backenderna stöder, så att
# klientkod kan skrivas mot ett gränssnitt utan if-satser per anrop.


class Anslutning:
    """Ett tunt gränssnitt över asyncpg-Connection eller aiosqlite-Connection."""

    def __init__(self, raw: Any, ar_postgres: bool) -> None:
        self._raw = raw
        self._pg = ar_postgres

    async def execute(self, sql: str, *parametrar: Any) -> None:
        if self._pg:
            await self._raw.execute(sql, *parametrar)
        else:
            await self._raw.execute(sql, parametrar)
            await self._raw.commit()

    async def executemany(self, sql: str, rader: list[tuple[Any, ...]]) -> None:
        if self._pg:
            await self._raw.executemany(sql, rader)
        else:
            await self._raw.executemany(sql, rader)
            await self._raw.commit()

    async def fetch(self, sql: str, *parametrar: Any) -> list[dict[str, Any]]:
        if self._pg:
            rader = await self._raw.fetch(sql, *parametrar)
            return [dict(r) for r in rader]
        cursor = await self._raw.execute(sql, parametrar)
        try:
            kolumner = [c[0] for c in cursor.description] if cursor.description else []
            rader = await cursor.fetchall()
            return [dict(zip(kolumner, r)) for r in rader]
        finally:
            await cursor.close()

    async def fetchrow(
        self, sql: str, *parametrar: Any
    ) -> dict[str, Any] | None:
        rader = await self.fetch(sql, *parametrar)
        return rader[0] if rader else None

    async def fetchval(self, sql: str, *parametrar: Any) -> Any:
        rad = await self.fetchrow(sql, *parametrar)
        if rad is None:
            return None
        return next(iter(rad.values()))


# ============================================================================
# Connection pool (http) eller per-anrop (stdio)
# ============================================================================


_pool: Any | None = None
_pool_las = asyncio.Lock()


def _anvander_pool() -> bool:
    """Pool används bara i http-transport där flera klienter kan vara parallella."""
    return os.getenv("DOA_MCP_TRANSPORT", "stdio").strip().lower() == "http"


def _sqlite_sokvag(db_url: str) -> str:
    """Plockar ut filsökvägen ur en sqlite-URL.

    Hanterar både `sqlite:///abs/path.db`, `sqlite:///relative.db` och en
    bar sökväg. För specialvärdet ":memory:" returneras det oförändrat.
    """
    if db_url == ":memory:":
        return db_url
    if db_url.startswith("sqlite:"):
        # sqlite:///path -> /path; sqlite://path -> path
        url = re.sub(r"^sqlite:/{2,3}", "", db_url)
        return url or ":memory:"
    return db_url


async def _oppna_pool() -> Any:
    """Skapar en pool för aktuell backend. Anropas under _pool_las."""
    db_url = os.getenv("DOA_DB_URL", "").strip()
    if not db_url:
        raise RuntimeError(
            "DOA_DB_URL saknas — sätt den i .env innan DB-anslutning."
        )

    if ar_postgres():
        import asyncpg
        # Modest pool — MCP-anrop är låg-samtidiga jämfört med en webbapp.
        return await asyncpg.create_pool(db_url, min_size=1, max_size=10)
    else:
        # SQLite har ingen riktig pool i aiosqlite. Vi använder en
        # semaphor + en delad anslutning i pool-läge.
        import aiosqlite
        anslutning = await aiosqlite.connect(_sqlite_sokvag(db_url))
        return anslutning  # behandlas som "pool av en"


async def _hamta_pool() -> Any:
    global _pool
    if _pool is None:
        async with _pool_las:
            if _pool is None:
                _pool = await _oppna_pool()
    return _pool


async def stang_pool() -> None:
    """Stänger den globala poolen (graceful shutdown i http-läget)."""
    global _pool
    if _pool is None:
        return
    if ar_postgres():
        await _pool.close()
    else:
        await _pool.close()
    _pool = None


@asynccontextmanager
async def hamta_db() -> AsyncIterator[Anslutning]:
    """Async context manager som ger en `Anslutning` per anrop.

    I http-läget hämtas en anslutning från poolen och lämnas tillbaka när
    blocket avslutas. I stdio-läget öppnas en anslutning per anrop och
    stängs efteråt.
    """
    db_url = os.getenv("DOA_DB_URL", "").strip()
    if not db_url:
        raise RuntimeError(
            "DOA_DB_URL saknas — sätt den i .env innan DB-anslutning."
        )

    pg = ar_postgres()

    if _anvander_pool():
        pool = await _hamta_pool()
        if pg:
            async with pool.acquire() as raw:
                yield Anslutning(raw, ar_postgres=True)
        else:
            # En delad aiosqlite-anslutning — serialiseras av asyncio
            # eftersom alla anrop blir på samma event-loop.
            yield Anslutning(pool, ar_postgres=False)
        return

    # Per-anrops-anslutning (stdio)
    if pg:
        import asyncpg
        raw = await asyncpg.connect(db_url)
        try:
            yield Anslutning(raw, ar_postgres=True)
        finally:
            await raw.close()
    else:
        import aiosqlite
        raw = await aiosqlite.connect(_sqlite_sokvag(db_url))
        try:
            yield Anslutning(raw, ar_postgres=False)
        finally:
            await raw.close()


# ============================================================================
# Schemainit
# ============================================================================


def _projekt_rot() -> Path:
    # db.py ligger i data_och_analys/infra/. Två steg upp = projektets rot.
    return Path(__file__).resolve().parents[2]


def _schema_filer() -> list[Path]:
    rot = _projekt_rot() / "db"
    if not rot.is_dir():
        return []
    return sorted(rot.glob("schema_*.sql"))


def _skala_bort_kommentar(rad: str) -> str:
    """Tar bort en radkommentar, men bara utanför stränglitteraler.

    Ett `--` inuti en sträng är data, inte en kommentar. Enkla citattecken
    escapas i SQL genom att dubbleras, och eftersom vi bara letar efter
    `--` räcker det att räkna dem: ett jämnt antal betyder att vi står
    utanför en sträng.
    """
    i, inne = 0, False
    while i < len(rad):
        tecken = rad[i]
        if tecken == "'":
            inne = not inne
        elif not inne and tecken == "-" and rad[i + 1:i + 2] == "-":
            return rad[:i]
        i += 1
    return rad


def _dela_satser(sql: str) -> list[str]:
    """Delar SQL-text på ';' med kommentarer bortskalade.

    Kommentarer måste bort före delningen, inte bara de som står först på
    raden. En efterföljande kommentar med semikolon i — `TEXT, -- NULL för
    riksrapporter; annars ...` — delar annars ett CREATE TABLE mitt itu,
    och Postgres svarar "syntax error at end of input" på en sats med
    oavslutad parentes. Felet är obehagligt eftersom tabellen tyst uteblir
    på en nyinstallation medan allt ser rätt ut i filen.

    Fortfarande naivt i ett avseende: dollar-quotade block ($$...$$) och
    funktionskroppar med interna semikolon skulle brytas. Schemafilerna
    håller sig till ren DDL, och den dagen de inte gör det behövs en
    riktig parser.
    """
    rader = []
    for rad in sql.splitlines():
        utan_kommentar = _skala_bort_kommentar(rad)
        if utan_kommentar.strip():
            rader.append(utan_kommentar.rstrip())
    sammanhängande = "\n".join(rader)
    return [s.strip() for s in sammanhängande.split(";") if s.strip()]


def initiera_schema() -> None:
    """Läser projektets schema_*.sql-filer och kör dem idempotent.

    Synkron med vilje — anropas i MCP-servrarnas __main__ före event-loopen
    startar, omslutet av try/except så att serverprocessen står kvar i
    MCP-klienten även om DB är nere.
    """
    filer = _schema_filer()
    if not filer:
        logger.info("Inga schemafiler hittade — hoppar över initiering")
        return

    async def _kor() -> None:
        async with hamta_db() as db:
            for fil in filer:
                sql = fil.read_text(encoding="utf-8")
                satser = _dela_satser(sql)
                logger.info("Initierar schema: %s (%d satser)", fil.name, len(satser))
                for sats in satser:
                    await db.execute(sats)

    asyncio.run(_kor())
