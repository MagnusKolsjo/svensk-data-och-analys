# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Register över PxWeb-myndigheter — version och bas-URL per myndighet.

Båda PxWeb-klienterna (v1 och v2) läser sin myndighetslista här. Det är
en enda sanningskälla för:

    - Vilken PxWeb-version en myndighet talar just nu
    - Bas-URL till installationen
    - Myndighetsspecifika gränser (t.ex. celltak på v2)

Versionen kan flyttas utan kodändring via .env. Det är poängen: PxWeb-
myndigheterna går stegvis från v1 till v2, och flytten ska kunna ske
samma dag som källan rullar över genom att man sätter en miljövariabel
— inte genom en commit och en omdeployment.

Env-överstyrningar (per myndighet, <NAMN> i versaler):

    DOA_PXWEB_<NAMN>_VERSION     -> 1 eller 2
    DOA_PXWEB_<NAMN>_BAS_URL     -> hel URL utan slut-snedstreck
    DOA_PXWEB_<NAMN>_MAX_CELLER  -> heltal (relevant för v2)

Ingångspunkter:
    hamta(namn) -> Myndighet
    myndigheter_med_version(version) -> list[str]
    alla_myndigheter() -> list[str]
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Myndighet:
    """Konfig för en PxWeb-myndighet vid en given tidpunkt."""

    namn: str
    version: int  # 1 eller 2
    bas_url: str
    sprak: str = "sv"
    # SCB:s dokumenterade tak används som standard; överskrivs per myndighet
    # när installationen har ett annat tak.
    max_celler: int = 150_000


# ============================================================================
# Baslinje
# ============================================================================
#
# Listan uppdateras när källan officiellt rullar över till en ny version.
# Tillfälliga eller tidiga flyttar (t.ex. testköra mot en pilot-endpoint)
# görs i .env istället så att koden inte behöver röras.
#
# Bas-URL till och med språkkoden för v1 (.../api/v1/sv) eller till
# /api/v2 för v2. Slut-snedstreck utelämnas så att klienterna kan slå
# ihop entydigt.
#
# Medlingsinstitutets och SLU:s PxWeb-URL:er verifieras vid faktisk
# implementation och lämnas utanför baslinjen tills bas-URL bekräftats
# mot källan.
_BASLINJE: dict[str, Myndighet] = {
    "scb": Myndighet(
        namn="scb",
        version=2,
        bas_url="https://api.scb.se/OV0104/v2beta/api/v2",
    ),
    "jordbruksverket": Myndighet(
        namn="jordbruksverket",
        version=1,
        bas_url="https://statistik.sjv.se/PXWeb/api/v1/sv",
    ),
    "folkhalsomyndigheten": Myndighet(
        namn="folkhalsomyndigheten",
        version=1,
        bas_url="https://fohm-app.folkhalsomyndigheten.se/Folkhalsodata/api/v1/sv",
    ),
    "konjunkturinstitutet": Myndighet(
        namn="konjunkturinstitutet",
        version=1,
        bas_url="https://statistik.konj.se/PXWeb/api/v1/sv",
    ),
    "ki_prognos": Myndighet(
        namn="ki_prognos",
        version=1,
        bas_url="https://prognos.konj.se/PxWeb/api/v1/sv",
    ),
    "csn": Myndighet(
        namn="csn",
        version=1,
        bas_url="https://statistik.csn.se/PXWeb/api/v1/sv",
    ),
    "energimyndigheten": Myndighet(
        namn="energimyndigheten",
        version=1,
        # Utan /PxWeb-segmentet. Med det svarar installationen 500.
        bas_url="https://pxexternal.energimyndigheten.se/api/v1/sv",
    ),
    "skogsstyrelsen": Myndighet(
        namn="skogsstyrelsen",
        version=1,
        # Utan /PxWeb-segmentet, som hos Energimyndigheten. Servern
        # avvisar dessutom anrop utan webbläsarlik User-Agent med 403.
        bas_url="https://pxweb.skogsstyrelsen.se/api/v1/sv",
    ),
    "tillvaxtanalys": Myndighet(
        namn="tillvaxtanalys",
        version=1,
        bas_url="https://statistik.tillvaxtanalys.se/PxWeb/api/v1/sv",
    ),
    # Skolverkets statistikdatabas bär Kommunala jämförelsetal för
    # grundskola/förskola/gymnasium m.fl. tillbaka till 1992. Distinkt
    # från `api.skolverket.se/planned-educations/v3` (i `klienter/skolverket.py`)
    # — det är ett separat API med ~5 års rullande fönster på enhetsnivå,
    # medan denna PxWeb-databas levererar långa kommunala tidsserier.
    "skolverket_statistikdatabas": Myndighet(
        namn="skolverket_statistikdatabas",
        version=1,
        bas_url="https://statistikdatabasen.skolverket.se/PxWeb/api/v1/sv",
    ),
}


# ============================================================================
# Env-överstyrning
# ============================================================================


def _miljo(namn: str, falt: str) -> str | None:
    return os.getenv(f"DOA_PXWEB_{namn.upper()}_{falt.upper()}")


def _med_overstyrningar(bas: Myndighet) -> Myndighet:
    """Tillämpar env-överstyrningar på en baslinjepost.

    Returnerar bas-objektet oförändrat om inget är överstyrt; annars en
    ny instans. Frozen-dataclass tål inte direkt mutation.
    """
    version = bas.version
    bas_url = bas.bas_url
    max_celler = bas.max_celler

    if (v := _miljo(bas.namn, "VERSION")) is not None:
        version = int(v)
    if (u := _miljo(bas.namn, "BAS_URL")) is not None:
        bas_url = u.rstrip("/")
    if (c := _miljo(bas.namn, "MAX_CELLER")) is not None:
        max_celler = int(c)

    if (version, bas_url, max_celler) == (
        bas.version,
        bas.bas_url,
        bas.max_celler,
    ):
        return bas
    return Myndighet(
        namn=bas.namn,
        version=version,
        bas_url=bas_url,
        sprak=bas.sprak,
        max_celler=max_celler,
    )


# ============================================================================
# Publikt API
# ============================================================================


def hamta(namn: str) -> Myndighet:
    """Returnerar konfig för en myndighet, med env-överstyrningar tillämpade."""
    if namn not in _BASLINJE:
        kanda = ", ".join(sorted(_BASLINJE.keys()))
        raise ValueError(f"okänd myndighet: {namn!r}. Tillgängliga: {kanda}")
    return _med_overstyrningar(_BASLINJE[namn])


def alla_myndigheter() -> list[str]:
    """Listar alla myndigheter i registret, oavsett version."""
    return sorted(_BASLINJE.keys())


def myndigheter_med_version(version: int) -> list[str]:
    """Listar myndigheter som för närvarande talar en given PxWeb-version.

    Slår upp varje myndighet via `hamta` så att env-överstyrningar
    räknas in — annars hade en .env-flytt från v1 till v2 inte synts.
    """
    return sorted(n for n in _BASLINJE if hamta(n).version == version)
