# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The board: messages and their parts, tasks and decisions.

A message goes from one session to one or more sessions, or to all. Each
recipient has a part of its status, which it keeps (open, noted, done, ...); a
message stays active while a part is open or in force, and for seven days after
its last change, as the Markdown board's lines did (claude/working-agreement.md,
"Handoff and session log"). Nothing is ever moved or deleted: search finds the
old messages too.

Every function takes an open connection and returns text for the tools, short
and in the order a session needs it.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from zoneinfo import ZoneInfo

from . import db

OPEN_STATES = ("open", "blocked", "waiting")
CLOSED_STATES = ("noted", "done", "answered", "declined", "superseded", "withdrawn", "no-answer")
STATES = OPEN_STATES + CLOSED_STATES + ("in-force",)
TASK_STATES = ("open", "in-progress", "blocked", "done", "dropped")
DECISION_STATES = ("in-force", "superseded", "revoked")

ACTIVE_DAYS = 7
FIRST_BRIEF_DAYS = 14
MAX_TITLE = 300
MAX_BODY = 40_000
MAX_NOTE = 4_000
LEAD = 170

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,59}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BoardError(ValueError):
    """A request the board refuses; its text goes back to the session."""


# --- helpers -----------------------------------------------------------------


def known_sessions(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM sessions ORDER BY name")]


def board_day(conn: sqlite3.Connection, now: dt.datetime) -> str:
    """The date of a moment on the board's calendar: the zone the settings name
    (day_zone, kept in the database's meta), UTC when none is set."""
    name = db.get_meta(conn, "day_zone") or "UTC"
    try:
        return now.astimezone(ZoneInfo(name)).date().isoformat()
    except (KeyError, ValueError, OSError):
        return now.astimezone(dt.UTC).date().isoformat()


def session_name(conn: sqlite3.Connection, name: str, allow_all: bool = False) -> str:
    n = (name or "").strip().upper()
    if allow_all and n == "ALL":
        return "all"
    if conn.execute("SELECT 1 FROM sessions WHERE name = ?", (n,)).fetchone() is None:
        names = ", ".join(known_sessions(conn))
        raise BoardError(f"unknown session {name!r}; the sessions are {names}" + (", or all" if allow_all else ""))
    return n


def one_line(s: str) -> str:
    return " ".join((s or "").split())


def lead(body: str, n: int = LEAD) -> str:
    text = one_line(body)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def audit(conn: sqlite3.Connection, actor: str, client: str, action: str, target: str = "", detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit(at, actor, client, action, target, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (db.iso(db.utcnow()), actor, client, action, target, detail[:500]),
    )


def _parts(conn: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    # 'all' first, then the sessions in the order they were addressed.
    return conn.execute(
        "SELECT * FROM parts WHERE message_id = ? ORDER BY session = 'all' DESC, rowid",
        (message_id,),
    ).fetchall()


def _to(parts: list[sqlite3.Row]) -> str:
    return ", ".join(p["session"] for p in parts)


def _states(parts: list[sqlite3.Row]) -> str:
    return "; ".join(f"{p['session']}: {p['state']}" for p in parts)


def _newest(msg: sqlite3.Row, parts: list[sqlite3.Row]) -> dt.datetime:
    times = [msg["updated_at"]] + [p["updated_at"] for p in parts]
    return max(db.parse_iso(t) for t in times)


def is_active(msg: sqlite3.Row, parts: list[sqlite3.Row], now: dt.datetime) -> bool:
    if any(p["state"] in OPEN_STATES or p["state"] == "in-force" for p in parts):
        return True
    return (now - _newest(msg, parts)).days < ACTIVE_DAYS


def reindex_message(conn: sqlite3.Connection, message_id: int) -> None:
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    conn.execute("DELETE FROM search WHERE kind = 'message' AND ref = ?", (str(message_id),))
    if msg is None:
        return
    notes = " ".join(f"{p['session']}: {p['state']} {p['note']}" for p in _parts(conn, message_id))
    conn.execute(
        "INSERT INTO search(kind, ref, title, body) VALUES ('message', ?, ?, ?)",
        (str(message_id), msg["title"], f"{msg['sender']} {msg['body']} {notes}"),
    )


def _head(msg: sqlite3.Row, parts: list[sqlite3.Row]) -> str:
    return f"#{msg['id']} {msg['day']} {msg['sender']} → {_to(parts)} — {msg['title']}"


# --- messages ----------------------------------------------------------------


def post(
    conn: sqlite3.Connection,
    sender: str,
    to: list[str],
    title: str,
    body: str,
    reply_to: int | None = None,
    all_until: str = "",
    client: str = "",
    now: dt.datetime | None = None,
) -> str:
    now = now or db.utcnow()
    sender = session_name(conn, sender)
    recipients: list[str] = []
    for name in to or []:
        r = session_name(conn, name, allow_all=True)
        if r == sender:
            raise BoardError("a session does not address itself: name the sessions the message is for")
        if r not in recipients:
            recipients.append(r)
    if not recipients:
        raise BoardError("name at least one recipient, or all")
    title = one_line(title)
    if not 3 <= len(title) <= MAX_TITLE:
        raise BoardError(f"the title is one line of 3 to {MAX_TITLE} characters")
    body = (body or "").strip()
    if len(body) > MAX_BODY:
        raise BoardError(f"the body is {len(body)} characters; at most {MAX_BODY}: put the details in your log or a doc, and point to it")
    if reply_to is not None and conn.execute("SELECT 1 FROM messages WHERE id = ?", (reply_to,)).fetchone() is None:
        raise BoardError(f"no message #{reply_to} to reply to")
    if all_until and "all" not in recipients:
        raise BoardError("all_until is for a notice to all")
    t = db.iso(now)
    with db.write(conn):
        cur = conn.execute(
            "INSERT INTO messages(day, posted_at, sender, title, body, reply_to, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'mcp', ?)",
            (board_day(conn, now), t, sender, title, body, reply_to, t),
        )
        mid = cur.lastrowid
        for r in recipients:
            if r == "all":
                state, note = ("in-force", f"until {one_line(all_until)}") if all_until else ("no-answer", "")
            else:
                state, note = "open", ""
            conn.execute(
                "INSERT INTO parts(message_id, session, state, note, updated_at) VALUES (?, ?, ?, ?, ?)",
                (mid, r, state, note, t),
            )
        reindex_message(conn, mid)
        audit(conn, sender, client, "post", f"#{mid}", title)
    return f"Posted #{mid}: {sender} → {', '.join(recipients)} — {title}"


def set_part(
    conn: sqlite3.Connection,
    message_id: int,
    session: str,
    state: str,
    note: str = "",
    part: str = "",
    client: str = "",
    now: dt.datetime | None = None,
) -> str:
    now = now or db.utcnow()
    session = session_name(conn, session)
    state = (state or "").strip().lower()
    if state not in STATES:
        raise BoardError(f"unknown state {state!r}; the states are {', '.join(STATES)}")
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise BoardError(f"the note is {len(note)} characters; at most {MAX_NOTE}")
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if msg is None:
        raise BoardError(f"no message #{message_id}")
    target = session_name(conn, part, allow_all=True) if part else session
    t = db.iso(now)
    with db.write(conn):
        if state == "withdrawn":
            if session != msg["sender"]:
                raise BoardError(f"only the sender, {msg['sender']}, withdraws #{message_id}")
            conn.execute(
                "UPDATE parts SET state = 'withdrawn', note = ?, updated_at = ? WHERE message_id = ? "
                "AND state IN ('open', 'blocked', 'waiting', 'in-force')",
                (note, t, message_id),
            )
            text = f"#{message_id} withdrawn"
        else:
            if target == "all":
                if session != msg["sender"]:
                    raise BoardError(f"the part of all is the sender's, {msg['sender']}'s, to keep")
                if state not in ("in-force", "no-answer", "done"):
                    raise BoardError("the part of all is in-force, no-answer or done")
            elif target != session:
                raise BoardError("a session keeps its own part: set part only to all, as the sender")
            elif state == "in-force":
                raise BoardError("in-force is for the part of all")
            exists = conn.execute(
                "SELECT 1 FROM parts WHERE message_id = ? AND session = ?", (message_id, target)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE parts SET state = ?, note = ?, updated_at = ? WHERE message_id = ? AND session = ?",
                    (state, note, t, message_id, target),
                )
            else:
                if target == msg["sender"]:
                    raise BoardError(f"{target} sent #{message_id}: post a new message instead, or withdraw it")
                conn.execute(
                    "INSERT INTO parts(message_id, session, state, note, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (message_id, target, state, note, t),
                )
            text = f"#{message_id} {target}: {state}" + (f" ({lead(note, 80)})" if note else "")
        conn.execute("UPDATE messages SET updated_at = ? WHERE id = ?", (t, message_id))
        reindex_message(conn, message_id)
        audit(conn, session, client, "set_part", f"#{message_id}", f"{target}: {state}")
    return text


def read(conn: sqlite3.Connection, ids: list[int]) -> str:
    if not ids:
        raise BoardError("name the messages to read, such as ids=[412]")
    if len(ids) > 10:
        raise BoardError("read at most 10 messages at a time")
    out = []
    for mid in ids:
        msg = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        if msg is None:
            out.append(f"#{mid}: no such message")
            continue
        parts = _parts(conn, mid)
        head = f"#{mid} · {msg['day']} · {msg['sender']} → {_to(parts)}"
        if msg["reply_to"]:
            head += f" · reply to #{msg['reply_to']}"
        if msg["source"] != "mcp":
            head += f" · {msg['source']}"
        lines = [head, f"**{msg['title']}**", "", msg["body"], "", "Parts:"]
        for p in parts:
            note = f" — {p['note']}" if p["note"] else ""
            lines.append(f"- {p['session']}: {p['state']}{note} ({p['updated_at'][:10]})")
        replies = conn.execute("SELECT id, sender, title FROM messages WHERE reply_to = ? ORDER BY id", (mid,)).fetchall()
        if replies:
            lines.append("Replies: " + "; ".join(f"#{r['id']} {r['sender']} — {lead(r['title'], 80)}" for r in replies))
        out.append("\n".join(lines))
    return "\n\n---\n\n".join(out)


# --- the brief ---------------------------------------------------------------


def brief(conn: sqlite3.Connection, session: str, now: dt.datetime | None = None, update_seen: bool = True) -> str:
    """What a session needs at its start, in a few kilobytes."""
    now = now or db.utcnow()
    session = session_name(conn, session)
    row = conn.execute("SELECT last_brief_at FROM sessions WHERE name = ?", (session,)).fetchone()
    since = db.parse_iso(row["last_brief_at"]) if row and row["last_brief_at"] else now - dt.timedelta(days=FIRST_BRIEF_DAYS)
    since_s = db.iso(since)
    out = [f"ShineBoard · {session} · {db.iso(now)}" + (f" · last brief {since_s}" if row and row["last_brief_at"] else " · first brief")]

    # 1. Messages waiting for this session.
    inbox = conn.execute(
        "SELECT m.*, p.state AS mystate FROM parts p JOIN messages m ON m.id = p.message_id "
        "WHERE p.session = ? AND p.state IN ('open', 'blocked', 'waiting') ORDER BY m.id DESC",
        (session,),
    ).fetchall()
    out.append("")
    out.append(f"Waiting for {session} ({len(inbox)}):" if inbox else f"Waiting for {session}: nothing.")
    for i, m in enumerate(inbox):
        parts = _parts(conn, m["id"])
        mark = "" if m["mystate"] == "open" else f" [{m['mystate']}]"
        if i < 10:
            out.append(f"- {_head(m, parts)}{mark}")
            out.append(f"  {lead(m['body'])}")
        else:
            out.append(f"- #{m['id']} {m['sender']} — {lead(m['title'], 90)}{mark}")

    # 2. Notices to all since the last brief, and those in force.
    notices = conn.execute(
        "SELECT m.*, p.state AS allstate, p.note AS allnote FROM parts p JOIN messages m ON m.id = p.message_id "
        "WHERE p.session = 'all' AND m.sender != ? AND (m.posted_at > ? OR p.state = 'in-force') ORDER BY m.id DESC LIMIT 11",
        (session, since_s),
    ).fetchall()
    if notices:
        out.append("")
        out.append(f"To all, new since your last brief or in force ({'10+' if len(notices) > 10 else len(notices)}):")
        for m in notices[:10]:
            force = f" [in force {m['allnote']}]" if m["allstate"] == "in-force" else ""
            out.append(f"- #{m['id']} {m['day']} {m['sender']} — {lead(m['title'], 110)}{force}")

    # 3. Answers to this session's messages since its last brief.
    answers = conn.execute(
        "SELECT m.id, m.title, p.session, p.state, p.note FROM parts p JOIN messages m ON m.id = p.message_id "
        "WHERE m.sender = ? AND p.session != 'all' AND p.updated_at > ? AND p.state NOT IN ('open') "
        "ORDER BY p.updated_at DESC LIMIT 13",
        (session, since_s),
    ).fetchall()
    if answers:
        out.append("")
        out.append(f"Answers to your messages since your last brief ({'12+' if len(answers) > 12 else len(answers)}):")
        for a in answers[:12]:
            note = f" — {lead(a['note'], 90)}" if a["note"] else ""
            out.append(f"- #{a['id']} {lead(a['title'], 60)} · {a['session']}: {a['state']}{note}")

    # 4. This session's messages that others still have open.
    pending = conn.execute(
        "SELECT m.id, m.title, group_concat(p.session, ', ') AS who FROM messages m JOIN parts p ON p.message_id = m.id "
        "WHERE m.sender = ? AND p.state IN ('open', 'blocked', 'waiting') GROUP BY m.id ORDER BY m.id DESC LIMIT 13",
        (session,),
    ).fetchall()
    if pending:
        out.append("")
        out.append(f"Your messages others have open ({'12+' if len(pending) > 12 else len(pending)}):")
        for p in pending[:12]:
            out.append(f"- #{p['id']} {lead(p['title'], 80)} · open: {p['who']}")

    # 5. This session's tasks.
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE area = ? AND status NOT IN ('done', 'dropped') ORDER BY id LIMIT 25", (session,)
    ).fetchall()
    if tasks:
        out.append("")
        out.append(f"Your tasks ({len(tasks)}):")
        for t in tasks:
            ref = f"{t['ref']} " if t["ref"] else ""
            out.append(f"- task {t['id']} {ref}[{t['status']}] {lead(t['title'], 110)}")

    # 6. Decisions since the last brief.
    decisions = conn.execute(
        "SELECT * FROM decisions WHERE recorded_at > ? AND status = 'in-force' ORDER BY decided_on DESC, id DESC LIMIT 15",
        (since_s,),
    ).fetchall()
    if decisions:
        out.append("")
        out.append(f"Decisions recorded since your last brief ({len(decisions)}):")
        for d in decisions:
            out.append(f"- {d['key']} ({d['decided_on']}, {d['decided_by']}): {lead(d['title'], 120)}")

    out.append("")
    out.append("Next: board_read(ids=[…]) for a message in full; board_set_part(id=…, session=\""
               f"{session}\", state=\"done\", note=\"…\") once answered; board_post(…) for a new message.")
    if update_seen:
        with db.write(conn):
            conn.execute("UPDATE sessions SET last_brief_at = ? WHERE name = ?", (db.iso(now), session))
    return "\n".join(out)


# --- search ------------------------------------------------------------------


def _fts(query: str) -> str:
    """The query as FTS5 understands it: the words, each as a quoted term."""
    words = re.findall(r"[\w@][\w@./:+-]*", query, flags=re.UNICODE)
    return " ".join('"' + w.replace('"', '""') + '"' for w in words)


def find(
    conn: sqlite3.Connection,
    query: str = "",
    sender: str = "",
    to: str = "",
    state: str = "",
    since: str = "",
    until: str = "",
    active_only: bool = False,
    limit: int = 20,
    now: dt.datetime | None = None,
) -> str:
    now = now or db.utcnow()
    limit = max(1, min(int(limit or 20), 50))
    where, args = [], []
    if sender:
        where.append("m.sender = ?")
        args.append(session_name(conn, sender))
    if to:
        where.append("EXISTS (SELECT 1 FROM parts q WHERE q.message_id = m.id AND q.session = ?" + (" AND q.state = ?)" if state else ")"))
        args.append(session_name(conn, to, allow_all=True))
        if state:
            args.append(state)
    elif state:
        where.append("EXISTS (SELECT 1 FROM parts q WHERE q.message_id = m.id AND q.state = ?)")
        args.append(state)
    for value, op in ((since, ">="), (until, "<=")):
        if value:
            if not DAY_RE.match(value):
                raise BoardError("since and until are dates, YYYY-MM-DD")
            where.append(f"m.day {op} ?")
            args.append(value)
    cond = (" AND " + " AND ".join(where)) if where else ""
    fts = _fts(query) if query else ""
    if query and not fts:
        raise BoardError("the query has no words to search for")
    if fts:
        rows = conn.execute(
            "SELECT m.*, snippet(search, 3, '[', ']', '…', 14) AS snip FROM search "
            "JOIN messages m ON m.id = CAST(search.ref AS INTEGER) "
            f"WHERE search MATCH ? AND search.kind = 'message'{cond} ORDER BY bm25(search, 0, 0, 5.0, 1.0), m.id DESC LIMIT ?",
            [fts] + args + [limit * 4 if active_only else limit],
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT m.*, '' AS snip FROM messages m WHERE 1 = 1{cond} ORDER BY m.id DESC LIMIT ?",
            args + [limit * 4 if active_only else limit],
        ).fetchall()
    out = []
    for m in rows:
        parts = _parts(conn, m["id"])
        if active_only and not is_active(m, parts, now):
            continue
        out.append(f"- {_head(m, parts)} [{_states(parts)}]")
        if m["snip"]:
            out.append(f"  {one_line(m['snip'])}")
        if len(out) >= limit * 2:
            break
    if not out:
        return "No messages found."
    n = sum(1 for line in out if line.startswith("- "))
    return f"{n} message{'s' if n != 1 else ''}, newest first" + (" by relevance" if fts else "") + ":\n" + "\n".join(out)


def overview(conn: sqlite3.Connection, now: dt.datetime | None = None) -> str:
    """Each session's open parts, and its own messages others have open."""
    now = now or db.utcnow()
    lines = ["Session · waiting for it · its messages others have open · last brief"]
    for s in conn.execute("SELECT * FROM sessions ORDER BY name").fetchall():
        name = s["name"]
        waiting = conn.execute(
            "SELECT count(*) FROM parts WHERE session = ? AND state IN ('open', 'blocked', 'waiting')", (name,)
        ).fetchone()[0]
        theirs = conn.execute(
            "SELECT count(DISTINCT m.id) FROM messages m JOIN parts p ON p.message_id = m.id "
            "WHERE m.sender = ? AND p.state IN ('open', 'blocked', 'waiting')", (name,)
        ).fetchone()[0]
        lines.append(f"- {name}: {waiting} · {theirs} · {s['last_brief_at'] or 'never'}")
    active = 0
    for m in conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 2000").fetchall():
        if is_active(m, _parts(conn, m["id"]), now):
            active += 1
    total = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    lines.append(f"Messages: {active} active of {total}.")
    return "\n".join(lines)


# --- tasks -------------------------------------------------------------------


def tasks(conn: sqlite3.Connection, session: str = "", status: str = "", include_closed: bool = False) -> str:
    where, args = [], []
    if session:
        where.append("area = ?")
        args.append(session_name(conn, session))
    if status:
        if status not in TASK_STATES:
            raise BoardError(f"the task states are {', '.join(TASK_STATES)}")
        where.append("status = ?")
        args.append(status)
    elif not include_closed:
        where.append("status NOT IN ('done', 'dropped')")
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT * FROM tasks{cond} ORDER BY area, id LIMIT 200", args).fetchall()
    if not rows:
        return "No tasks."
    out = []
    for t in rows:
        ref = f"{t['ref']} " if t["ref"] else ""
        msg = f" (from #{t['message_id']})" if t["message_id"] else ""
        out.append(f"- task {t['id']} {t['area']} {ref}[{t['status']}] {t['title']}{msg}")
        if t["detail"]:
            out.append(f"  {lead(t['detail'], 200)}")
    return "\n".join(out)


def set_task(
    conn: sqlite3.Connection,
    session: str,
    title: str = "",
    status: str = "",
    ref: str = "",
    task_id: int | None = None,
    detail: str | None = None,
    message_id: int | None = None,
    client: str = "",
    now: dt.datetime | None = None,
) -> str:
    now = now or db.utcnow()
    session = session_name(conn, session)
    t = db.iso(now)
    if status and status not in TASK_STATES:
        raise BoardError(f"the task states are {', '.join(TASK_STATES)}")
    ref = (ref or "").strip()
    title = one_line(title)
    if title and len(title) > MAX_TITLE:
        raise BoardError(f"the title is at most {MAX_TITLE} characters")
    if detail is not None and len(detail) > MAX_NOTE:
        raise BoardError(f"the detail is at most {MAX_NOTE} characters")
    if message_id is not None and conn.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone() is None:
        raise BoardError(f"no message #{message_id}")
    with db.write(conn):
        row = None
        if task_id is not None:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise BoardError(f"no task {task_id}")
        elif ref:
            row = conn.execute("SELECT * FROM tasks WHERE ref = ?", (ref,)).fetchone()
        if row is not None:
            if row["area"] != session:
                raise BoardError(f"task {row['id']} is {row['area']}'s")
            conn.execute(
                "UPDATE tasks SET title = ?, status = ?, detail = ?, ref = ?, message_id = ?, updated_at = ? WHERE id = ?",
                (
                    title or row["title"],
                    status or row["status"],
                    row["detail"] if detail is None else detail.strip(),
                    ref or row["ref"],
                    row["message_id"] if message_id is None else message_id,
                    t,
                    row["id"],
                ),
            )
            tid, verb = row["id"], "updated"
        else:
            if not title:
                raise BoardError("a new task needs a title")
            cur = conn.execute(
                "INSERT INTO tasks(ref, area, title, status, detail, message_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ref or None, session, title, status or "open", (detail or "").strip(), message_id, t, t),
            )
            tid, verb = cur.lastrowid, "created"
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        conn.execute("DELETE FROM search WHERE kind = 'task' AND ref = ?", (str(tid),))
        conn.execute(
            "INSERT INTO search(kind, ref, title, body) VALUES ('task', ?, ?, ?)",
            (str(tid), task["title"], f"{task['area']} {task['ref'] or ''} {task['detail']}"),
        )
        audit(conn, session, client, f"task_{verb}", f"task {tid}", f"{task['status']}: {task['title']}")
    return f"Task {tid} {verb}: {task['area']} {task['ref'] or ''} [{task['status']}] {task['title']}".replace("  ", " ")


# --- decisions ---------------------------------------------------------------


def decisions(conn: sqlite3.Connection, query: str = "", area: str = "", since: str = "", include_old: bool = False) -> str:
    where, args = [], []
    if not include_old:
        where.append("d.status = 'in-force'")
    if area:
        a = session_name(conn, area)
        where.append("(',' || d.areas || ',') LIKE ?")
        args.append(f"%,{a},%")
    if since:
        if not DAY_RE.match(since):
            raise BoardError("since is a date, YYYY-MM-DD")
        where.append("d.decided_on >= ?")
        args.append(since)
    cond = (" AND " + " AND ".join(where)) if where else ""
    if query:
        fts = _fts(query)
        if not fts:
            raise BoardError("the query has no words to search for")
        rows = conn.execute(
            "SELECT d.* FROM search JOIN decisions d ON d.id = CAST(search.ref AS INTEGER) "
            f"WHERE search MATCH ? AND search.kind = 'decision'{cond} ORDER BY bm25(search, 0, 0, 5.0, 1.0) LIMIT 30",
            [fts] + args,
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT d.* FROM decisions d WHERE 1 = 1{cond} ORDER BY d.decided_on DESC, d.id DESC LIMIT 50", args).fetchall()
    if not rows:
        return "No decisions found."
    out = []
    for d in rows:
        status = "" if d["status"] == "in-force" else f" [{d['status']}" + (f" by {d['superseded_by']}]" if d["superseded_by"] else "]")
        out.append(f"- {d['key']} · {d['decided_on']} · {d['decided_by']}{status}: **{d['title']}**")
        out.append(f"  {d['decision']}")
        meta = []
        if d["areas"]:
            meta.append(f"areas {d['areas']}")
        if d["home"]:
            meta.append(f"recorded in {d['home']}")
        if meta:
            out.append("  (" + "; ".join(meta) + ")")
    return "\n".join(out)


def record_decision(
    conn: sqlite3.Connection,
    session: str,
    key: str,
    title: str,
    decision: str,
    decided_on: str,
    areas: list[str] | None = None,
    home: str = "",
    decided_by: str = "user",
    supersedes: str = "",
    client: str = "",
    now: dt.datetime | None = None,
) -> str:
    now = now or db.utcnow()
    session = session_name(conn, session)
    key = (key or "").strip().lower()
    if not KEY_RE.match(key):
        raise BoardError("the key is 2 to 60 characters of a-z, 0-9, '.' and '-', such as security-contact")
    title = one_line(title)
    if not 3 <= len(title) <= MAX_TITLE:
        raise BoardError(f"the title is one line of 3 to {MAX_TITLE} characters")
    decision = (decision or "").strip()
    if not decision or len(decision) > MAX_NOTE:
        raise BoardError(f"the decision is 1 to {MAX_NOTE} characters: what was decided, in a few sentences")
    if not DAY_RE.match(decided_on or ""):
        raise BoardError("decided_on is a date, YYYY-MM-DD")
    names = [session_name(conn, a) for a in (areas or [])]
    t = db.iso(now)
    with db.write(conn):
        if supersedes:
            old = conn.execute("SELECT id FROM decisions WHERE key = ?", (supersedes,)).fetchone()
            if old is None:
                raise BoardError(f"no decision {supersedes!r} to supersede")
            conn.execute("UPDATE decisions SET status = 'superseded', superseded_by = ? WHERE key = ?", (key, supersedes))
        conn.execute(
            "INSERT INTO decisions(key, title, decision, decided_on, decided_by, areas, home, status, recorded_by, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'in-force', ?, ?) ON CONFLICT(key) DO UPDATE SET title = excluded.title, "
            "decision = excluded.decision, decided_on = excluded.decided_on, decided_by = excluded.decided_by, "
            "areas = excluded.areas, home = excluded.home, status = 'in-force', superseded_by = NULL, "
            "recorded_by = excluded.recorded_by, recorded_at = excluded.recorded_at",
            (key, title, decision, decided_on, one_line(decided_by) or "user", ",".join(names), one_line(home), session, t),
        )
        d = conn.execute("SELECT * FROM decisions WHERE key = ?", (key,)).fetchone()
        conn.execute("DELETE FROM search WHERE kind = 'decision' AND ref = ?", (str(d["id"]),))
        conn.execute(
            "INSERT INTO search(kind, ref, title, body) VALUES ('decision', ?, ?, ?)",
            (str(d["id"]), title, f"{key} {decision} {d['areas']} {d['home']}"),
        )
        audit(conn, session, client, "decision", key, title)
    return f"Decision {key} recorded ({decided_on}, {decided_by}): {title}" + (f"; {supersedes} superseded" if supersedes else "")
