# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Provar Lantmäteriets OGC Features-endpoint och rapporterar vad som fattas.

Lantmäteriets WSO2 accepterar enligt sitt eget 401-svar tre saker: en
OAuth-token, Basic-auth, eller en `ApiKey`-header. Vilken som fungerar
avgörs av hur applikationen är uppsatt i devportalen — inte av koden.
Skriptet provar det som är konfigurerat och säger vad svaret betyder,
i stället för att låta en synk falla på ett 403 långt senare.

Inga hemligheter skrivs ut. Skriptet rapporterar bara om en variabel är
satt, aldrig dess värde, så utdata går att klistra in var som helst.

Körs mot sviten venv:

    .venv/bin/python cli/prova_lantmateriet.py

Ingångspunkter:
    prova() -> int
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJEKT_ROT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJEKT_ROT))

from data_och_analys.infra import konfig  # noqa: E402
from data_och_analys.katalog_klienter import ogc_api_features as oaf  # noqa: E402

INSTANS = "lantmateriet_admin"
COLLECTION = "kommuner-2026"


KRAVT_SCOPE = "ogc-features:ngp.read"


def _satt(namn: str) -> str:
    return "satt" if os.getenv(namn, "").strip() else "TOM"


async def _visa_beviljat_scope(instans) -> None:
    """Frågar token-endpointen vilket scope den faktiskt beviljar.

    Ett 403 på items kan bero på två helt olika saker: prenumerationen
    saknas, eller token utfärdades utan det scope operationen kräver.
    WSO2 binder scopes till roller — begär man ett scope kontot inte har
    rätt till får man ändå en giltig token, fast med `default` i stället.
    Gatewayen svarar då 403, vilket ser identiskt ut från utsidan.

    Skriver ut scope, inte token.
    """
    import base64

    import httpx

    ck = os.getenv("LANTMATERIET_CONSUMER_KEY", "").strip()
    cs = os.getenv("LANTMATERIET_CONSUMER_SECRET", "").strip()
    if not (ck and cs):
        return

    print("\nToken-utfärdande")
    basic = base64.b64encode(f"{ck}:{cs}".encode()).decode()
    onskat = os.getenv("LANTMATERIET_OAUTH_SCOPES", "").strip() or KRAVT_SCOPE
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as klient:
            svar = await klient.post(
                instans.token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": onskat},
            )
    except Exception as fel:
        print(f"  token-anrop misslyckades: {type(fel).__name__}: {fel}")
        return

    if svar.status_code >= 400:
        print(f"  HTTP {svar.status_code}: {svar.text[:160]}")
        return

    kropp = svar.json()
    beviljat = kropp.get("scope", "(inget scope-fält i svaret)")
    print(f"  begärt scope   {onskat}")
    print(f"  beviljat scope {beviljat}")
    if KRAVT_SCOPE not in str(beviljat):
        print(f"  ⚠ {KRAVT_SCOPE} saknas i den utfärdade token — det förklarar 403.")
        print("    Scopet är rollstyrt i WSO2. Kontrollera i devportalen att")
        print("    ditt konto har rollen som ger Nationell Geodataplattform-")
        print("    läsning, eller använd basic_auth-vägen (se nedan).")


async def _prova_fardig_token() -> int:
    """Provar en token som redan hämtats manuellt ur portalen.

    Läser den ur miljövariabeln LM_TOKEN — inte ur .env. En access token
    lever en timme och hör inte hemma i konfigurationen; poängen med det
    här läget är att ta reda på om portalen kunde utfärda scopet, vilket
    i så fall betyder att rollen finns och att det vanliga
    client_credentials-flödet också fungerar.

    Kör så här, utan att token skrivs till någon fil:

        LM_TOKEN=<token> .venv/bin/python cli/prova_lantmateriet.py
    """
    import httpx

    token = os.environ["LM_TOKEN"].strip()
    print("\nProvar medskickad token (LM_TOKEN)")

    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as klient:
        # Introspektion visar scope utan att vi behöver gissa.
        ck = os.getenv("LANTMATERIET_CONSUMER_KEY", "").strip()
        cs = os.getenv("LANTMATERIET_CONSUMER_SECRET", "").strip()
        if ck and cs:
            import base64
            basic = base64.b64encode(f"{ck}:{cs}".encode()).decode()
            try:
                i = await klient.post(
                    "https://apimanager.lantmateriet.se/oauth2/introspect",
                    headers={"Authorization": f"Basic {basic}",
                             "Content-Type": "application/x-www-form-urlencoded"},
                    data={"token": token},
                )
                if i.status_code == 200:
                    j = i.json()
                    print(f"  aktiv:  {j.get('active')}")
                    print(f"  scope:  {j.get('scope')}")
                    print(f"  klient: {j.get('client_id', '')[:12]}…")
            except Exception as fel:
                print(f"  introspektion gick inte: {type(fel).__name__}")

        url = (
            "https://api.lantmateriet.se/ogc-features/v1/administrativ-indelning"
            f"/collections/{COLLECTION}/items?limit=1"
        )
        svar = await klient.get(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/geo+json"},
        )
        print(f"\n  items -> HTTP {svar.status_code}")
        if svar.status_code == 200:
            d = svar.json()
            f = (d.get("features") or [{}])[0]
            print("  ✓ FUNGERAR")
            print(f"  fält: {', '.join(list((f.get('properties') or {}))[:10])}")
            print("\n  Rollen finns alltså på kontot. Kör om utan LM_TOKEN —")
            print("  då ska client_credentials ge samma scope, och ingen")
            print("  token behöver läggas i .env.")
            return 0
        print(f"  {svar.text[:200]}")
        return 1


async def _prova_basic_auth() -> None:
    """Provar den andra vägen specen tillåter.

    API:ts OpenAPI anger `security: [{default: [ogc-features:ngp.read]},
    {basic_auth: []}]` — basic_auth är alltså ett fullgott alternativ och
    kräver inget scope alls. Värt att prova när scope-vägen stängs.
    """
    import base64

    import httpx

    ck = os.getenv("LANTMATERIET_CONSUMER_KEY", "").strip()
    cs = os.getenv("LANTMATERIET_CONSUMER_SECRET", "").strip()
    if not (ck and cs):
        return

    print("\n  Provar basic_auth-vägen (kräver inget scope):")
    url = (
        "https://api.lantmateriet.se/ogc-features/v1/administrativ-indelning"
        f"/collections/{COLLECTION}/items?limit=1"
    )
    basic = base64.b64encode(f"{ck}:{cs}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as klient:
            svar = await klient.get(
                url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/geo+json",
                },
            )
    except Exception as fel:
        print(f"    misslyckades: {type(fel).__name__}: {fel}")
        return

    print(f"    HTTP {svar.status_code}")
    if svar.status_code == 200:
        print("    ✓ basic_auth fungerar. Sätt i .env:")
        print("      DOA_OAFEAT_LANTMATERIET_ADMIN_AUTH_TYP=basic_auth")
    else:
        print(f"    {svar.text[:150]}")
        print("    Basic med consumer key/secret räcker inte heller — då är")
        print("    det rollen på kontot som behöver åtgärdas hos Lantmäteriet.")


async def prova() -> int:
    konfig.las_env()
    instans = oaf.hamta_instans(INSTANS)

    print("Konfiguration")
    print(f"  bas_url    {instans.bas_url}")
    print(f"  auth_typ   {instans.auth_typ}")
    for namn in ("LANTMATERIET_CONSUMER_KEY", "LANTMATERIET_CONSUMER_SECRET",
                 "LANTMATERIET_API_NYCKEL", "LANTMATERIET_OAUTH_SCOPES"):
        print(f"  {namn:30} {_satt(namn)}")

    if os.getenv("LM_TOKEN", "").strip():
        return await _prova_fardig_token()

    await _visa_beviljat_scope(instans)

    print("\nÖppna endpoints (kräver ingen autentisering)")
    try:
        samlingar = await oaf.lista_collections(INSTANS)
        ider = [c["id"] for c in samlingar.get("collections", [])]
        print(f"  collections  OK — {len(ider)} st: {', '.join(ider[:4])} …")
    except Exception as fel:
        print(f"  collections  MISSLYCKADES: {type(fel).__name__}: {fel}")
        return 1

    print(f"\nSkyddad endpoint ({COLLECTION}/items)")
    try:
        svar = await oaf.hamta_items(
            instans=INSTANS, collection_id=COLLECTION, limit=1
        )
        antal = len(svar.get("features", []))
        print(f"  items        OK — {antal} feature hämtad")
        if antal:
            falt = list((svar["features"][0].get("properties") or {}))
            print(f"  fält         {', '.join(falt[:10])}")
        print("\nKlart — synken kan köras.")
        return 0
    except Exception as fel:
        text = str(fel)
        print(f"  items        MISSLYCKADES: {type(fel).__name__}")
        print(f"  {text[:220]}")
        print("\nTolkning:")
        if "401" in text or "Missing Credentials" in text:
            print("  401 — inga credentials nådde fram. Fyll i")
            print("  LANTMATERIET_CONSUMER_KEY och _SECRET i .env, eller sätt")
            print("  DOA_OAFEAT_LANTMATERIET_ADMIN_AUTH_TYP=apikey och")
            print("  LANTMATERIET_API_NYCKEL om applikationen använder API-nyckel.")
        elif "403" in text:
            print("  403 — token är giltig men saknar behörighet. Se")
            print("  'beviljat scope' ovan: står det inte ogc-features:ngp.read")
            print("  där är scopet rollstyrt och kontot saknar rollen.")
            await _prova_basic_auth()
        elif "invalid_client" in text or "Unsupported client" in text:
            print("  Token-endpointen avvisade klienten — kontrollera att")
            print("  consumer key och secret hör ihop och är från samma")
            print("  application i devportalen.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(prova()))
