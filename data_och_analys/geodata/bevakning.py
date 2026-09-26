# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Bevakar om källorna publicerat något nyare än vad vi hämtat.

`synkstatus` svarar på hur gammal **vår** kopia är. Det räcker inte: nio av
nitton dataset är händelsestyrda och blir inaktuella först när myndigheten
publicerar en ny version, inte när tiden går. En åldersjämförelse kan inte
veta att SKR reviderat sin kommungruppsindelning.

Bevakningen jämför i stället **källans egen signatur** — ETag, Last-Modified
och storlek, som servern uppger i ett HEAD-anrop. Ändras någon av dem har
filen bytts ut. Det är robustare än att skrapa myndighetens webbsida efter
en ny rubrik, och fungerar likadant för alla källor.

Modulen triggar aldrig en synk. Den rapporterar vad som ändrats och vilket
verktyg som hämtar om det — beslutet att skriva till databasen är
användarens.

För dataset vars synk tar URL:en som parameter eller ur `.env` (SKR,
Tillväxtverket) finns ingen signatur förrän en synk körts med en URL. Det
redovisas som "ingen känd URL", inte som att allt är i ordning.

Ingångspunkter:
    KANDA_URLER
    notera_kalla(dataset, url) -> None
    kontrollera(dataset) -> list[dict]
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from data_och_analys.geodata.synkregister import REGISTER
from data_och_analys.infra import db

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
    "svensk-data-och-analys/Bevakning"
)
_TIMEOUT = httpx.Timeout(45.0, connect=15.0)

# Dataset vars synk har en fast URL i koden. Övriga får sin signatur först
# när en synk körts med en URL — de flesta klassificeringskällorna tar den
# som parameter eller ur .env, eftersom myndigheterna byter sökväg vid varje
# publikation.
KANDA_URLER: dict[str, str] = {
    "kommun_folkmangd": (
        "https://www.scb.se/contentassets/"
        "ef676b89b44d4bcbacce5b2d399065cd/be0101_folkmangdkom2024.xlsx"
    ),
}


async def notera_kalla(dataset: str, url: str) -> None:
    """Sparar källans signatur. Anropas efter en lyckad hämtning."""
    sig = await _signatur(url)
    if sig is None:
        return
    async with db.hamta_db() as anslutning:
        await anslutning.execute(
            f"INSERT INTO {db.prefix()}kallsignatur "
            f"(dataset, url, etag, last_modified, storlek) "
            f"VALUES ($1, $2, $3, $4, $5) "
            f"ON CONFLICT (dataset) DO UPDATE SET "
            f"url = EXCLUDED.url, etag = EXCLUDED.etag, "
            f"last_modified = EXCLUDED.last_modified, "
            f"storlek = EXCLUDED.storlek, senast_kontrollerad = NOW()",
            dataset, url, sig.get("etag"), sig.get("last_modified"),
            sig.get("storlek"),
        )


async def _signatur(url: str) -> dict[str, Any] | None:
    """HEAD:ar en URL och plockar ut det servern uppger om filen."""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as klient:
            svar = await klient.head(url)
            # Vissa servrar svarar inte på HEAD. Ett GET med Range på en byte
            # ger samma huvuden utan att ladda hem filen.
            if svar.status_code in (405, 501):
                svar = await klient.get(url, headers={"Range": "bytes=0-0"})
    except Exception as fel:  # noqa: BLE001
        logger.warning("Kunde inte kontrollera %s: %s", url, fel)
        return None
    if svar.status_code >= 400:
        logger.warning("%s svarade %s", url, svar.status_code)
        return None
    langd = svar.headers.get("content-range") or svar.headers.get("content-length")
    if langd and "/" in str(langd):
        langd = str(langd).rsplit("/", 1)[-1]
    return {
        "etag": svar.headers.get("etag"),
        "last_modified": svar.headers.get("last-modified"),
        "storlek": int(langd) if langd and str(langd).isdigit() else None,
    }


def _skiljer(gammal: dict[str, Any], ny: dict[str, Any]) -> list[str]:
    """Vilka fält som ändrats. Tomma fält jämförs inte — en server som
    slutat skicka ETag har inte publicerat om filen."""
    andrat = []
    for falt in ("etag", "last_modified", "storlek"):
        g, n = gammal.get(falt), ny.get(falt)
        if g is not None and n is not None and g != n:
            andrat.append(falt)
    return andrat


async def kontrollera(dataset: str | None = None) -> list[dict[str, Any]]:
    """Kontrollerar källorna och rapporterar vad som ändrats.

    Skriver aldrig till någon datatabell — bara till signaturen, så att
    nästa kontroll jämför mot det som finns nu.
    """
    async with db.hamta_db() as anslutning:
        lagrade = {
            r["dataset"]: dict(r)
            for r in await anslutning.fetch(
                f"SELECT * FROM {db.prefix()}kallsignatur"
            )
        }

    valda = [dataset] if dataset else sorted(set(KANDA_URLER) | set(lagrade))
    resultat: list[dict[str, Any]] = []

    for namn in valda:
        post = REGISTER.get(namn)
        url = (lagrade.get(namn) or {}).get("url") or KANDA_URLER.get(namn)
        if not url:
            resultat.append({
                "dataset": namn, "lage": "ingen känd URL",
                "verktyg": post.verktyg if post else None,
                "rad": "Kör synken en gång med en URL — då kan källan bevakas.",
            })
            continue

        ny = await _signatur(url)
        if ny is None:
            resultat.append({
                "dataset": namn, "url": url, "lage": "nås inte",
                "verktyg": post.verktyg if post else None,
                "rad": "Källan svarade inte. Det kan betyda flyttad fil.",
            })
            continue

        gammal = lagrade.get(namn)
        if gammal is None:
            await notera_kalla(namn, url)
            resultat.append({
                "dataset": namn, "url": url, "lage": "första kontrollen",
                "verktyg": post.verktyg if post else None,
                "rad": "Signaturen sparad; nästa kontroll kan jämföra.",
            })
            continue

        andrat = _skiljer(gammal, ny)
        # Signaturen uppdateras INTE här. Gjorde den det skulle larmet
        # kvitteras av att man tittade på det, och försvinna innan någon
        # hunnit hämta in ändringen. Den skrivs om först när en synk faktiskt
        # körts, via notera_kalla — då är kopian i fas med källan igen.
        async with db.hamta_db() as anslutning:
            await anslutning.execute(
                f"UPDATE {db.prefix()}kallsignatur SET senast_kontrollerad = NOW()"
                + (", senast_andrad = coalesce(senast_andrad, NOW())" if andrat else "")
                + " WHERE dataset = $1",
                namn,
            )
        resultat.append({
            "dataset": namn, "url": url,
            "lage": "ändrad hos källan" if andrat else "oförändrad",
            "andrade_falt": andrat,
            "verktyg": post.verktyg if post else None,
            "rad": (
                f"Källan har publicerat om filen ({', '.join(andrat)}). "
                f"Kör {post.verktyg} för att hämta in den."
                if andrat and post else
                "Källan har publicerat om filen." if andrat else
                "Ingen förändring sedan förra kontrollen."
            ),
        })
    return resultat
