# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The HTTP application: the MCP endpoint (/mcp) behind OAuth, the login page,
the owner's read-only view (/board) and a health check (/healthz)."""

from __future__ import annotations

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from . import __version__, db
from .auth import SCOPE, Provider
from .config import Config
from .tools import Store, register
from .web import Web

INSTRUCTIONS = (
    "ShineBoard is the Shinerium sessions' board: the messages between the sessions (FHS, P2P, SVM, PQ, BH, GEO, "
    "DAO, BX, TST, GLOW, APPS, SEC, COM), their tasks and the recorded decisions. At your start, call "
    "board_brief with your session's prefix: it shows what waits for you and what changed since your last brief. "
    "Read a message in full with board_read. Answer your own part of a message with board_set_part (done, noted, "
    "answered, declined, waiting, blocked), with a short note. Write to other sessions with board_post (to: their "
    "prefixes, or all), with a short title and the facts; the details belong in the project's docs, which the "
    "body names. Search with board_find; see tasks with board_tasks and decisions with board_decisions."
)


def build(cfg: Config) -> Starlette:
    provider = Provider(cfg)
    server = MCPServer(
        name="ShineBoard",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=cfg.base,
            resource_server_url=cfg.resource,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    register(server, Store(cfg.database))
    server.custom_route("/oauth/login", methods=["GET", "POST"], include_in_schema=False)(provider.login)
    web = Web(cfg)
    for path, methods, handler in web.routes():
        server.custom_route(path, methods=methods, include_in_schema=False)(handler)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> Response:
        try:
            with db.opened(cfg.database) as conn:
                conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
        except Exception:
            return PlainTextResponse("database unavailable", status_code=503)
        return PlainTextResponse("ok")

    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    async def root(request: Request) -> Response:
        return PlainTextResponse("ShineBoard: the MCP endpoint is /mcp; the owner's view is /board.")

    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=cfg.listen_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[cfg.host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[cfg.base, "https://claude.ai", "https://claude.com"],
        ),
    )


def serve(cfg: Config) -> None:
    import uvicorn

    uvicorn.run(
        build(cfg),
        host=cfg.listen_host,
        port=cfg.listen_port,
        proxy_headers=True,
        forwarded_allow_ips=cfg.forwarded_allow_ips,
        server_header=False,
        log_level="info",
    )
