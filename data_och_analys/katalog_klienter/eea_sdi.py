# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""EEA:s geodatakatalog och dess publika datalager.

Europeiska miljöbyrån driver en GeoNetwork-katalog på sdi.eea.europa.eu
med samma Elasticsearch-API som geodata.se. Filerna ligger i en publik
Nextcloud-delning som nås över WebDAV med delningstoken som användarnamn
och tomt lösenord — ingen registrering.

Modulen finns för kustlinjen. Suitens kommun- och länsgeometri byggs
genom att unionera SCB:s DeSO-områden, men de följer den administrativa
gränsen som för kustkommuner löper långt ut i havet: unionen blir
532 000 km² mot Sveriges faktiska 447 000, och Gotland fem gånger för
stort. Att snitta bort vattnet kräver en landmask.

LICENS är hela poängen med just den här källan. Alternativen är
Lantmäteriet (avtal och rollstyrd behörighet) och Eurostat GISCO
(© EuroGeographics, förbud mot kommersiell användning, icke överlåtbar
nyttjanderätt). Båda gör suitens kartutdata ofritt för mottagaren, vilket
inte går ihop med AGPL:s syfte. EEA:s kustlinje är CC-BY 4.0: attribution
räcker, kommersiell användning och vidaredistribution är tillåtna.

    © European Environment Agency, CC-BY 4.0
    https://creativecommons.org/licenses/by/4.0/

Källan är dessutom finare än GISCO — 1:100 000 mot 1:1 miljon.

Ingångspunkter:
    KUSTLINJE — metadata om datamängden
    hamta_kustlinje(rapportera) -> Path  (sökväg till nedladdad .shp)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from data_och_analys.infra import framsteg

logger = logging.getLogger(__name__)

WEBDAV = "https://sdi.eea.europa.eu/datashare/public.php/webdav"

# Nextcloud-delningar autentiseras med token som användarnamn och tomt
# lösenord. Token ligger i katalogpostens FOLDERPATH-länk och byts bara
# om EEA lägger om delningen.
DELNINGSTOKEN = "sptXqwkQr5g7Bp5"


@dataclass(frozen=True)
class Datamangd:
    """En publik datamängd i EEA:s datalager."""

    mapp: str
    stam: str
    licens: str
    attribution: str
    skala: str
    #: Shapefile är flera filer; alla måste hämtas för att kunna läsas.
    andelser: tuple[str, ...] = ("shp", "shx", "dbf", "prj")


KUSTLINJE = Datamangd(
    mapp="eea_v_3035_100_k_coastline-poly_1995-2012_p_v02_r00",
    stam="Europe_coastline_poly_rev2015",
    licens="CC-BY 4.0",
    attribution="© European Environment Agency (EEA), CC-BY 4.0",
    skala="1:100 000",
)

# Shapefilen är ~52 MB totalt och EEA:s datalager är inte snabbt.
_TIMEOUT = httpx.Timeout(600.0, connect=30.0)

USER_AGENT = "svensk-data-och-analys/EeaSdiClient (+https://github.com/)"


async def hamta_kustlinje(
    maldir: Path,
    datamangd: Datamangd = KUSTLINJE,
    rapportera: framsteg.Rapportor = framsteg.INGEN_RAPPORTOR,
) -> Path:
    """Hämtar kustlinjens shapefile till `maldir` och returnerar .shp-sökvägen.

    Redan hämtade filer hoppas över — datamängden är från 2015 och
    revideras inte, så en omhämtning är rent slöseri.
    """
    maldir.mkdir(parents=True, exist_ok=True)
    shp = maldir / f"{datamangd.stam}.shp"

    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        auth=(DELNINGSTOKEN, ""),
        follow_redirects=True,
    ) as klient:
        for i, andelse in enumerate(datamangd.andelser, start=1):
            mal = maldir / f"{datamangd.stam}.{andelse}"
            if mal.exists() and mal.stat().st_size > 0:
                logger.info("%s finns redan — hoppar över", mal.name)
                continue

            url = f"{WEBDAV}/{datamangd.mapp}/{datamangd.stam}.{andelse}"
            await rapportera(
                i, len(datamangd.andelser), f"Hämtar {mal.name}"
            )
            async with klient.stream("GET", url) as svar:
                svar.raise_for_status()
                totalt = int(svar.headers.get("content-length") or 0) or None
                hamtat = 0
                with open(mal, "wb") as f:
                    async for bit in svar.aiter_bytes(1 << 20):
                        f.write(bit)
                        hamtat += len(bit)
                        if totalt and totalt > (5 << 20):
                            await rapportera(
                                i, len(datamangd.andelser),
                                f"{mal.name}: {hamtat // (1 << 20)} av "
                                f"{totalt // (1 << 20)} MB",
                            )
            logger.info("Hämtade %s (%d byte)", mal.name, mal.stat().st_size)

    if not shp.exists():
        raise RuntimeError(f"kustlinjens shapefile saknas efter hämtning: {shp}")
    return shp
