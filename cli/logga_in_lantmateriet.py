# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Inloggning mot Lantmäteriet med Authorization Code + PKCE.

Lantmäteriets rollstyrda scopes går inte att nå med client_credentials.
En sådan token bär `aut: APPLICATION` och har ingen användare bakom sig,
medan scopet `ogc-features:ngp.read` kräver rollen `LMKE/ak001750_Prod_R`
— och roller sitter på användare. Servern svarar därför `scope: default`
oavsett vilka prenumerationer applikationen har.

Authorization Code ger en `APPLICATION_USER`-token som bär inloggarens
roller. Till skillnad från password grant passerar lösenordet aldrig den
här koden: användaren loggar in hos Lantmäteriet i webbläsaren, och vi
får bara en engångskod tillbaka. PKCE binder koden till den här
processen så att den inte kan lösas in av någon annan som snappar upp
omdirigeringen.

Token hålls i minnet för körningen och skrivs aldrig till disk. Det är
avsiktligt: en access token lever en timme och hör inte hemma i
konfigurationen. `.env` behöver bara consumer key och secret, som
kodväxlingen ändå kräver eftersom applikationen är konfidentiell.

Förutsättningar i devportalen:
    Grant types    Code och Refresh Token ikryssade
    Callback URL   samma som DOA_LM_CALLBACK_URL nedan

Körs så här:

    .venv/bin/python cli/logga_in_lantmateriet.py

Ingångspunkter:
    logga_in() -> str          — returnerar access token
    main() -> int              — loggar in och provar API:t
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

PROJEKT_ROT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJEKT_ROT))

from data_och_analys.infra import konfig  # noqa: E402

AUKTORISERING = "https://apimanager.lantmateriet.se/oauth2/authorize"
TOKEN = "https://apimanager.lantmateriet.se/oauth2/token"
STANDARD_CALLBACK = "http://localhost:8765/callback"

# Scopet som ger kommun- och länsgeometri. `openid` behövs för att WSO2
# ska utfärda en användarkontext över huvud taget.
STANDARD_SCOPE = "openid ogc-features:ngp.read"

# Hur länge vi väntar på att användaren loggar in innan vi ger upp.
VANTETID_SEKUNDER = 300


class _Mottagare(http.server.BaseHTTPRequestHandler):
    """Tar emot omdirigeringen och plockar ut koden.

    Servern lever bara tills koden kommit — den lyssnar på loopback och
    accepterar ett enda anrop.
    """

    kod: str | None = None
    state: str | None = None
    fel: str | None = None

    def do_GET(self) -> None:  # noqa: N802  (namnet krävs av basklassen)
        fraga = urllib.parse.urlparse(self.path).query
        param = urllib.parse.parse_qs(fraga)
        _Mottagare.kod = (param.get("code") or [None])[0]
        _Mottagare.state = (param.get("state") or [None])[0]
        _Mottagare.fel = (param.get("error_description") or param.get("error")
                          or [None])[0]

        lyckades = _Mottagare.kod is not None
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        rubrik = "Inloggningen klar" if lyckades else "Inloggningen misslyckades"
        text = ("Du kan stänga fliken och gå tillbaka till terminalen."
                if lyckades else f"Fel: {_Mottagare.fel}")
        self.wfile.write(
            f"<!doctype html><meta charset='utf-8'>"
            f"<body style='font-family:system-ui;padding:3rem'>"
            f"<h2>{rubrik}</h2><p>{text}</p></body>".encode()
        )

    def log_message(self, *_args) -> None:
        """Tystar http.server — den skriver annars till stderr."""


def _pkce() -> tuple[str, str]:
    """Skapar code_verifier och tillhörande S256-challenge."""
    verifierare = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifierare.encode("ascii")).digest()
    utmaning = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifierare, utmaning


async def logga_in(scope: str | None = None) -> str:
    """Kör hela flödet och returnerar en access token.

    Kastar RuntimeError med ett läsbart svenskt fel om något går fel —
    saknade creds, avbruten inloggning, eller ett scope som inte
    beviljades.
    """
    import httpx

    konfig.las_env()
    ck = os.getenv("LANTMATERIET_CONSUMER_KEY", "").strip()
    cs = os.getenv("LANTMATERIET_CONSUMER_SECRET", "").strip()
    if not (ck and cs):
        raise RuntimeError(
            "LANTMATERIET_CONSUMER_KEY och _SECRET saknas i .env — de behövs "
            "även för Authorization Code eftersom applikationen är konfidentiell"
        )

    callback = os.getenv("DOA_LM_CALLBACK_URL", STANDARD_CALLBACK).strip()
    scope = scope or os.getenv("LANTMATERIET_OAUTH_SCOPES", "").strip() or STANDARD_SCOPE
    delar = urllib.parse.urlparse(callback)
    port = delar.port or 80

    verifierare, utmaning = _pkce()
    state = secrets.token_urlsafe(24)

    _Mottagare.kod = _Mottagare.state = _Mottagare.fel = None
    server = http.server.HTTPServer(("127.0.0.1", port), _Mottagare)
    tråd = threading.Thread(target=server.serve_forever, daemon=True)
    tråd.start()

    url = AUKTORISERING + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": ck,
        "redirect_uri": callback,
        "scope": scope,
        "state": state,
        "code_challenge": utmaning,
        "code_challenge_method": "S256",
    })
    print(f"Öppnar webbläsaren för inloggning hos Lantmäteriet.")
    print(f"  callback: {callback}")
    print(f"  scope:    {scope}")
    print("\nGår den inte upp av sig själv, öppna denna URL:")
    print(f"  {url}\n")
    webbrowser.open(url)

    try:
        for _ in range(VANTETID_SEKUNDER * 10):
            if _Mottagare.kod or _Mottagare.fel:
                break
            await asyncio.sleep(0.1)
    finally:
        server.shutdown()

    if _Mottagare.fel:
        raise RuntimeError(f"Lantmäteriet nekade inloggningen: {_Mottagare.fel}")
    if not _Mottagare.kod:
        raise RuntimeError(
            f"ingen kod mottagen inom {VANTETID_SEKUNDER} sekunder — avbröts "
            "inloggningen, eller pekar callback-URL:en i portalen någon annanstans?"
        )
    # Skyddar mot att någon annan lurar in en kod i vår lyssnare.
    if _Mottagare.state != state:
        raise RuntimeError("state stämmer inte — avbryter, svaret kan vara förfalskat")

    basic = base64.b64encode(f"{ck}:{cs}".encode()).decode()
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as klient:
        svar = await klient.post(
            TOKEN,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": _Mottagare.kod,
                "redirect_uri": callback,
                "code_verifier": verifierare,
            },
        )
    if svar.status_code >= 400:
        raise RuntimeError(f"kodväxlingen misslyckades ({svar.status_code}): "
                           f"{svar.text[:200]}")

    kropp = svar.json()
    beviljat = kropp.get("scope", "")
    print(f"Token utfärdad. Beviljat scope: {beviljat}")
    if "ogc-features:ngp.read" not in beviljat:
        print("  ⚠ ogc-features:ngp.read saknas — kontot har inte rollen")
        print("    LMKE/ak001750_Prod_R. Inloggningen fungerade, men")
        print("    behörigheten till Nationell Geodataplattform gör det inte.")
    return kropp["access_token"]


async def main() -> int:
    import httpx

    try:
        token = await logga_in()
    except RuntimeError as fel:
        print(f"\nFEL: {fel}")
        return 1

    url = ("https://api.lantmateriet.se/ogc-features/v1/administrativ-indelning"
           "/collections/kommuner-2026/items?limit=1")
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as klient:
        svar = await klient.get(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/geo+json"})
    print(f"\nProvanrop mot kommuner-2026/items -> HTTP {svar.status_code}")
    if svar.status_code != 200:
        print(f"  {svar.text[:200]}")
        return 1

    d = svar.json()
    f = (d.get("features") or [{}])[0]
    print("  ✓ Åtkomst fungerar")
    print(f"  fält: {', '.join(list((f.get('properties') or {}))[:10])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
