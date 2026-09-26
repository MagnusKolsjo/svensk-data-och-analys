# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Visningsnamn för sökindexets källor.

Indexets `kalla` är en nyckel: gemener, utan diakriter, ibland förkortad
("scb", "trafa", "ki_prognos"). Den duger som nyckel men inte i ett
gränssnitt — en användare som ska välja källa ska se myndighetens namn
som myndigheten själv skriver det.

Namnen står här och inte i `klienter/myndigheter.py`, eftersom flera källor
i indexet inte är PxWeb-myndigheter alls: Kolada är en databas hos RKA,
Bibstat en insamling hos KB, och Skolverkets skolenhetsregister är ett annat
API än dess statistikdatabas.

Ingångspunkter:
    KALLNAMN
    visningsnamn(kalla) -> str
"""

from __future__ import annotations

KALLNAMN: dict[str, str] = {
    "scb": "Statistiska centralbyrån",
    "kolada": "Kolada",
    "skolverket": "Skolverket",
    "socialstyrelsen": "Socialstyrelsen",
    "forsakringskassan": "Försäkringskassan",
    "folkhalsomyndigheten": "Folkhälsomyndigheten",
    "jordbruksverket": "Jordbruksverket",
    "konjunkturinstitutet": "Konjunkturinstitutet",
    # Två skilda PxWeb-installationer hos samma myndighet: statistik.konj.se
    # bär utfall, prognos.konj.se bär prognoser. Namnen måste skilja dem åt
    # eller så blir valet meningslöst.
    "ki_prognos": "Konjunkturinstitutet (prognoser)",
    "tillvaxtanalys": "Tillväxtanalys",
    "trafa": "Trafikanalys",
    "riksbanken": "Sveriges riksbank",
    "kb_bibstat": "Kungliga biblioteket (biblioteksstatistik)",
    "csn": "Centrala studiestödsnämnden",
    "energimyndigheten": "Energimyndigheten",
    "skogsstyrelsen": "Skogsstyrelsen",
    "medlingsinstitutet": "Medlingsinstitutet",
    "slu": "Sveriges lantbruksuniversitet",
    "bra": "Brottsförebyggande rådet",
}


def visningsnamn(kalla: str) -> str:
    """Myndighetens namn, eller nyckeln med versal om den är okänd.

    En ny källa ska synas i gränssnittet direkt, inte försvinna för att
    ingen hunnit skriva in dess namn här. Reservformen är därför nyckeln
    med inledande versal, inte tom sträng.
    """
    if kalla in KALLNAMN:
        return KALLNAMN[kalla]
    return (kalla or "").replace("_", " ").capitalize()
