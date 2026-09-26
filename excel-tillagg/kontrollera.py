#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Kontrollerar Office-tilläggets JavaScript innan det driftsätts.

`node --check` fångar syntaxfel men inte ett anrop till en funktion som inte
finns. Ett tillägg laddas i Excels webbvy, och en ReferenceError syns där som
en tyst funktion som aldrig gör något — inte som ett felmeddelande.

Kontrollen gäller anrop på toppnivå, alltså namn som inte föregås av punkt.
Metodanrop på objekt kan inte avgöras statiskt och lämnas därför.

Ingångspunkter:
    kontrollera(sokvag) -> list[str]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

INBYGGT = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "new", "await",
    "function", "async", "fetch", "parseInt", "parseFloat", "isNaN", "setTimeout",
    "clearTimeout", "require", "encodeURIComponent", "decodeURIComponent",
    "alert", "console", "structuredClone", "queueMicrotask",
}


def _rensa(kalla: str) -> str:
    """Tar bort kommentarer och stränginnehåll.

    Utan det här flaggas svenska ord inne i strängar — "Välj minst ett värde
    per dimension (eller Alla)" ser ut som anropen dimension() och värden().
    """
    # Strängarna först. Tas radkommentarer före dem kapas varje rad som
    # innehåller "https://" mitt i strängen, och resten av raden tolkas som
    # kod — vilket gör svenska ord inuti strängar till falska anrop.
    kalla = re.sub(r"`(?:\\.|[^`\\])*`", "``", kalla)
    kalla = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', kalla)
    kalla = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", kalla)
    kalla = re.sub(r"/\*.*?\*/", " ", kalla, flags=re.S)
    kalla = re.sub(r"(?m)//.*$", " ", kalla)
    return kalla


def kontrollera(sokvag: Path) -> list[str]:
    kalla = _rensa(sokvag.read_text(encoding="utf-8"))
    definierade = set(re.findall(r"(?:async\s+)?function\s+(\w+)", kalla))
    definierade |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=", kalla))
    definierade |= set(re.findall(r"(\w+)\s*(?::|,)?\s*\(?[^)]*\)?\s*=>", kalla))
    anrop = set(re.findall(r"(?<![.\w$])([a-zåäö][\w$]*)\s*\(", kalla))
    return [
        f"{sokvag.name}: {a}() anropas men finns inte"
        for a in sorted(anrop) if a not in definierade and a not in INBYGGT
    ]


def main(argv: list[str]) -> int:
    rot = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent
    anmarkningar: list[str] = []
    for fil in sorted(rot.glob("*.js")):
        anmarkningar += kontrollera(fil)
    if anmarkningar:
        print("Kontrollen hittade fel:")
        for rad in anmarkningar:
            print(f"  - {rad}")
        return 1
    print("Kontroll ok.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
