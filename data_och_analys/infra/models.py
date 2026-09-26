# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Pydantic v2-modeller för suitens egna svarsformer.

Modellerna används där suiten själv äger formen — geo-domänen och
pagineringskuvertet. Rå-genomsläpp från externa API:er (Kolada, PxWeb,
WFS, ArcGIS, OAFeat, SPARQL) modelleras medvetet inte: formen ägs av
tredje part och skulle behöva skrivas om vid varje API-ändring.

Poängen är `outputSchema`. En returannotation på `list[dict[str, Any]]`
ger klienten schemat "en array av vad som helst", och SDK:t skickar
dessutom ett textblock per rad. En modell i pagineringskuvertet ger
i stället ett fältnamngivet schema och ett enda block.

Fältnamnen speglar kolumnnamnen i `db/schema_geo.sql` — modellerna är
en beskrivning av det som redan returneras, inte en ny form.

Ingångspunkter:
    Lan, Region, Kommun, KlassificeringsSystem, Grupp,
    KommunIGrupp, Folkmangdsrad
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Lan(BaseModel):
    """Ett län — 21 stycken."""

    kod: str = Field(description="Tvåsiffrig länskod, t.ex. '01'")
    namn: str = Field(description="Länets namn")
    bokstav: str | None = Field(
        default=None, description="Länsbokstav, t.ex. 'AB' för Stockholm"
    )


class Region(BaseModel):
    """En region (tidigare landsting) — 21 stycken."""

    kod: str = Field(description="Tvåsiffrig regionkod")
    namn: str = Field(description="Regionens namn")


class Kommun(BaseModel):
    """En kommun — 290 stycken."""

    kod: str = Field(description="Fyrsiffrig kommunkod med inledande nolla")
    namn: str = Field(description="Kommunens namn")
    lan_kod: str | None = Field(default=None, description="Länet kommunen ligger i")
    region_kod: str | None = Field(default=None, description="Kommunens region")


class KlassificeringsSystem(BaseModel):
    """Ett klassificeringssystem, t.ex. SKR:s kommungrupper."""

    system: str = Field(description="Systemets ID, används som nyckel i andra verktyg")
    namn: str | None = Field(default=None, description="Läsbart namn")
    kalla: str | None = Field(default=None, description="Utgivande myndighet")
    beskrivning: str | None = Field(default=None, description="Vad systemet delar in")
    senast_synkad: datetime | None = Field(
        default=None, description="När systemet senast hämtades"
    )


class Grupp(BaseModel):
    """En grupp inom ett klassificeringssystem."""

    grupp_kod: str = Field(description="Gruppens kod inom systemet")
    grupp_namn: str | None = Field(default=None, description="Gruppens namn")


class KommunIGrupp(BaseModel):
    """En kommun som tillhör en viss grupp."""

    kod: str = Field(description="Fyrsiffrig kommunkod")
    namn: str | None = Field(default=None, description="Kommunens namn")


class Folkmangdsrad(BaseModel):
    """Folkmängd för en kommun ett år, i en given kommunindelning."""

    kommun_kod: str = Field(description="Fyrsiffrig kommunkod")
    kommun_namn: str | None = Field(default=None, description="Kommunens namn")
    ar: int = Field(description="Kalenderår som värdet avser")
    folkmangd: int | None = Field(default=None, description="Antal folkbokförda")
    indelning_ar: int | None = Field(
        default=None,
        description=(
            "Kommunindelningens årgång — serier med olika indelning_ar är "
            "inte jämförbara"
        ),
    )
    # Sätts bara av topplisteverktyget, som sorterar i SQL.
    rang: int | None = Field(
        default=None, description="Placering i storleksordning, 1 är störst"
    )
