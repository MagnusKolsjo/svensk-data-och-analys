# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Register över var kommuner, regioner och myndigheter delar öppna data.

Registret svarar på frågan som föregår varje sökning: vilken katalog ska
frågas? Det fylls av kartläggningen (`cli/kartlagg_datadelning.py`) med de
kataloger som faktiskt svarade — inte med det som en gång anmälts till
dataportal.se.

Ingångspunkter:
    Katalog
    spara(kataloger) -> int
    ta_bort_utom(kategori, nycklar) -> int
    las(kategori, lan_kod, namn, plattform) -> list[Katalog]
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from data_och_analys.infra import db

PLATTFORMAR = ("entryscape", "ckan", "huwise", "arcgis_hub", "dcat_fil")


class Katalog(BaseModel):
    org_nyckel: str
    kategori: str = Field(description="kommun, region eller myndighet")
    kod: str = Field(default="", description="Kommunkod, länsbokstav eller organisationsnummer")
    namn: str
    lan_kod: str = Field(default="", description="Tvåsiffrig länskod för kommuner och regioner")
    plattform: str = Field(description=", ".join(PLATTFORMAR))
    bas_url: str
    kontext: str = Field(default="", description="EntryScape-kontext när instansen delas av flera utgivare")
    katalog_url: str = ""
    antal_dataset: int | None = None
    belagg: str = ""
    kontrollerad: datetime | None = None


_KOLUMNER = ("org_nyckel", "kategori", "kod", "namn", "lan_kod", "plattform", "bas_url",
             "kontext", "katalog_url", "antal_dataset", "belagg")


async def spara(kataloger: list[Katalog]) -> int:
    """Skriver in eller uppdaterar. Rader som inte finns i anropet lämnas orörda —
    en organisation vars katalog tillfälligt inte svarade ska inte försvinna."""
    if not kataloger:
        return 0
    p = db.prefix()
    ph = ", ".join(db.ph(i + 1) for i in range(len(_KOLUMNER)))
    uppdatera = ", ".join(f"{k} = EXCLUDED.{k}" for k in _KOLUMNER[1:] if k not in ("plattform", "bas_url", "kontext"))
    sql = (f"INSERT INTO {p}datakatalog ({', '.join(_KOLUMNER)}) VALUES ({ph}) "
           f"ON CONFLICT (org_nyckel, plattform, bas_url, kontext) DO UPDATE SET "
           f"{uppdatera}, kontrollerad = NOW()")
    async with db.hamta_db() as anslutning:
        for k in kataloger:
            await anslutning.execute(sql, *(getattr(k, c) if getattr(k, c) is not None else None
                                            for c in _KOLUMNER))
    return len(kataloger)


async def ta_bort_utom(kategori: str, nycklar: set[str]) -> int:
    """Tar bort kategorins rader för organisationer som inte längre finns.

    Anropas bara med en fullständig organisationslista — en myndighet som gått
    upp i en annan finns inte i den, och dess katalog hör då till
    efterträdaren. Rader för organisationer som finns men inte svarade vid
    kontrollen lämnas orörda.
    """
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            f"SELECT DISTINCT org_nyckel FROM {db.prefix()}datakatalog WHERE kategori = {db.ph(1)}",
            kategori)
        borta = [r["org_nyckel"] for r in rader if r["org_nyckel"] not in nycklar]
        for nyckel in borta:
            await anslutning.execute(
                f"DELETE FROM {db.prefix()}datakatalog WHERE org_nyckel = {db.ph(1)}", nyckel)
    return len(borta)


async def las(kategori: str | None = None, lan_kod: str | None = None,
              namn: str | None = None, plattform: str | None = None) -> list[Katalog]:
    """Kataloger som matchar urvalet. `namn` är en delsträng, skiftlägesokänslig."""
    villkor, varden = [], []
    for kolumn, varde in (("kategori", kategori), ("lan_kod", lan_kod), ("plattform", plattform)):
        if varde:
            varden.append(varde)
            villkor.append(f"{kolumn} = {db.ph(len(varden))}")
    if namn:
        varden.append(f"%{namn.lower()}%")
        villkor.append(f"lower(namn) LIKE {db.ph(len(varden))}")
    var = f"WHERE {' AND '.join(villkor)}" if villkor else ""
    async with db.hamta_db() as anslutning:
        rader = await anslutning.fetch(
            f"SELECT * FROM {db.prefix()}datakatalog {var} ORDER BY kategori, namn, plattform",
            *varden)
    return [Katalog(**{k: (v if v is not None else Katalog.model_fields[k].default)
                       for k, v in dict(r).items() if k in Katalog.model_fields})
            for r in rader]
