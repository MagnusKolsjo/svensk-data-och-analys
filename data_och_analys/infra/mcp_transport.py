# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Uppstart och transportval för MCP-servrar — stdio eller http.

stdio och http är symmetriska val. stdio passar lokalt, där MCP-klienten
startar processen direkt. http passar delad drift bakom Nginx
där en serverprocess betjänar flera klienter; Bearer-token-autentisering
med DOA_MCP_API_KEY krävs.

I http-läget är inställningen fail-closed: om DOA_MCP_API_KEY är
tom avbryts uppstart med exitkod 2 och ett tydligt felmeddelande.
stdio-läget berörs aldrig av autentiseringslogiken.

Modulen äger hela uppstartssekvensen — .env-inläsning, valfri
initiering och transport — så att de elva servrarna inte upprepar den
var för sig.

Ingångspunkter:
    starta(mcp, initiera=None) -> None
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


def starta(mcp: "MCPServer", initiera: Callable[[], None] | None = None) -> None:
    """Läser .env, kör valfri initiering och startar rätt transport.

    `.env` läses först: en klient som startas från skrivbordet ärver inte
    shell-miljön när den startar processen, så DOA_MCP_TRANSPORT med flera kommer härifrån.

    `initiera` körs före transporten och wrappas i try/except. En server
    ska gå upp i klienten även när Postgres-containern är nere —
    verktygsanropen felar då tills den startas, vilket är ett tydligare
    besked än en server som saknas i listan.
    """
    from data_och_analys.infra import konfig
    konfig.las_env()

    if initiera is not None:
        try:
            initiera()
            logger.info("Initiering klar")
        except Exception as fel:
            logger.warning("Initiering misslyckades: %s — fortsätter ändå", fel)

    transport = os.getenv("DOA_MCP_TRANSPORT", "stdio").strip().lower()

    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    if transport == "http":
        api_nyckel = os.getenv("DOA_MCP_API_KEY", "").strip()
        if not api_nyckel:
            # Fail-closed: hellre kraschen direkt än en öppen endpoint.
            logger.error(
                "DOA_MCP_API_KEY saknas — uppstart i http-läge avbryts. "
                "Sätt nyckeln i .env eller använd DOA_MCP_TRANSPORT=stdio."
            )
            sys.exit(2)
        host = os.getenv("DOA_MCP_HOST", "127.0.0.1")
        port = int(os.getenv("DOA_MCP_PORT", "8001"))
        _starta_http(mcp, host=host, port=port, api_nyckel=api_nyckel)
        return

    logger.error(
        "Okänt värde för DOA_MCP_TRANSPORT: %r. Tillåtna värden: stdio, http",
        transport,
    )
    sys.exit(2)


def _starta_http(mcp: "MCPServer", host: str, port: int, api_nyckel: str) -> None:
    """Startar MCPServer:s streamable-http-app via uvicorn med Bearer-auth.

    Beroenden (uvicorn, starlette) krävs bara i http-läget och importeras
    här så att stdio-läget fungerar utan dem.
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class BearerAuth(BaseHTTPMiddleware):
        """Kräver `Authorization: Bearer <nyckel>` på alla anrop."""

        async def dispatch(self, request: Request, call_next):
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return JSONResponse(
                    {"fel": "saknar Authorization-header"}, status_code=401
                )
            given = auth.split(" ", 1)[1].strip()
            if given != api_nyckel:
                return JSONResponse({"fel": "ogiltig API-nyckel"}, status_code=403)
            return await call_next(request)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth)
    logger.info("MCP-server lyssnar på http://%s:%s (Bearer-auth)", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
