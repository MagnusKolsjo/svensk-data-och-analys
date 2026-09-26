# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Tasks-extensionen — långkörande synkar utan att blockera anropet.

Synkarna hämtar stora filer och skriver tiotusentals rader. Blockerar de
längre än klientens tidsgräns avbryts anropet, och modellen får ett
timeout-fel utan att veta om skrivningen lyckades, misslyckades eller
hann halvvägs. Tasks byter ut blockeringen mot ett handtag: anropet
svarar direkt, arbetet fortsätter, och klienten pollar.

Protokollet definierar Tasks som en extension (SEP: io.modelcontextprotocol/tasks).
SDK:t levererar ramverket — `Extension`, `MethodBinding`, `intercept_tool_call`
— men ingen färdig implementation. Den här modulen är den.

Extensionen är opt-in i båda riktningarna. En klient som inte deklarerat
`io.modelcontextprotocol/tasks` får aldrig ett task-svar utan körs
synkront precis som förut; specen kräver det uttryckligen. Det gör
modulen ofarlig att ha inkopplad även mot klienter som saknar stöd.

Lagret är i minnet, och det är ett medvetet val snarare än en genväg.
I stdio-läget lever serverprocessen exakt så länge klienten håller den:
startar klienten om respawnas servern, och en task som "överlevt" i en
databas skulle ändå sakna sin körning. Handtaget kan alltså inte bli mer
varaktigt än processen som utför arbetet. Kör suiten någon gång http med
flera arbetsprocesser behöver lagret bytas mot ett delat — `Uppgiftslager`
är avgränsat så att bytet blir lokalt.

Ingångspunkter:
    TasksExtension(taskbara_verktyg)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import mcp_types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolRequestParams

from data_och_analys.infra import framsteg

logger = logging.getLogger(__name__)

IDENTIFIERARE = "io.modelcontextprotocol/tasks"

# Hur länge en färdig task går att hämta innan den städas bort. Klienten
# ska hinna polla klart även om användaren lämnat konversationen ett tag.
STANDARD_TTL_MS = 3_600_000

# Vad servern föreslår att klienten väntar mellan pollningar. Synkarna tar
# minuter, så tätare än så ger bara trafik utan ny information.
STANDARD_POLL_MS = 2_000


def _nu() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Uppgift:
    """En pågående eller avslutad körning.

    `korning` är asyncio-handtaget, som behövs för att kunna avbryta.
    `resultat` och `fel` är ömsesidigt uteslutande och sätts först när
    statusen blivit terminal.
    """

    task_id: str
    verktyg: str
    status: types.TaskStatus = "working"
    status_meddelande: str | None = None
    skapad: str = field(default_factory=_nu)
    andrad: str = field(default_factory=_nu)
    resultat: Any = None
    fel: dict[str, Any] | None = None
    korning: asyncio.Task[Any] | None = None
    _utgar: float = field(default_factory=lambda: time.monotonic() + STANDARD_TTL_MS / 1000)

    def som_protokoll(self) -> types.Task:
        return types.Task(
            task_id=self.task_id,
            status=self.status,
            status_message=self.status_meddelande,
            created_at=self.skapad,
            last_updated_at=self.andrad,
            ttl=STANDARD_TTL_MS,
            poll_interval=STANDARD_POLL_MS,
        )


class Uppgiftslager:
    """Håller uppgifterna för den här processen.

    Avgränsad med avsikt: byts lagret mot en delad tabell är det den här
    klassen som skrivs om, inte extensionen.
    """

    def __init__(self) -> None:
        self._uppgifter: dict[str, Uppgift] = {}

    def skapa(self, verktyg: str) -> Uppgift:
        self._stada()
        u = Uppgift(task_id=str(uuid.uuid4()), verktyg=verktyg)
        self._uppgifter[u.task_id] = u
        return u

    def hamta(self, task_id: str) -> Uppgift | None:
        self._stada()
        return self._uppgifter.get(task_id)

    def lista(self) -> list[Uppgift]:
        self._stada()
        return sorted(self._uppgifter.values(), key=lambda u: u.skapad)

    def _stada(self) -> None:
        """Tar bort utgångna uppgifter. Pågående arbete rörs aldrig."""
        nu = time.monotonic()
        for tid in [
            t for t, u in self._uppgifter.items()
            if u._utgar < nu and u.status != "working"
        ]:
            del self._uppgifter[tid]


class TasksExtension(Extension):
    """Gör utvalda verktyg körbara som tasks.

    `taskbara_verktyg` är namnen på de verktyg som får bli tasks. Övriga
    verktyg går alltid den synkrona vägen — de flesta svarar på
    millisekunder och skulle bara bli krångligare av ett handtag.
    """

    identifier = IDENTIFIERARE

    def __init__(self, taskbara_verktyg: Iterable[str]) -> None:
        self._taskbara = set(taskbara_verktyg)
        self._lager = Uppgiftslager()

    # -- metoder extensionen serverar ---------------------------------------

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding("tasks/get", types.GetTaskRequestParams, self._hamta),
            MethodBinding("tasks/result", types.GetTaskPayloadRequestParams,
                          self._resultat),
            # tasks/list är paginerad i protokollet och har därför ingen egen
            # params-typ — den delar PaginatedRequestParams med övriga listor.
            MethodBinding("tasks/list", types.PaginatedRequestParams, self._lista),
            MethodBinding("tasks/cancel", types.CancelTaskRequestParams,
                          self._avbryt),
        ]

    async def _hamta(self, ctx: ServerRequestContext[Any, Any], params: Any):
        return self._kravd(params.task_id).som_protokoll()

    async def _resultat(self, ctx: ServerRequestContext[Any, Any], params: Any):
        u = self._kravd(params.task_id)
        if u.status != "completed":
            raise MCPError(
                code=types.INVALID_REQUEST,
                message=(
                    f"task {params.task_id} har status {u.status}, inte "
                    "completed — resultatet finns först när körningen lyckats"
                    + (f" ({u.status_meddelande})" if u.status_meddelande else "")
                ),
            )
        return u.resultat

    async def _lista(self, ctx: ServerRequestContext[Any, Any], params: Any):
        return types.ListTasksResult(tasks=[u.som_protokoll()
                                            for u in self._lager.lista()])

    async def _avbryt(self, ctx: ServerRequestContext[Any, Any], params: Any):
        u = self._kravd(params.task_id)
        # Avbrott är kooperativt enligt specen: vi signalerar, men en synk
        # som står mitt i en databasskrivning får slutföra den.
        if u.korning is not None and not u.korning.done():
            u.korning.cancel()
        return {"task": u.som_protokoll().model_dump(
            by_alias=True, exclude_none=True)}

    def _kravd(self, task_id: str) -> Uppgift:
        """Slår upp en uppgift och ger ett läsbart fel när den saknas.

        Ett okänt id är oftast en utgången task, inte ett programfel — TTL
        städar bort avslutade körningar. Felet ska säga det, inte bli ett
        anonymt internal error.
        """
        u = self._lager.hamta(task_id)
        if u is None:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message=(
                    f"okänd task: {task_id} — den kan ha gått ut "
                    f"({STANDARD_TTL_MS // 60000} minuter efter att den avslutats)"
                ),
            )
        return u

    # -- avledningen av tools/call ------------------------------------------

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if params.name not in self._taskbara or not _klienten_stodjer(ctx):
            return await call_next(ctx)

        uppgift = self._lager.skapa(params.name)

        async def rapportera(gjort: int, av: int | None, meddelande: str) -> None:
            uppgift.status_meddelande = (
                f"{meddelande} ({gjort} av {av})" if av else meddelande
            )
            uppgift.andrad = _nu()

        async def kor() -> None:
            # Rapportören sätts i contextvar så att verktyget hittar den utan
            # att veta att det körs som en task. Se framsteg.fran_context.
            framsteg.aktiv_rapportor.set(framsteg.strypt(rapportera))
            try:
                uppgift.resultat = await call_next(ctx)
                uppgift.status = "completed"
                uppgift.status_meddelande = "Klar"
            except asyncio.CancelledError:
                uppgift.status = "cancelled"
                uppgift.status_meddelande = "Avbruten"
                raise
            except Exception as fel:
                logger.exception("Task %s misslyckades", uppgift.task_id)
                uppgift.status = "failed"
                uppgift.status_meddelande = str(fel)
                uppgift.fel = {"code": types.INTERNAL_ERROR, "message": str(fel)}
            finally:
                uppgift.andrad = _nu()

        uppgift.korning = asyncio.create_task(kor())
        logger.info("Task %s startad för %s", uppgift.task_id, params.name)

        # Returneras som mappning, inte som `types.CreateTaskResult`. Den
        # modellen är 2025-11-25-eran och saknar `resultType`, så SDK:t
        # sieva:r den mot 2026-unionen och avvisar den. Ett `resultType`
        # utanför kärnvokabulären markerar i stället en form som ägs av
        # extensionen, och släpps igenom orörd på moderna anslutningar.
        return {
            "resultType": "task",
            "task": uppgift.som_protokoll().model_dump(
                by_alias=True, exclude_none=True
            ),
        }


def _klienten_stodjer(ctx: ServerRequestContext[Any, Any]) -> bool:
    """Sant bara när klienten deklarerat extensionen i det här anropet.

    Specen är uttrycklig: en klient som inte deklarerat stöd får aldrig ett
    task-svar. Kontrollen görs per anrop eftersom kapabiliteterna i
    2026-07-28 följer med varje request i stället för i en handskakning.

    `ctx.meta` är en TypedDict, inte en modell — kapabiliteterna ligger
    under den reserverade nyckeln och läses som vanlig dict.
    """
    meta = getattr(ctx, "meta", None) or {}
    kapabiliteter = meta.get(types.CLIENT_CAPABILITIES_META_KEY) or {}
    if not isinstance(kapabiliteter, dict):
        # Vissa transporter ger en modell i stället för rådata.
        kapabiliteter = getattr(kapabiliteter, "__dict__", {}) or {}
    extensions = kapabiliteter.get("extensions") or {}
    return IDENTIFIERARE in extensions
