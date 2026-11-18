# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""A read-only view of the board for the owner's browser, behind the same password.

Server-rendered HTML with one stylesheet: no scripts, nothing from elsewhere.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import anyio
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from . import auth, board, db
from .config import Config

STYLE = """
:root { --bg: #ffffff; --fg: #1d2433; --muted: #5c667a; --line: #dfe3ea; --accent: #273459; --amber: #b07800;
        --open: #b3261e; --closed: #2e7d32; --code: #f3f5f8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #11151d; --fg: #e6e9ef; --muted: #9aa3b5; --line: #2a3140; --accent: #9fb3e8; --amber: #ffb80f;
          --open: #ff8a80; --closed: #81c784; --code: #1b212c; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.4rem; margin: .4em 0; } h2 { font-size: 1.1rem; margin: 1.4em 0 .4em; }
a { color: var(--accent); }
nav { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; border-bottom: 1px solid var(--line); padding-bottom: 8px; }
nav form { margin-left: auto; display: flex; gap: 6px; }
input[type=search], input[type=password] { padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }
button { padding: 6px 12px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent); color: var(--bg); cursor: pointer; }
button.secondary { background: transparent; color: var(--accent); }
table { width: 100%; border-collapse: collapse; } td, th { text-align: left; vertical-align: top; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .85rem; }
.muted { color: var(--muted); } .error { color: var(--open); }
.state-open, .state-blocked, .state-waiting { color: var(--open); font-weight: 600; }
.state-in-force { color: var(--amber); font-weight: 600; }
.state-done, .state-noted, .state-answered, .state-no-answer { color: var(--closed); }
code { background: var(--code); padding: 0 3px; border-radius: 3px; font-size: .92em; overflow-wrap: anywhere; }
.body p { margin: .6em 0; overflow-wrap: anywhere; }
ul.parts { padding-left: 1.2em; } label { display: block; margin: 12px 0; }
"""


def inline(text: str) -> str:
    """Escaped text with `code`, **bold** and links to #messages; no other markup passes."""
    out = html.escape(text or "")
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"(?<![\w/&])#(\d+)\b", r"<a href='/board/m/\1'>#\1</a>", out)


def render(text: str) -> str:
    """inline() in paragraphs."""
    return "".join(f"<p>{inline(p).replace(chr(10), '<br>')}</p>" for p in (text or "").split("\n\n") if p.strip())


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def states_html(parts) -> str:
    return "; ".join(f"{esc(p['session'])}: <span class='state-{esc(p['state'])}'>{esc(p['state'])}</span>" for p in parts)


class Web:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        def call() -> Any:
            with db.opened(self.cfg.database) as conn:
                return fn(conn, *args)

        return await anyio.to_thread.run_sync(call)

    async def _secret(self) -> str:
        return await self._run(lambda conn: db.get_meta(conn, "cookie_secret") or "")

    async def _authed(self, request: Request) -> bool:
        return auth.cookie_valid(await self._secret(), request.cookies.get(auth.COOKIE))

    def _frame(self, title: str, body: str, status: int = 200) -> Response:
        nav = (
            "<nav><a href='/board'><strong>ShineBoard</strong></a><a href='/board/tasks'>Tasks</a>"
            "<a href='/board/decisions'>Decisions</a><a href='/board/logout'>Log out</a>"
            "<form method='get' action='/board/find'><input type='search' name='q' placeholder='Search'>"
            "<button type='submit'>Find</button></form></nav>"
        )
        return auth.page(title, nav + body, status)

    def routes(self) -> list[tuple[str, list[str], Callable]]:
        return [
            ("/board/style.css", ["GET"], self.style),
            ("/board/login", ["GET", "POST"], self.login),
            ("/board/logout", ["GET"], self.logout),
            ("/board", ["GET"], self.home),
            ("/board/m/{id:int}", ["GET"], self.message),
            ("/board/s/{name}", ["GET"], self.session),
            ("/board/find", ["GET"], self.find),
            ("/board/tasks", ["GET"], self.tasks),
            ("/board/decisions", ["GET"], self.decisions),
        ]

    async def style(self, request: Request) -> Response:
        return Response(STYLE, media_type="text/css", headers={"Cache-Control": "max-age=3600", "X-Content-Type-Options": "nosniff"})

    async def login(self, request: Request) -> Response:
        nxt = request.query_params.get("next", "/board")
        if not nxt.startswith("/board"):
            nxt = "/board"
        error = ""
        status = 200
        if request.method == "POST":
            form = await request.form()
            nxt = str(form.get("next") or "/board")
            if not nxt.startswith("/board"):
                nxt = "/board"
            failure = await self._run(auth.check_login, auth.client_ip(request), str(form.get("password") or ""))
            if not failure:
                response = RedirectResponse(nxt, status_code=303)
                auth.set_cookie(response, self.cfg, await self._secret())
                return response
            error = f"<p class='error'>{esc(failure)}</p>"
            status = 429 if failure == auth.BLOCKED else 401
        body = (
            f"<h1>ShineBoard</h1>{error}<form method='post' action='/board/login'>"
            f"<input type='hidden' name='next' value='{esc(nxt)}'>"
            "<label>Password <input type='password' name='password' autocomplete='current-password' autofocus></label>"
            "<button type='submit'>Log in</button></form>"
        )
        return auth.page("Log in", body, status if error else 200)

    async def logout(self, request: Request) -> Response:
        response = RedirectResponse("/board/login", status_code=303)
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    def _need_login(self, request: Request) -> Response:
        return RedirectResponse("/board/login?next=" + quote(request.url.path + ("?" + request.url.query if request.url.query else "")), status_code=303)

    def _rows(self, conn, messages) -> str:
        rows = []
        for m in messages:
            parts = board._parts(conn, m["id"])
            rows.append(
                f"<tr><td><a href='/board/m/{m['id']}'>#{m['id']}</a><br><span class='muted'>{esc(m['day'])}</span></td>"
                f"<td><a href='/board/s/{esc(m['sender'])}'>{esc(m['sender'])}</a></td>"
                f"<td><a href='/board/m/{m['id']}'>{esc(m['title'])}</a><br><span class='muted'>{states_html(parts)}</span></td></tr>"
            )
        if not rows:
            return "<p class='muted'>None.</p>"
        return "<table><tr><th>No.</th><th>From</th><th>Message and parts</th></tr>" + "".join(rows) + "</table>"

    async def home(self, request: Request) -> Response:
        if not await self._authed(request):
            return self._need_login(request)

        def fn(conn):
            now = db.utcnow()
            sessions = []
            for s in conn.execute("SELECT * FROM sessions ORDER BY name").fetchall():
                waiting = conn.execute(
                    "SELECT count(*) FROM parts WHERE session = ? AND state IN ('open', 'blocked', 'waiting')", (s["name"],)
                ).fetchone()[0]
                sessions.append((s["name"], waiting, s["last_brief_at"] or "never"))
            recent = conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 400").fetchall()
            active = [m for m in recent if board.is_active(m, board._parts(conn, m["id"]), now)][:80]
            return sessions, self._rows(conn, active)

        sessions, table = await self._run(fn)
        cells = "".join(
            f"<tr><td><a href='/board/s/{esc(n)}'>{esc(n)}</a></td><td class='{'state-open' if w else 'muted'}'>{w}</td>"
            f"<td class='muted'>{esc(b)}</td></tr>"
            for n, w, b in sessions
        )
        body = (
            "<h1>The board</h1><h2>Sessions</h2><table><tr><th>Session</th><th>Waiting for it</th><th>Last brief</th></tr>"
            f"{cells}</table><h2>Active messages</h2>{table}"
        )
        return self._frame("Board", body)

    async def message(self, request: Request) -> Response:
        if not await self._authed(request):
            return self._need_login(request)
        mid = request.path_params["id"]

        def fn(conn):
            m = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            if m is None:
                return None
            parts = board._parts(conn, mid)
            replies = conn.execute("SELECT * FROM messages WHERE reply_to = ? ORDER BY id", (mid,)).fetchall()
            return m, parts, replies

        found = await self._run(fn)
        if found is None:
            return self._frame("Not found", "<h1>No such message</h1>", 404)
        m, parts, replies = found
        items = "".join(
            f"<li><strong>{esc(p['session'])}</strong>: <span class='state-{esc(p['state'])}'>{esc(p['state'])}</span>"
            f"{(' — ' + inline(p['note'])) if p['note'] else ''} <span class='muted'>({esc(p['updated_at'][:10])})</span></li>"
            for p in parts
        )
        reply = f" · reply to <a href='/board/m/{m['reply_to']}'>#{m['reply_to']}</a>" if m["reply_to"] else ""
        rep = (
            "<h2>Replies</h2><ul>" + "".join(f"<li><a href='/board/m/{r['id']}'>#{r['id']}</a> {esc(r['sender'])} — {esc(r['title'])}</li>" for r in replies) + "</ul>"
            if replies else ""
        )
        body = (
            f"<p class='muted'>#{m['id']} · {esc(m['day'])} · <a href='/board/s/{esc(m['sender'])}'>{esc(m['sender'])}</a>"
            f" → {esc(', '.join(p['session'] for p in parts))}{reply} · {esc(m['source'])}</p>"
            f"<h1>{esc(m['title'])}</h1><div class='body'>{render(m['body'])}</div>"
            f"<h2>Parts</h2><ul class='parts'>{items}</ul>{rep}"
        )
        return self._frame(f"#{m['id']}", body)

    async def session(self, request: Request) -> Response:
        if not await self._authed(request):
            return self._need_login(request)
        name = request.path_params["name"].upper()

        def fn(conn):
            if conn.execute("SELECT 1 FROM sessions WHERE name = ?", (name,)).fetchone() is None:
                return None
            waiting = conn.execute(
                "SELECT m.* FROM parts p JOIN messages m ON m.id = p.message_id WHERE p.session = ? "
                "AND p.state IN ('open', 'blocked', 'waiting') ORDER BY m.id DESC", (name,)
            ).fetchall()
            theirs = conn.execute(
                "SELECT DISTINCT m.* FROM messages m JOIN parts p ON p.message_id = m.id WHERE m.sender = ? "
                "AND p.state IN ('open', 'blocked', 'waiting') ORDER BY m.id DESC", (name,)
            ).fetchall()
            recent = conn.execute("SELECT * FROM messages WHERE sender = ? ORDER BY id DESC LIMIT 30", (name,)).fetchall()
            return self._rows(conn, waiting), self._rows(conn, theirs), self._rows(conn, recent)

        found = await self._run(fn)
        if found is None:
            return self._frame("Not found", "<h1>No such session</h1>", 404)
        waiting, theirs, recent = found
        body = (
            f"<h1>{esc(name)}</h1><h2>Waiting for {esc(name)}</h2>{waiting}"
            f"<h2>{esc(name)}'s messages others have open</h2>{theirs}<h2>Sent by {esc(name)}, newest</h2>{recent}"
        )
        return self._frame(name, body)

    async def find(self, request: Request) -> Response:
        if not await self._authed(request):
            return self._need_login(request)
        q = request.query_params.get("q", "").strip()

        def fn(conn):
            fts = board._fts(q)
            if not fts:
                return None
            rows = conn.execute(
                "SELECT m.* FROM search JOIN messages m ON m.id = CAST(search.ref AS INTEGER) WHERE search MATCH ? "
                "AND search.kind = 'message' ORDER BY bm25(search, 0, 0, 5.0, 1.0), m.id DESC LIMIT 100",
                (fts,),
            ).fetchall()
            return self._rows(conn, rows)

        table = await self._run(fn) if q else None
        body = f"<h1>Search: {esc(q)}</h1>" + (table if table is not None else "<p class='muted'>Give words to find.</p>")
        return self._frame("Search", body)

    async def tasks(self, request: Request) -> Response:
        if not await self._authed(request):
            return self._need_login(request)
        rows = await self._run(lambda conn: conn.execute("SELECT * FROM tasks ORDER BY status IN ('done', 'dropped'), area, id").fetchall())
        cells = "".join(
            f"<tr><td>{t['id']}</td><td>{esc(t['area'])}</td><td>{esc(t['ref'] or '')}</td>"
            f"<td class='state-{esc(t['status'])}'>{esc(t['status'])}</td><td>{esc(t['title'])}"
            f"{('<br><span class=muted>' + esc(t['detail']) + '</span>') if t['detail'] else ''}</td></tr>"
            for t in rows
        )
        body = "<h1>Tasks</h1>" + (
            f"<table><tr><th>No.</th><th>Session</th><th>Ref</th><th>Status</th><th>Task</th></tr>{cells}</table>" if rows else "<p class='muted'>None.</p>"
        )
        return self._frame("Tasks", body)

    async def decisions(self, request: Request) -> Response:
        if not await self._authed(request):
            return self._need_login(request)
        rows = await self._run(lambda conn: conn.execute("SELECT * FROM decisions ORDER BY decided_on DESC, id DESC").fetchall())
        cells = "".join(
            f"<tr><td>{esc(d['decided_on'])}</td><td><strong>{esc(d['title'])}</strong> <span class='muted'>({esc(d['key'])}"
            f"{', ' + esc(d['status']) if d['status'] != 'in-force' else ''})</span><div class='body'>{render(d['decision'])}</div>"
            f"<span class='muted'>{esc(d['areas'])}{' · ' + esc(d['home']) if d['home'] else ''}</span></td></tr>"
            for d in rows
        )
        body = "<h1>Decisions</h1>" + (
            f"<table><tr><th>Decided</th><th>Decision</th></tr>{cells}</table>" if rows else "<p class='muted'>None.</p>"
        )
        return self._frame("Decisions", body)
