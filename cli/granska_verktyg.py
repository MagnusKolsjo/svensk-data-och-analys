# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Granskar verktygsmetadata över hela doa-suiten.

Ett svep över drygt hundra verktyg i tolv filer går inte att kontrollera
för hand.
Skriptet laddar varje MCP-server, kör `list_tools()` och kontrollerar det
som annars tyst faller bort: verktyg utan `title`, utan `annotations`, utan
`outputSchema`, och servrar utan `instructions`.

Det rapporterar också hur många verktyg som är skrivande. Det talet ska
matcha förväntan — ett skrivverktyg som råkat klassas som läsning syns inte
på något annat sätt än här.

Körs mot den venv som faktiskt kör servrarna:

    .venv/bin/python cli/granska_verktyg.py

Avslutar med kod 1 om något saknas, så att det går att koppla på en
deploy-kontroll.

Ingångspunkter:
    granska() -> int
"""

from __future__ import annotations

import asyncio
import builtins
import symtable
import importlib.util
import sys
from pathlib import Path

PROJEKT_ROT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJEKT_ROT))

# Antalet skrivande verktyg är känt och ska inte ändras av misstag. Ändras
# det avsiktligt uppdateras talet här — annars är avvikelsen en bugg.
FORVANTADE_SKRIVVERKTYG = 19


def _falt(objekt, *namn):
    """Läser första fältet som finns — camelCase i mcp 1.x, snake_case i 2.x.

    SDK:t bytte till snake_case på Python-sidan i 2.0 (`readOnlyHint` →
    `read_only_hint`) medan wire-formatet är oförändrat. Skriptet ska gå
    att köra mot båda venv:arna under migreringen.
    """
    for n in namn:
        if hasattr(objekt, n):
            return getattr(objekt, n)
    return None


def _odefinierade_namn(sokvag: Path) -> list[str]:
    """Hittar namn en funktion läser utan att de finns någonstans.

    Modulen laddas bara vid metadatakontrollen nedan — verktygen anropas
    aldrig. Ett verktyg kan därför se korrekt ut i `list_tools()` och ändå
    kasta NameError vid första anropet. Det inträffar i praktiken när en
    funktionskropp skrivs om men signaturen inte följer med: kroppen
    börjar läsa `sida` eller `rapportera` innan parametern finns.

    Scope-analysen görs med `symtable`, inte för hand. En hemsnickrad
    AST-vandring missar comprehension-variabler och stängningar över
    omslutande funktioners parametrar, och rapporterar dem som fel —
    `symtable` är CPython:s egen analys och får dem rätt.

    Ett namn flaggas när det är globalt refererat i en funktion men
    varken finns på modulnivå eller bland inbyggda.
    """
    kalla = sokvag.read_text()
    try:
        rot = symtable.symtable(kalla, str(sokvag), "exec")
    except SyntaxError as fel:
        return [f"kan inte parsas: {fel}"]

    modulnamn = {s.get_name() for s in rot.get_symbols()
                 if s.is_assigned() or s.is_imported()}
    # Dundrar som alltid finns i en modul men inte syns som tilldelningar.
    kanda = modulnamn | set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
    }

    fel: list[str] = []

    def ga_igenom(tabell) -> None:
        if tabell.get_type() == "function":
            for symbol in tabell.get_symbols():
                namn = symbol.get_name()
                if (symbol.is_global() and symbol.is_referenced()
                        and namn not in kanda):
                    fel.append(
                        f"{tabell.get_name()} läser odefinierat namn '{namn}'"
                    )
        for barn in tabell.get_children():
            ga_igenom(barn)

    ga_igenom(rot)
    return sorted(set(fel))


def _kod_efter_main(sokvag: Path) -> list[str]:
    """Hittar kod som ligger efter `if __name__ == "__main__":`.

    Skriptet importerar servern för att läsa dess metadata, och vid import
    hoppas __main__-blocket över — all kod därefter körs alltså. Startas
    samma fil som ett skript blockerar `starta()` i blocket, och tolken når
    aldrig raderna under. Verktyg och resurser som registrerats där finns
    då i granskningen men saknas hos klienten.

    Felet är svårt att se: metadatan ser komplett ut, servern startar, och
    först ett anrop över tråden avslöjar att verktyget inte finns.
    """
    rader = sokvag.read_text().splitlines()
    try:
        i = next(n for n, r in enumerate(rader)
                 if r.startswith('if __name__ == "__main__":'))
    except StopIteration:
        return []
    efter = [(n, r) for n, r in enumerate(rader[i + 1:], start=i + 2)
             if r.strip() and not r.startswith((" ", "\t", "#"))]
    if not efter:
        return []
    n, r = efter[0]
    return [f"kod på rad {n} ligger efter __main__ och körs aldrig av servern "
            f"({r.strip()[:40]}…)"]


def _ladda_server(sokvag: Path):
    """Laddar en MCP-serverfil och returnerar dess `mcp`-instans."""
    spec = importlib.util.spec_from_file_location(sokvag.stem, sokvag)
    if spec is None or spec.loader is None:
        raise ImportError(f"Kunde inte ladda {sokvag}")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul.mcp


async def granska() -> int:
    filer = sorted((PROJEKT_ROT / "mcp_servrar").glob("*_mcp.py"))
    anmarkningar: list[str] = []
    totalt_verktyg = 0
    totalt_skrivande = 0

    # Namnkontrollen körs över hela kärnbiblioteket, inte bara servrarna.
    # Buggen den fångar — kropp omskriven utan att signaturen följde med —
    # uppstår lika gärna i en synkfunktion som i ett verktyg.
    for fil in sorted((PROJEKT_ROT / "data_och_analys").rglob("*.py")):
        for rad in _odefinierade_namn(fil):
            anmarkningar.append(f"{fil.relative_to(PROJEKT_ROT)}: {rad}")

    for fil in filer:
        mcp = _ladda_server(fil)
        verktyg = await mcp.list_tools()
        totalt_verktyg += len(verktyg)

        for rad in _odefinierade_namn(fil) + _kod_efter_main(fil):
            anmarkningar.append(f"{fil.name}: {rad}")

        if not mcp.instructions:
            anmarkningar.append(f"{fil.name}: saknar instructions=")

        skrivande = 0
        for t in verktyg:
            if not t.title:
                anmarkningar.append(f"{fil.name}:{t.name} saknar title")
            if not t.annotations:
                anmarkningar.append(f"{fil.name}:{t.name} saknar annotations")
            elif not _falt(t.annotations, 'read_only_hint', 'readOnlyHint'):
                skrivande += 1
            # huwise_exportera stänger av strukturerad utdata med avsikt —
            # se kommentaren vid verktyget. Övriga ska ha ett schema.
            if (_falt(t, 'output_schema', 'outputSchema') is None
                    and t.name != "huwise_exportera"):
                anmarkningar.append(f"{fil.name}:{t.name} saknar outputSchema")

        totalt_skrivande += skrivande
        print(f"{fil.stem:26} verktyg={len(verktyg):3}  skrivande={skrivande:2}")

    print(f"\nTotalt: {totalt_verktyg} verktyg i {len(filer)} servrar, "
          f"varav {totalt_skrivande} skrivande")

    if totalt_skrivande != FORVANTADE_SKRIVVERKTYG:
        anmarkningar.append(
            f"antalet skrivverktyg är {totalt_skrivande}, "
            f"förväntat {FORVANTADE_SKRIVVERKTYG}"
        )

    if anmarkningar:
        print(f"\n{len(anmarkningar)} anmärkningar:")
        for rad in anmarkningar:
            print(f"  - {rad}")
        return 1

    print("Inga anmärkningar.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(granska()))
