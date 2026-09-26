# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Hur färsk den lagrade datan är, och vad som behöver synkas om.

Svarar på frågan som lagrad data alltid väcker men sällan besvarar: går det
att lita på det här? Antalet rader säger ingenting — en tabell med rätt antal
rader kan vara tre år gammal.

Åldern läses ur två håll. Ungefär hälften av tabellerna bär `senast_synkad`
per rad; resten har ingenting, och för dem gäller journalen `synkkorning`.
Saknas båda är svaret "okänd ålder", vilket är ett ärligare besked än att
tiga.

Ingångspunkter:
    notera_synk(dataset, antal_rader, kommentar) -> None
    las_status() -> list[dict]
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from data_och_analys.geodata.synkregister import REGISTER
from data_och_analys.infra import db

# Tabeller som bär tidsstämpel per rad. Den är mer exakt än journalen —
# den överlever att journalen rensas och sätts av själva skrivningen.
_TIDSKOLUMN = {
    "deso_geom": "senast_synkad",
    "regso_geom": "senast_synkad",
    "kommun_geom": "senast_synkad",
    "lan_geom": "senast_synkad",
    "region_geom": "senast_synkad",
    "valdistrikt_geom": "senast_synkad",
    "valkrets_geom": "senast_synkad",
    "skolenhet_punkt": "senast_synkad",
    "klassificeringssystem": "senast_synkad",
    "sok_index": "uppdaterad",
    "datakatalog": "kontrollerad",
}


async def notera_synk(
    dataset: str, antal_rader: int | None = None, kommentar: str | None = None
) -> None:
    """Skriver en rad i journalen. Ska anropas i slutet av varje synk.

    En rad per körning, inte en status som skrivs över — så att kadensen
    går att mäta i efterhand i stället för att antas.
    """
    async with db.hamta_db() as anslutning:
        await anslutning.execute(
            f"INSERT INTO {db.prefix()}synkkorning (dataset, antal_rader, kommentar) "
            f"VALUES ({db.ph(1)}, {db.ph(2)}, {db.ph(3)})",
            dataset, antal_rader, kommentar,
        )


async def las_status() -> list[dict[str, Any]]:
    """Ålder och färskhetsbedömning per registrerat dataset."""
    nu = datetime.now(timezone.utc)
    p = db.prefix()
    rader: list[dict[str, Any]] = []

    async with db.hamta_db() as anslutning:
        journal = {
            r["dataset"]: (r["tidpunkt"], r["antal_rader"])
            for r in await anslutning.fetch(
                f"SELECT DISTINCT ON (dataset) dataset, tidpunkt, antal_rader "
                f"FROM {p}synkkorning ORDER BY dataset, tidpunkt DESC"
            )
        }

        for namn, d in REGISTER.items():
            antal = tid = None
            try:
                antal = await anslutning.fetchval(
                    f"SELECT count(*) FROM {p}{d.tabell}")
                kolumn = _TIDSKOLUMN.get(d.tabell)
                if kolumn:
                    tid = await anslutning.fetchval(
                        f"SELECT max({kolumn}) FROM {p}{d.tabell}")
            except Exception:  # noqa: BLE001
                # Tabellen finns inte än — datasetet är registrerat men
                # aldrig synkat. Det är information, inte ett fel.
                pass

            ur_journal = journal.get(namn)
            if ur_journal and (tid is None or ur_journal[0] > tid):
                tid = ur_journal[0]

            alder = None
            if tid is not None:
                if tid.tzinfo is None:
                    tid = tid.replace(tzinfo=timezone.utc)
                alder = (nu - tid).days

            rader.append({
                "dataset": namn,
                "tabell": d.tabell,
                "kalla": d.kalla,
                "verktyg": d.verktyg,
                "antal_rader": antal,
                "senast_synkad": tid.isoformat() if tid else None,
                "alder_dagar": alder,
                "kadens_dagar": d.kadens_dagar,
                "kadens_grund": d.kadens_grund,
                "varfor_lokalt": d.varfor_lokalt,
                "bedomning": _bedom(antal, alder, d.kadens_dagar),
            })
    return rader


def _bedom(antal: int | None, alder: int | None, kadens: int | None) -> str:
    """Kort omdöme i klartext.

    `kadens_dagar = None` betyder att datat inte har någon takt utan följer
    val eller reformer. Sådant blir aldrig "inaktuellt" av att tiden går —
    bara av att något händer, och det kan en åldersjämförelse inte veta.
    """
    if not antal:
        return "saknas — aldrig synkad"
    if alder is None:
        return "okänd ålder — synka för att sätta en tidsstämpel"
    if kadens is None:
        return "ändras vid reform, inte med kalendern"
    if alder > kadens * 2:
        return f"inaktuell — {alder} dagar, kadens {kadens}"
    if alder > kadens:
        return f"bör synkas — {alder} dagar, kadens {kadens}"
    return f"aktuell — {alder} dagar"
