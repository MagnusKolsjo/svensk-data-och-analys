# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Kontroll av adresser som kommer utifrån, innan de hämtas.

Flera verktyg hämtar från adresser som anroparen anger eller som står i en
katalog: distributioner, katalogfiler, tjänsteadresser och datamängds-id som
bär sin katalogs adress. Modellen som anropar verktygen läser text ur samma
okända kataloger och kan förmås att skicka vidare en adress som texten
föreslår. Utan kontroll kan en sådan adress nå det som bara är tänkt att nås
inifrån — databasen, REST-API:t, molnleverantörens metadatatjänst.

En anslutning godtas därför bara till publika adresser. Privata nät,
loopback, länklokala adresser, CGNAT, multicast och reserverade block
avvisas, liksom IPv4 inbäddad i IPv6.

Kontrollen sitter i anslutningsledet, inte före anropet. `klient()` ger en
httpx-klient vars nätverkslager slår upp värdnamnet en gång, prövar varje
adress och sedan ansluter till just den adress som prövades. Ett separat
uppslag före anropet hade lämnat en lucka: klienten slår då upp namnet igen,
och en namnserver som byter svar däremellan (DNS rebinding) kommer förbi.
TLS verifieras fortfarande mot värdnamnet, eftersom bara TCP-anslutningen
går till den prövade adressen. Varje anslutning prövas, även de som en
omdirigering leder till.

En proxy i miljön (`HTTPS_PROXY`) tar trafiken förbi nätverkslagret;
klienten läser därför inte proxyinställningar ur miljön.

`DOA_TILLATNA_INTERNA_VARDAR` tar en kommaseparerad lista med värdnamn som
får nås trots att de ligger internt, till exempel en egen GeoServer i det
lokala nätet.

Ingångspunkter:
    klient(**httpx_argument) -> httpx.AsyncClient
    kontrollera(url) -> None          (asynkront; OtillatenAdress vid avslag)
    OtillatenAdress
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpcore
import httpx

_SCHEMAN = {"http", "https"}


class OtillatenAdress(ValueError):
    """Adressen leder till ett internt nät eller använder fel protokoll."""


# ---------------------------------------------------------------------------
# Prövning
# ---------------------------------------------------------------------------

def _tillatna_vardar() -> set[str]:
    return {v.strip().lower().rstrip(".")
            for v in os.getenv("DOA_TILLATNA_INTERNA_VARDAR", "").split(",") if v.strip()}


def _ar_publik(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        inbaddad = ip.ipv4_mapped or ip.sixtofour
        if inbaddad is not None:
            return _ar_publik(inbaddad)
    # is_global täcker privata nät, loopback, länklokalt, CGNAT och de
    # reserverade blocken; multicast räknas inte alltid bort av den.
    return ip.is_global and not ip.is_multicast


async def _uppslag(vard: str, port: int) -> list[str]:
    try:
        return [ipaddress.ip_address(vard).compressed]
    except ValueError:
        pass
    try:
        svar = await asyncio.get_running_loop().getaddrinfo(
            vard, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise httpcore.ConnectError(f"{vard} går inte att slå upp: {e}") from e
    # Ordningen från getaddrinfo behålls; den speglar systemets val av
    # adressfamilj.
    return list(dict.fromkeys(rad[4][0] for rad in svar))


async def _godkanda(vard: str, port: int, url: str = "") -> list[str]:
    """Värdens adresser, eller OtillatenAdress om någon av dem är intern.

    Hela uppslaget avvisas om en enda adress är intern. Att välja bort den
    och ansluta till de övriga skulle låta en namnserver blanda in en intern
    adress och styra vilken som används.
    """
    vard = vard.lower().rstrip(".")
    adresser = await _uppslag(vard, port)
    for adress in adresser:
        # Zonindex (fe80::1%en0) följer med ur getaddrinfo för länklokala.
        ip = ipaddress.ip_address(adress.split("%", 1)[0])
        if not _ar_publik(ip):
            var = f"{vard} ligger" if vard == ip.compressed else f"{vard} leder till {ip}, som ligger"
            raise OtillatenAdress(
                f"{url or vard}: {var} i ett internt nät. Sådana adresser hämtas inte; "
                "en egen tjänst i det lokala nätet kan tillåtas med "
                "DOA_TILLATNA_INTERNA_VARDAR i .env")
    return adresser


async def kontrollera(url: str) -> None:
    """Prövar en adress utan att hämta den.

    För adresser som någon annan än suitens klienter hämtar, som rdflib när
    den läser JSON-LD-kontexter. Det som hämtas med `klient()` prövas i
    anslutningen och behöver inte prövas här.
    """
    delar = urlsplit(url)
    if delar.scheme.lower() not in _SCHEMAN:
        raise OtillatenAdress(f"{url}: bara http och https hämtas")
    vard = (delar.hostname or "").lower().rstrip(".")
    if not vard:
        raise OtillatenAdress(f"{url}: adressen saknar värd")
    if vard in _tillatna_vardar():
        return
    port = delar.port or (443 if delar.scheme.lower() == "https" else 80)
    await _godkanda(vard, port, url)


# ---------------------------------------------------------------------------
# Nätverkslager
# ---------------------------------------------------------------------------

class _ProvandeNatverk(httpcore.AsyncNetworkBackend):
    """Ansluter bara till prövade adresser, och till den adress som prövades."""

    def __init__(self, inre: httpcore.AsyncNetworkBackend) -> None:
        self._inre = inre

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                          local_address: str | None = None,
                          socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        if host.lower().rstrip(".") in _tillatna_vardar():
            return await self._inre.connect_tcp(host, port, timeout, local_address, socket_options)
        senaste: Exception | None = None
        for adress in await _godkanda(host, port):
            try:
                return await self._inre.connect_tcp(adress, port, timeout, local_address,
                                                    socket_options)
            except httpcore.ConnectError as e:
                senaste = e
        raise senaste or httpcore.ConnectError(f"{host} har inga adresser")

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        raise OtillatenAdress("Unix-socklar ansluts inte")

    async def sleep(self, seconds: float) -> None:
        await self._inre.sleep(seconds)


def klient(*, verify: Any = True, limits: httpx.Limits | None = None,
           **argument: Any) -> httpx.AsyncClient:
    """En httpx.AsyncClient som bara når publika adresser.

    Tar samma argument som httpx.AsyncClient. `verify` och `limits` hör till
    nätverkslagret och skickas dit; resten går till klienten.
    """
    transport = httpx.AsyncHTTPTransport(
        verify=verify, limits=limits or httpx.Limits(max_connections=100, max_keepalive_connections=20),
        trust_env=False)
    # httpx exponerar inte nätverkslagret; poolen bär det som enda plats.
    pool = transport._pool
    pool._network_backend = _ProvandeNatverk(pool._network_backend)
    return httpx.AsyncClient(transport=transport, trust_env=False, **argument)
