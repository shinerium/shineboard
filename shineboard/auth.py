# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The owner's login and the OAuth 2.1 authorization server.

Claude's connectors register themselves (RFC 7591), send the owner to the login
page, and receive tokens for the MCP endpoint; the SDK checks PKCE, the redirect
address and the token requests, and this module keeps the clients, the codes and
the tokens in the database. There is one owner, with a password: every
authorization needs it, and every client's redirect address must be on the
allowlist. Only the SHA-256 of a code or a token is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import db
from .config import Config

SCOPE = "board"
SUBJECT = "owner"
CODE_SECONDS = 300
PENDING_SECONDS = 600
FAIL_WINDOW = 15 * 60
FAIL_PER_IP = 5
FAIL_PER_HOUR = 30
COOKIE = "sb_session"
COOKIE_SECONDS = 12 * 3600
MIN_PASSWORD = 12


# --- passwords and tokens ----------------------------------------------------


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"the password is at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    n, r, p = 2**15, 8, 1
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024, dklen=32
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(key))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


# --- the owner's login: rate limit and cookie --------------------------------


def login_blocked(conn, ip: str, now: int) -> bool:
    per_ip = conn.execute("SELECT count(*) FROM login_failures WHERE ip = ? AND at > ?", (ip, now - FAIL_WINDOW)).fetchone()[0]
    total = conn.execute("SELECT count(*) FROM login_failures WHERE at > ?", (now - 3600,)).fetchone()[0]
    return per_ip >= FAIL_PER_IP or total >= FAIL_PER_HOUR


def login_failed(conn, ip: str, now: int) -> None:
    with db.write(conn):
        conn.execute("INSERT INTO login_failures(ip, at) VALUES (?, ?)", (ip, now))
        conn.execute("DELETE FROM login_failures WHERE at < ?", (now - 86400,))


def login_ok(conn, ip: str) -> None:
    with db.write(conn):
        conn.execute("DELETE FROM login_failures WHERE ip = ?", (ip,))


BLOCKED = "Too many failed logins. Wait a quarter of an hour."


def check_login(conn, ip: str, password: str) -> str | None:
    """None when the password is right; otherwise why the login failed."""
    now = int(time.time())
    if login_blocked(conn, ip, now):
        return BLOCKED
    stored = db.get_meta(conn, "password_hash")
    if not stored:
        return "No password is set: run `shineboard set-password` on the server."
    if not verify_password(password, stored):
        login_failed(conn, ip, now)
        return "The password is wrong."
    login_ok(conn, ip)
    return None


def make_cookie(secret: str, now: int | None = None) -> str:
    exp = str((now or int(time.time())) + COOKIE_SECONDS)
    sig = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def cookie_valid(secret: str, value: str | None, now: int | None = None) -> bool:
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    want = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, want):
        return False
    return exp.isdigit() and int(exp) > (now or int(time.time()))


def set_cookie(response: Response, cfg: Config, secret: str) -> None:
    response.set_cookie(
        COOKIE, make_cookie(secret), max_age=COOKIE_SECONDS, httponly=True, secure=cfg.secure, samesite="lax", path="/"
    )


def client_ip(request: Request) -> str:
    # Uvicorn puts the address that the trusted proxy forwarded in request.client
    # (proxy_headers with forwarded_allow_ips); without a proxy it is the peer.
    return request.client.host if request.client else "unknown"


# --- pages -------------------------------------------------------------------

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} · ShineBoard</title><link rel='stylesheet' href='/board/style.css'></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=SECURITY_HEADERS)


# --- the authorization server ------------------------------------------------


class Provider:
    """OAuthAuthorizationServerProvider on the board's database."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        def call() -> Any:
            with db.opened(self.cfg.database) as conn:
                return fn(conn, *args)

        return await anyio.to_thread.run_sync(call)

    # Clients (RFC 7591).

    def redirect_allowed(self, uri: str) -> bool:
        if uri in self.cfg.allowed_redirect_uris:
            return True
        if self.cfg.allow_localhost_redirects:
            p = urlparse(uri)
            if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"):
                return True
        return False

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def fn(conn):
            row = conn.execute("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
            return None if row is None else OAuthClientInformationFull.model_validate_json(row["info"])

        return await self._run(fn)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self.redirect_allowed(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"{uri} is not an allowed redirect address of this server",
                )

        def fn(conn):
            with db.write(conn):
                n = conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
                if n >= self.cfg.max_clients:
                    # Make room by forgetting a client that never got a token.
                    conn.execute(
                        "DELETE FROM oauth_clients WHERE client_id IN (SELECT client_id FROM oauth_clients "
                        "WHERE last_used IS NULL AND client_id NOT IN (SELECT client_id FROM oauth_tokens) "
                        "ORDER BY created_at LIMIT 10)"
                    )
                    n = conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0]
                if n >= self.cfg.max_clients:
                    raise RegistrationError(error="invalid_client_metadata", error_description="too many registered clients")
                conn.execute(
                    "INSERT INTO oauth_clients(client_id, info, created_at) VALUES (?, ?, ?)",
                    (client_info.client_id, client_info.model_dump_json(), db.iso(db.utcnow())),
                )

        await self._run(fn)

    # The authorization: the owner logs in on /oauth/login.

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.cfg.resource:
            raise AuthorizeError(error="invalid_target", error_description=f"this server's resource is {self.cfg.resource}")
        if SCOPE not in (params.scopes or [SCOPE]):
            raise AuthorizeError(error="invalid_scope", error_description=f"the scope is {SCOPE}")
        pid, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = int(time.time())

        def fn(conn):
            with db.write(conn):
                conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
                conn.execute(
                    "INSERT INTO oauth_pending(id, client_id, params, csrf, expires_at) VALUES (?, ?, ?, ?, ?)",
                    (pid, client.client_id, params.model_dump_json(), csrf, now + PENDING_SECONDS),
                )

        await self._run(fn)
        return f"{self.cfg.base}/oauth/login?request={pid}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        def fn(conn):
            row = conn.execute("SELECT data FROM oauth_codes WHERE code_hash = ?", (digest(authorization_code),)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            if data["client_id"] != client.client_id:
                return None
            return AuthorizationCode(code=authorization_code, **data)

        return await self._run(fn)

    def _issue(self, conn, client_id: str, scopes: list[str], family: str) -> OAuthToken:
        now = int(time.time())
        access, refresh = new_token("sba_"), new_token("sbr_")
        t = db.iso(db.utcnow())
        access_exp = now + self.cfg.access_token_minutes * 60
        refresh_exp = now + self.cfg.refresh_token_days * 86400
        conn.execute(
            "INSERT INTO oauth_tokens(token_hash, kind, client_id, scopes, family, expires_at, created_at) VALUES (?, 'access', ?, ?, ?, ?, ?)",
            (digest(access), client_id, " ".join(scopes), family, access_exp, t),
        )
        conn.execute(
            "INSERT INTO oauth_tokens(token_hash, kind, client_id, scopes, family, expires_at, created_at) VALUES (?, 'refresh', ?, ?, ?, ?, ?)",
            (digest(refresh), client_id, " ".join(scopes), family, refresh_exp, t),
        )
        conn.execute("UPDATE oauth_clients SET last_used = ? WHERE client_id = ?", (t, client_id))
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.cfg.access_token_minutes * 60,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        def fn(conn):
            with db.write(conn):
                gone = conn.execute(
                    "DELETE FROM oauth_codes WHERE code_hash = ?", (digest(authorization_code.code),)
                ).rowcount
                if gone != 1:
                    raise TokenError(error="invalid_grant", error_description="the code was used or has expired")
                return self._issue(conn, client.client_id, authorization_code.scopes or [SCOPE], secrets.token_hex(12))

        return await self._run(fn)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        def fn(conn):
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'", (digest(refresh_token),)
            ).fetchone()
            if row is None or row["client_id"] != client.client_id:
                return None
            if row["expires_at"] is not None and row["expires_at"] < time.time():
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=row["client_id"],
                scopes=row["scopes"].split(),
                expires_at=row["expires_at"],
                resource=self.cfg.resource,
                subject=SUBJECT,
            )

        return await self._run(fn)

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        def fn(conn):
            with db.write(conn):
                row = conn.execute(
                    "SELECT family, scopes FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                    (digest(refresh_token.token),),
                ).fetchone()
                if row is None:
                    raise TokenError(error="invalid_grant", error_description="the refresh token was used or revoked")
                conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (digest(refresh_token.token),))
                granted = row["scopes"].split()
                wanted = [s for s in (scopes or granted) if s in granted] or granted
                return self._issue(conn, client.client_id, wanted, row["family"])

        return await self._run(fn)

    async def load_access_token(self, token: str) -> AccessToken | None:
        def fn(conn):
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind IN ('access', 'static')", (digest(token),)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] is not None and row["expires_at"] < time.time():
                return None
            if row["kind"] == "static":
                last = row["last_used"]
                now = db.utcnow()
                if last is None or (now - db.parse_iso(last)).total_seconds() > 600:
                    with db.write(conn):
                        conn.execute("UPDATE oauth_tokens SET last_used = ? WHERE token_hash = ?", (db.iso(now), row["token_hash"]))
            return AccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=row["scopes"].split(),
                expires_at=row["expires_at"],
                resource=self.cfg.resource,
                subject=SUBJECT,
            )

        return await self._run(fn)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        def fn(conn):
            with db.write(conn):
                row = conn.execute("SELECT family FROM oauth_tokens WHERE token_hash = ?", (digest(token.token),)).fetchone()
                if row is not None:
                    conn.execute("DELETE FROM oauth_tokens WHERE family = ?", (row["family"],))

        await self._run(fn)

    # The login page.

    async def login(self, request: Request) -> Response:
        form = await request.form() if request.method == "POST" else None
        pid = (form.get("request") if form else request.query_params.get("request")) or ""
        ip = client_ip(request)

        def load(conn):
            row = conn.execute(
                "SELECT * FROM oauth_pending WHERE id = ? AND expires_at > ?", (str(pid), int(time.time()))
            ).fetchone()
            if row is None:
                return None, None
            info = conn.execute("SELECT info FROM oauth_clients WHERE client_id = ?", (row["client_id"],)).fetchone()
            client = None if info is None else OAuthClientInformationFull.model_validate_json(info["info"])
            return row, client

        pending, client = await self._run(load)
        if pending is None or client is None:
            return page("Expired", "<h1>This authorization has expired</h1><p>Start the connection again from the app.</p>", 400)
        params = AuthorizationParams.model_validate_json(pending["params"])
        if form is None:
            return self._login_form(pending, client, params)
        if not hmac.compare_digest(str(form.get("csrf") or ""), pending["csrf"]):
            return page("Refused", "<h1>The form has expired</h1><p>Start the connection again from the app.</p>", 400)
        if form.get("decision") == "deny":
            await self._run(lambda conn: conn.execute("DELETE FROM oauth_pending WHERE id = ?", (pending["id"],)))
            return RedirectResponse(
                construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state), status_code=302
            )
        password = str(form.get("password") or "")
        failure = await self._run(check_login, ip, password)
        if failure:
            return self._login_form(pending, client, params, failure, 429 if failure == BLOCKED else 401)
        code = secrets.token_urlsafe(32)
        data = {
            "scopes": params.scopes or [SCOPE],
            "expires_at": time.time() + CODE_SECONDS,
            "client_id": client.client_id,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": self.cfg.resource,
            "subject": SUBJECT,
        }

        def store_code(conn):
            with db.write(conn):
                conn.execute("DELETE FROM oauth_pending WHERE id = ?", (pending["id"],))
                conn.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (int(time.time()),))
                conn.execute(
                    "INSERT INTO oauth_codes(code_hash, data, expires_at) VALUES (?, ?, ?)",
                    (digest(code), json.dumps(data), int(data["expires_at"])),
                )
                conn.execute(
                    "INSERT INTO audit(at, actor, client, action, target) VALUES (?, 'owner', ?, 'authorize', ?)",
                    (db.iso(db.utcnow()), client.client_id, client.client_name or ""),
                )
                return db.get_meta(conn, "cookie_secret")

        secret = await self._run(store_code)
        response = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), status_code=302)
        if secret:
            set_cookie(response, self.cfg, secret)
        return response

    def _login_form(self, pending, client: OAuthClientInformationFull, params: AuthorizationParams, error: str = "", status: int = 200) -> Response:
        name = html.escape(client.client_name or "An application")
        target = html.escape(urlparse(str(params.redirect_uri)).netloc)
        err = f"<p class='error'>{html.escape(error)}</p>" if error else ""
        body = (
            "<h1>ShineBoard</h1>"
            f"<p><strong>{name}</strong> asks for access to the board, and will return to <strong>{target}</strong>.</p>"
            "<p>Allow it only if you started this connection yourself, just now. A link that someone "
            "sent you can give their account the access.</p>"
            f"{err}<form method='post' action='/oauth/login'>"
            f"<input type='hidden' name='request' value='{html.escape(pending['id'])}'>"
            f"<input type='hidden' name='csrf' value='{html.escape(pending['csrf'])}'>"
            "<label>Password <input type='password' name='password' autocomplete='current-password' autofocus></label>"
            "<p><button type='submit' name='decision' value='approve'>Allow</button> "
            "<button type='submit' name='decision' value='deny' class='secondary'>Deny</button></p></form>"
        )
        return page("Authorize", body, status)
