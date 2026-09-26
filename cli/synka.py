# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Schemalagd synk av det som lagras lokalt.

Synkregistret anger för varje lagrat dataset hur ofta det ändras och varför
takten är satt så. Den här körningen gör registret till handling, i tre steg:

  kallor   Bevakningen frågar källorna om filerna bytts ut. Ett händelsestyrt
           dataset — kommunindelning, valdistrikt, klassificeringar — blir
           inaktuellt när källan publicerar, inte när tiden går, och synkas
           om från den kända adressen när signaturen ändrats. En ny årgång
           på en ny adress läggs in i `.env` (`DOA_*_URL`); skiljer den sig
           från den bevakade adressen synkas den nya. Källor som ännu inte
           bevakas redovisas.
  takt     Dataset med uppmätt, dokumenterad eller antagen takt synkas när
           åldern passerat takten: skolenheter, postnummer, folkmängd,
           sökindex och katalogregistret.
  stadning Mellanlagret rensas från det som passerat sin hållbarhet.

Stegen är oberoende och körs även om ett tidigare felat, men körningen
avslutas med kod 1 om något felade — så syns det i loggen och för
schemaläggaren. Dataset som aldrig synkats rörs inte: den första synken är
en installation, inte underhåll (se README).

    python cli/synka.py                     # det som behöver synkas
    python cli/synka.py --steg kallor       # ett steg
    python cli/synka.py --visa              # vad som skulle köras
    python cli/synka.py --installera-schema # daglig körning via launchd/cron

Ingångspunkter:
    main() -> int
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from data_och_analys.geodata import bevakning, synk, synkstatus  # noqa: E402
from data_och_analys.infra import db, konfig, mellanlager  # noqa: E402

logger = logging.getLogger("synka")

STEG = ("kallor", "takt", "stadning")
STANDARD_SCHEMA = "45 4 * * *"
STANDARD_ETIKETT = "com.magnuskolsjo.doa-synk"


# ---------------------------------------------------------------------------
# Steg 1 — källor som bytt fil
# ---------------------------------------------------------------------------

async def _kor_synk(synk_id: str, url: str | None, visa: bool, fel: list[str]) -> None:
    if visa:
        return
    try:
        svar = await synk.kor(synk_id, url=url)
        logger.info("%s: klar — %s", synk_id, svar["resultat"])
    except Exception as e:  # noqa: BLE001 — ett dataset får inte stoppa de andra
        logger.error("%s: synken felade: %s", synk_id, e)
        fel.append(synk_id)


async def steg_kallor(visa: bool) -> list[str]:
    fel: list[str] = []
    korda: set[str] = set()
    kontroller = await bevakning.kontrollera()
    bevakade = {r["dataset"]: r.get("url") for r in kontroller}

    # En ny årgång på en ny adress syns inte hos källan — den gamla filen
    # ligger kvar oförändrad. Adressen i .env är därför det som avgör: skiljer
    # den sig från den adress signaturen sattes från hämtas den nya.
    for synk_id, s in synk.SYNKAR.items():
        if not s.url_env:
            continue
        ny = os.getenv(s.url_env, "").strip()
        sparade = {bevakade[d] for d in s.dataset if bevakade.get(d)}
        if not ny or not sparade or sparade == {ny}:
            continue
        logger.info("%s: ny adress i %s — synkar från den", synk_id, s.url_env)
        korda.add(synk_id)
        await _kor_synk(synk_id, ny, visa, fel)

    # En källa utan signatur syns inte i bevakningen alls. Tystnaden får inte
    # se ut som "oförändrad": finns en adress i .env synkas den en gång, så
    # att signaturen sätts; annars redovisas att adressen saknas.
    for synk_id, s in synk.SYNKAR.items():
        if not s.tar_url or set(bevakade) & set(s.dataset):
            continue
        url = synk.los_url(synk_id)
        if not url:
            logger.warning("%s: bevakas inte — ange adressen i %s, så synkar nästa körning därifrån", synk_id, s.url_env)
            continue
        logger.info("%s: ingen signatur än — synkar från %s så att bevakningen kan börja",
                    synk_id, s.url_env or "standardadressen")
        korda.add(synk_id)
        await _kor_synk(synk_id, url, visa, fel)

    for r in kontroller:
        dataset, lage = r["dataset"], r["lage"]
        if lage == "ändrad hos källan":
            synk_id = synk.synk_for_dataset(dataset)
            if not synk_id:
                logger.warning("%s: källan har bytt fil men har ingen automatisk synk — kör %s",
                               dataset, r.get("verktyg"))
                continue
            if synk_id in korda:
                continue
            korda.add(synk_id)
            logger.info("%s: källan har bytt fil (%s) — synkar %s",
                        dataset, ", ".join(r.get("andrade_falt") or []), synk_id)
            await _kor_synk(synk_id, r["url"], visa, fel)
        elif lage == "nås inte":
            logger.warning("%s: källan svarar inte på %s — filen kan ha flyttats", dataset, r.get("url"))
        elif lage == "ingen känd URL":
            logger.info("%s: ingen känd adress att bevaka — synka manuellt med %s", dataset, r.get("verktyg"))
    return fel


# ---------------------------------------------------------------------------
# Steg 2 — dataset med takt
# ---------------------------------------------------------------------------

def _skript(*args: str) -> None:
    """Kör ett av repots skript med samma tolk. Utdata går till loggen."""
    resultat = subprocess.run([sys.executable, str(REPO / args[0]), *args[1:]], cwd=REPO)
    if resultat.returncode != 0:
        raise RuntimeError(f"{args[0]} avslutades med kod {resultat.returncode}")


async def _synka_takt(dataset: str) -> None:
    if dataset == "skolenhet_punkt":
        # Hela registret om: ändringarna är ~10 % i månaden och syns inte i
        # ett pass som bara hämtar nya enheter. Geokodningen tar sedan de
        # enheter som saknar koordinat.
        _skript("cli/synka_skolenheter.py", "--om")
        _skript("cli/geokoda_skolenheter.py")
    elif dataset == "sok_index":
        from data_och_analys.sok import index
        resultat = await index.bygg_index()
        await synkstatus.notera_synk("sok_index", None, f"{len(resultat)} källor")
        _skript("cli/synka_geo_fasetter.py")
    elif dataset == "datakatalog":
        _skript("cli/kartlagg_datadelning.py")
    else:
        synk_id = synk.synk_for_dataset(dataset)
        if not synk_id:
            raise RuntimeError(f"{dataset} har ingen automatisk synk")
        await synk.kor(synk_id)


async def steg_takt(visa: bool) -> list[str]:
    fel: list[str] = []
    for s in await synkstatus.las_status():
        if s["kadens_dagar"] is None or s["kadens_grund"] == "händelsestyrd":
            continue
        if not s["antal_rader"]:
            logger.info("%s: aldrig synkad — den första synken görs vid installationen", s["dataset"])
            continue
        alder = s["alder_dagar"]
        if alder is not None and alder <= s["kadens_dagar"]:
            continue
        logger.info("%s: %s, takt %s dagar (%s) — synkar",
                    s["dataset"], "okänd ålder" if alder is None else f"{alder} dagar gammal",
                    s["kadens_dagar"], s["kadens_grund"])
        if visa:
            continue
        try:
            await _synka_takt(s["dataset"])
            logger.info("%s: klar", s["dataset"])
        except Exception as e:  # noqa: BLE001 — ett dataset får inte stoppa de andra
            logger.error("%s: synken felade: %s", s["dataset"], e)
            fel.append(s["dataset"])
    return fel


# ---------------------------------------------------------------------------
# Steg 3 — städning
# ---------------------------------------------------------------------------

async def steg_stadning(visa: bool) -> list[str]:
    if visa:
        logger.info("mellanlager: rensar det som är äldre än %d dagar", mellanlager.hallbarhet_dagar())
        return []
    try:
        antal = await mellanlager.rensa()
        logger.info("mellanlager: %d poster borttagna", antal)
        return []
    except Exception as e:  # noqa: BLE001
        logger.error("mellanlager: rensningen felade: %s", e)
        return ["mellanlager"]


# ---------------------------------------------------------------------------
# Schemaläggning
# ---------------------------------------------------------------------------

def installera_schema() -> int:
    """Daglig körning av synk/daglig_synk.sh via launchd (macOS) eller cron.

    launchd kör ett missat jobb när datorn vaknar; cron hoppar över det.
    På en dator som sover nattetid är launchd därför det som faktiskt körs.
    """
    schemalaggare = os.getenv("DOA_SYNK_SCHEMALAGGARE", "").strip().lower() or (
        "launchd" if platform.system() == "Darwin" else "cron")
    schema = os.getenv("DOA_SYNK_SCHEMA", "").strip() or STANDARD_SCHEMA
    wrapper = REPO / "synk" / "daglig_synk.sh"
    delar = schema.split()
    if len(delar) != 5 or not (delar[0].isdigit() and delar[1].isdigit()):
        print(f"DOA_SYNK_SCHEMA={schema!r} ska vara 'minut timme * * *', t.ex. {STANDARD_SCHEMA!r}")
        return 1

    if schemalaggare == "cron":
        rad = f"{schema} /bin/bash {wrapper}"
        befintlig = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if str(wrapper) in befintlig:
            print("  cron-raden finns redan")
            return 0
        subprocess.run(["crontab", "-"], input=befintlig + rad + "\n", text=True, check=True)
        print(f"  cron: {rad}")
        return 0
    if schemalaggare != "launchd":
        print(f"DOA_SYNK_SCHEMALAGGARE={schemalaggare!r} — välj launchd eller cron")
        return 1

    etikett = os.getenv("DOA_SYNK_ETIKETT", "").strip() or STANDARD_ETIKETT
    plist = Path.home() / "Library" / "LaunchAgents" / f"{etikett}.plist"
    loggar = REPO / "logs"
    loggar.mkdir(exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{etikett}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{wrapper}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>{int(delar[1])}</integer>
        <key>Minute</key><integer>{int(delar[0])}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>{loggar}/launchd-stdout.log</string>
    <key>StandardErrorPath</key><string>{loggar}/launchd-stderr.log</string>
</dict>
</plist>
""", encoding="utf-8")
    # bootout + bootstrap: launchctl kickstart läser inte om en ändrad plist.
    # bootout returnerar innan tjänsten är borta, och en bootstrap under
    # tiden felar med "5: Input/output error" — vänta ut den.
    domän = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domän}/{etikett}"], capture_output=True)
    for _ in range(20):
        if subprocess.run(["launchctl", "print", f"{domän}/{etikett}"],
                          capture_output=True).returncode != 0:
            break
        time.sleep(0.5)
    subprocess.run(["launchctl", "bootstrap", domän, str(plist)], check=True)
    print(f"  launchd: {plist}")
    print(f"  Körs dagligen {int(delar[1]):02d}:{int(delar[0]):02d}; loggar i {loggar}")
    return 0


# ---------------------------------------------------------------------------
# Körning
# ---------------------------------------------------------------------------

async def _kor(steg: list[str], visa: bool) -> int:
    fel: list[str] = []
    for namn in steg:
        logger.info("--- steg: %s%s", namn, " (visa)" if visa else "")
        fel += await {"kallor": steg_kallor, "takt": steg_takt, "stadning": steg_stadning}[namn](visa)
    if fel:
        logger.error("Klart med fel: %s", ", ".join(fel))
        return 1
    logger.info("Klart")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--steg", choices=STEG, action="append", help="Kör bara detta steg (kan upprepas)")
    p.add_argument("--visa", action="store_true", help="Visa vad som skulle synkas, utan att synka")
    p.add_argument("--installera-schema", action="store_true",
                   help="Installera daglig körning via launchd (macOS) eller cron")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for brusig in ("httpx", "httpcore"):
        logging.getLogger(brusig).setLevel(logging.WARNING)
    konfig.las_env()
    if args.installera_schema:
        return installera_schema()
    # Synken behöver databasen — till skillnad från MCP-servrarna, som ska gå
    # upp även när den är nere. Ett fel här ska synas direkt.
    db.initiera_schema()
    return asyncio.run(_kor(args.steg or list(STEG), args.visa))


if __name__ == "__main__":
    sys.exit(main())
