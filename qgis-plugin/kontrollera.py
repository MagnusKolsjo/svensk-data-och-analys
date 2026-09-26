#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Kontrollerar pluginet innan det paketeras.

`compileall` fångar syntaxfel men inte ett anrop till en metod som inte
finns. QGIS laddar pluginet först när användaren klickar på knappen, och
felet syns då som en traceback i loggpanelen långt efter att zipen byggts.

Kontrollen gäller bara egna hjälpmetoder — de som inleds med understreck.
Qt:s ärvda API (`setWidget`, `accept`) namnges utan understreck, så
avgränsningen skiljer våra egna fel från basklassens metoder utan att
behöva importera Qt.

Ingångspunkter:
    kontrollera(sokvag) -> list[str]
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def kontrollera(sokvag: Path) -> list[str]:
    kalla = sokvag.read_text(encoding="utf-8")
    try:
        trad = ast.parse(kalla)
    except SyntaxError as fel:
        return [f"{sokvag.name}: syntaxfel rad {fel.lineno}: {fel.msg}"]

    fel: list[str] = []
    for nod in ast.walk(trad):
        if not isinstance(nod, ast.ClassDef):
            continue
        kropp = ast.get_source_segment(kalla, nod) or ""
        definierade = {
            n.name for n in nod.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        } | set(re.findall(r"self\.(_\w+)\s*=", kropp))
        for namn in sorted(set(re.findall(r"self\.(_\w+)\b", kropp))):
            if namn not in definierade:
                fel.append(f"{sokvag.name}: {nod.name}.{namn} anropas men finns inte")
    return fel


def main(argv: list[str]) -> int:
    rot = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent
    anmarkningar: list[str] = []
    for fil in sorted(rot.rglob("*.py")):
        if "__pycache__" in fil.parts:
            continue
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
