# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The server over HTTP: OAuth as Claude's connector does it, MCP with the SDK's
own client, static tokens, the owner's view and the transport's checks."""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from shineboard import cli, db

from conftest import PASSWORD

CLAUDE = "https://claude.ai/api/mcp/auth_callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(c: httpx.Client, redirect: str = CLAUDE) -> httpx.Response:
    return c.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Claude",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def authorize(c: httpx.Client, cfg, client_id: str, password: str = PASSWORD) -> tuple[httpx.Response, str]:
    verifier, challenge = pkce()
    r = c.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "st-1", "scope": "board", "resource": cfg.resource,
        },
    )
    assert r.status_code == 302, r.text
    login = r.headers["location"]
    assert login.startswith(cfg.base + "/oauth/login?request=")
    form = c.get(login)
    assert form.status_code == 200 and "asks for access to the board" in form.text and "claude.ai" in form.text
    assert form.headers["x-frame-options"] == "DENY"
    request_id = re.search(r"name='request' value='([^']+)'", form.text).group(1)
    csrf = re.search(r"name='csrf' value='([^']+)'", form.text).group(1)
    r = c.post("/oauth/login", data={"request": request_id, "csrf": csrf, "password": password, "decision": "approve"})
    return r, verifier


def tokens(c: httpx.Client, cfg) -> tuple[str, dict]:
    client_id = register(c).json()["client_id"]
    r, verifier = authorize(c, cfg, client_id)
    assert r.status_code == 302
    location = urlparse(r.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE
    query = parse_qs(location.query)
    assert query["state"] == ["st-1"]
    t = c.post(
        "/token",
        data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CLAUDE,
            "client_id": client_id, "code_verifier": verifier, "resource": cfg.resource,
        },
    )
    assert t.status_code == 200, t.text
    return client_id, t.json()


async def call_tools(url: str, token: str) -> tuple[list[str], str]:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                posted = await session.call_tool(
                    "board_post",
                    {"sender": "PQ", "to": ["COM"], "title": "A test message", "body": "Over MCP."},
                )
                assert not posted.is_error, posted
                result = await session.call_tool("board_brief", {"session": "COM"})
                bad = await session.call_tool("board_brief", {"session": "NOPE"})
                assert bad.is_error and "unknown session" in bad.content[0].text
                return [t.name for t in listed.tools], result.content[0].text


def test_metadata_and_unauthorized(live):
    with httpx.Client(base_url=live.base) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
        assert "resource_metadata" in r.headers["www-authenticate"]
        resource = c.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource["resource"] == live.resource and resource["authorization_servers"] == [live.base]
        meta = c.get("/.well-known/oauth-authorization-server").json()
        assert meta["registration_endpoint"] == live.base + "/register" and "S256" in meta["code_challenge_methods_supported"]
        assert c.get("/healthz").text == "ok"
        bad_host = c.post("/mcp", json={}, headers={"Host": "evil.example", "Authorization": "Bearer x"})
        assert bad_host.status_code in (401, 421)


def test_registration_allowlist(live):
    with httpx.Client(base_url=live.base) as c:
        r = register(c, "https://evil.example/callback")
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
        assert register(c, "http://localhost:33418/callback").status_code == 201
        assert register(c).status_code == 201


def test_oauth_flow_and_mcp(live):
    with httpx.Client(base_url=live.base) as c:
        client_id, tok = tokens(c, live)
        names, brief = anyio.run(call_tools, live.resource, tok["access_token"])
        assert {"board_brief", "board_read", "board_post", "board_set_part", "board_find", "board_tasks",
                "board_set_task", "board_decisions", "board_record_decision", "board_overview"} <= set(names)
        assert "Waiting for COM (1):" in brief and "PQ → COM — A test message" in brief
        # The refresh token turns once.
        r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id})
        assert r.status_code == 200, r.text
        fresh = r.json()
        again = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id})
        assert again.status_code == 400
        # Revoking the new access token ends its family.
        # (The SDK's revocation form asks for client_secret even of a public client: an empty one.)
        assert c.post("/revoke", data={"token": fresh["access_token"], "client_id": client_id, "client_secret": ""}).status_code == 200
        r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": fresh["refresh_token"], "client_id": client_id})
        assert r.status_code == 400
        h = {"Authorization": f"Bearer {fresh['access_token']}", "Accept": "application/json, text/event-stream"}
        assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h).status_code == 401
    with db.opened(live.database) as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit ORDER BY id")]
    assert actions[:2] == ["authorize", "post"]


def test_wrong_password_and_rate_limit(live):
    with httpx.Client(base_url=live.base) as c:
        client_id = register(c).json()["client_id"]
        for _ in range(5):
            r, _ = authorize(c, live, client_id, "not the password")
            assert r.status_code == 401 and "The password is wrong." in r.text
        r, _ = authorize(c, live, client_id)
        assert r.status_code == 429 and "Too many failed logins" in r.text


def test_code_used_once_and_deny(live):
    with httpx.Client(base_url=live.base) as c:
        client_id = register(c).json()["client_id"]
        r, verifier = authorize(c, live, client_id)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE, "client_id": client_id, "code_verifier": verifier}
        bad = c.post("/token", data={**data, "code_verifier": "x" * 50})
        assert bad.status_code == 400
        assert c.post("/token", data=data).status_code == 200
        assert c.post("/token", data=data).status_code == 400
        # Deny goes back with access_denied.
        _, challenge = pkce()
        r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE,
                                         "code_challenge": challenge, "code_challenge_method": "S256", "state": "s2"})
        form = c.get(r.headers["location"]).text
        request_id = re.search(r"name='request' value='([^']+)'", form).group(1)
        csrf = re.search(r"name='csrf' value='([^']+)'", form).group(1)
        denied = c.post("/oauth/login", data={"request": request_id, "csrf": csrf, "decision": "deny"})
        assert denied.status_code == 302 and "error=access_denied" in denied.headers["location"]
        forged = c.post("/oauth/login", data={"request": request_id, "csrf": "forged", "password": PASSWORD})
        assert forged.status_code == 400


def test_static_token(live, capsys):
    import os

    os.environ["SHINEBOARD_CONFIG"] = ""
    from shineboard import config

    original = config.load
    config_obj = live
    cli.load = lambda path=None: config_obj
    try:
        assert cli.main(["token", "create", "com-cli"]) == 0
        token = capsys.readouterr().out.strip()
        names, brief = anyio.run(call_tools, live.resource, token)
        assert "board_brief" in names and "A test message" in brief
        assert cli.main(["token", "revoke", "com-cli"]) == 0
        with httpx.Client(base_url=live.base) as c:
            h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
            assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h).status_code == 401
    finally:
        cli.load = original


def test_web_view(live):
    with httpx.Client(base_url=live.base) as c:
        r = c.get("/board")
        assert r.status_code == 303 and r.headers["location"].startswith("/board/login")
        assert c.post("/board/login", data={"password": "wrong password!!", "next": "/board"}).status_code == 401
        r = c.post("/board/login", data={"password": PASSWORD, "next": "https://evil.example/"})
        assert r.status_code == 303 and r.headers["location"] == "/board"
        home = c.get("/board")
        assert home.status_code == 200 and "The board" in home.text and "Content-Security-Policy" in home.headers
        with db.opened(live.database) as conn:
            from shineboard import board

            board.post(conn, "FHS", ["COM"], "A <script>x</script> title", "Body with `code` and **bold** and #1.")
        page = c.get("/board/m/1")
        assert page.status_code == 200
        assert "&lt;script&gt;" in page.text and "<script>" not in page.text
        assert "<code>code</code>" in page.text and "<strong>bold</strong>" in page.text
        assert c.get("/board/find", params={"q": "script"}).status_code == 200
        assert c.get("/board/s/COM").status_code == 200 and c.get("/board/s/NOPE").status_code == 404
        assert c.get("/board/style.css").headers["content-type"].startswith("text/css")
        c.get("/board/logout")
        assert c.get("/board").status_code == 303
        # The owner's view counts failed logins as the OAuth login does (a good login cleared the earlier one).
        for _ in range(5):
            assert c.post("/board/login", data={"password": "wrong password!!"}).status_code == 401
        blocked = c.post("/board/login", data={"password": PASSWORD})
        assert blocked.status_code == 429 and "Too many failed logins" in blocked.text
