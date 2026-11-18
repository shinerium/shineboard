# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The Markdown board in and out: claude/session-board.md and its archive.

A line | date | from | to | notice | status | becomes a message; its status, the
recipients' parts. The parts are read as COM's scripts/board-archive.py in the
node's repository reads them: a part starts with its sessions' names at the
start of the status or after "; " or ". ", outside parentheses and code. An
import can run again: a line already imported has its parts brought up to date.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3

from . import board, db

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTHS = {m: i for i, m in enumerate(MONTH_NAMES, 1)}
DATE_RE = re.compile(r"\b(\d{1,2}) (" + "|".join(MONTHS) + r")\b")
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
OPEN_RE = re.compile(r"\b(?:open|BLOCKED|waits?|pending)\b")
FORCE_RE = re.compile(r"\bin force until\b")
LABEL_RE = re.compile(r"(?:^|(?<=; )|(?<=\. ))([A-Za-z0-9]+(?:, [A-Za-z0-9]+)*): ")
LEAD_WORDS = {
    "done": "done", "noted": "noted", "answered": "answered", "declined": "declined",
    "superseded": "superseded", "withdrawn": "withdrawn", "decided": "done", "agreed": "noted",
    "no answer needed": "no-answer", "open": "open", "blocked": "blocked", "waiting": "waiting",
}


class Row:
    def __init__(self, line: str):
        body = line.strip()
        if not (body.startswith("| ") and body.endswith(" |")):
            raise ValueError("not a table row")
        cells = body[2:-2].split(" | ")
        if len(cells) < 5:
            raise ValueError("fewer than five cells")
        self.line = body
        self.date = dt.date.fromisoformat(cells[0].strip())
        self.sender = cells[1].strip()
        self.to = [t.strip() for t in cells[2].split(",") if t.strip()]
        self.status = cells[-1].strip()
        self.notice = " | ".join(cells[3:-1]).strip()


def rows(text: str) -> list[Row]:
    """The table's rows, in the file's order (newest first)."""
    out, seen_sep = [], False
    for line in text.splitlines():
        if line.startswith("| --- |"):
            seen_sep = True
            continue
        if seen_sep and line.startswith("| 20"):
            out.append(Row(line))
    return out


def top_level(s: str) -> str:
    """s with what is in parentheses or `code` blanked out, the same length."""
    out, depth, code = [], 0, False
    for ch in s:
        if ch == "`":
            code = not code
            out.append(" ")
        elif code:
            out.append(" ")
        elif ch == "(":
            depth += 1
            out.append(" ")
        elif ch == ")" and depth:
            depth -= 1
            out.append(" ")
        else:
            out.append(ch if depth == 0 else " ")
    return "".join(out)


def parts_of(row: Row) -> dict[str, str | None]:
    """{recipient: its part of the status, or None when the status names it nowhere}."""
    flat = top_level(row.status)
    labels = []
    for m in LABEL_RE.finditer(flat):
        names = [t.strip() for t in m.group(1).split(",")]
        if all(n in row.to for n in names):
            labels.append((m.start(1), m.end(), names))
    default = row.status[: labels[0][0]] if labels else row.status
    out = {r: (default if default.strip(" ;.") else None) for r in row.to}
    for i, (_, end, names) in enumerate(labels):
        stop = labels[i + 1][0] if i + 1 < len(labels) else len(row.status)
        for n in names:
            out[n] = row.status[end:stop]
    return out


def newest_date(text: str, start: dt.date, today: dt.date) -> dt.date:
    best = start
    for m in DATE_RE.finditer(text):
        d_, month = int(m.group(1)), MONTHS[m.group(2)]
        for year in (today.year, today.year - 1):
            try:
                d = dt.date(year, month, d_)
            except ValueError:
                break
            if d <= today:
                best = max(best, d)
                break
    for m in ISO_RE.finditer(text):
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d <= today:
            best = max(best, d)
    return best


def classify(recipient: str, text: str | None) -> tuple[str, str]:
    """A part's text as (state, note)."""
    if text is None:
        return ("no-answer", "") if recipient == "all" else ("open", "")
    t = text.strip().rstrip(";").strip().rstrip(".").strip()
    top = top_level(t)
    if recipient == "all":
        state = "in-force" if FORCE_RE.search(top) else "no-answer"
    elif OPEN_RE.search(top):
        state = "blocked" if "BLOCKED" in top else ("waiting" if re.search(r"\b(?:waits?|pending)\b", top) else "open")
    else:
        lowered = t.lower()
        state = "noted"
        for word, s in LEAD_WORDS.items():
            if lowered.startswith(word):
                state = s
                break
    note = t
    m = re.match(r"^([A-Za-z][A-Za-z -]*?)\s*\((.*)\)$", t, re.S)
    if m and m.group(1).lower() in LEAD_WORDS:
        note = m.group(2).strip()
    elif t.lower() in LEAD_WORDS:
        note = ""
    return state, note


def title_and_body(notice: str) -> tuple[str, str]:
    m = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", notice, re.S)
    if m:
        return board.one_line(m.group(1)), m.group(2).strip()
    first = re.split(r"(?<=[.:])\s", notice, maxsplit=1)[0]
    title = board.lead(first, 160)
    return title, notice


def import_text(
    conn: sqlite3.Connection, text: str, source: str, today: dt.date | None = None
) -> tuple[dict[str, int], list[str]]:
    """Imports the table in text; returns counts of what it did, and the lines it could not take whole."""
    today = today or db.utcnow().date()
    known = set(board.known_sessions(conn))
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    problems: list[str] = []
    for row in reversed(rows(text)):  # oldest first, so that numbers grow with time
        m = re.match(r"([A-Za-z0-9]+)", row.sender)
        sender = m.group(1).upper() if m else ""
        if sender not in known:
            counts["skipped"] += 1
            problems.append(f"{row.date} {row.sender}: unknown sender")
            continue
        recipients = []
        for r in row.to:
            name = "all" if r.lower() == "all" else r.upper()
            if name == "all" or name in known:
                recipients.append((r, name))
            else:
                problems.append(f"{row.date} {row.sender} → {r}: unknown recipient, left out")
        if not recipients:
            counts["skipped"] += 1
            continue
        key = hashlib.sha256(f"{row.date}|{row.sender}|{', '.join(row.to)}|{row.notice}".encode()).hexdigest()
        title, body = title_and_body(row.notice)
        if row.sender != sender:
            body = f"(From: {row.sender}.) " + body
        texts = parts_of(row)
        stamp = f"{row.date.isoformat()}T12:00:00Z"
        with db.write(conn):
            existing = conn.execute("SELECT id FROM messages WHERE import_key = ?", (key,)).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO messages(day, posted_at, sender, title, body, source, import_key, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row.date.isoformat(), stamp, sender, title, body, source, key, stamp),
                )
                mid, changed = cur.lastrowid, True
                counts["added"] += 1
            else:
                mid, changed = existing["id"], False
            for raw, name in recipients:
                state, note = classify(name, texts.get(raw))
                when = newest_date(texts.get(raw) or "", row.date, today)
                t = f"{when.isoformat()}T12:00:00Z"
                old = conn.execute("SELECT state, note FROM parts WHERE message_id = ? AND session = ?", (mid, name)).fetchone()
                if old is None:
                    conn.execute(
                        "INSERT INTO parts(message_id, session, state, note, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (mid, name, state, note, t),
                    )
                    changed = changed or existing is not None
                elif (old["state"], old["note"]) != (state, note):
                    conn.execute(
                        "UPDATE parts SET state = ?, note = ?, updated_at = ? WHERE message_id = ? AND session = ?",
                        (state, note, t, mid, name),
                    )
                    changed = True
            if existing is not None:
                counts["updated" if changed else "unchanged"] += 1
            if changed:
                board.reindex_message(conn, mid)
    return counts, problems


def remove_imported(conn: sqlite3.Connection) -> int:
    """Removes what earlier imports brought in, for a fresh import at the cutover:
    the imported messages with their parts and search entries. Messages written on
    ShineBoard itself stay; a reply or a task that pointed at a removed message loses
    the link. Every session's last brief is reset, so its next brief starts afresh."""
    imported = "SELECT id FROM messages WHERE source LIKE 'import:%'"
    with db.write(conn):
        conn.execute(f"UPDATE messages SET reply_to = NULL WHERE source NOT LIKE 'import:%' AND reply_to IN ({imported})")
        conn.execute(f"UPDATE tasks SET message_id = NULL WHERE message_id IN ({imported})")
        conn.execute(
            "DELETE FROM search WHERE kind = 'message' AND ref IN "
            "(SELECT CAST(id AS TEXT) FROM messages WHERE source LIKE 'import:%')"
        )
        removed = conn.execute("DELETE FROM messages WHERE source LIKE 'import:%'").rowcount
        conn.execute("UPDATE sessions SET last_brief_at = NULL")
        board.audit(conn, "owner", "cli", "import_removed", "", f"{removed} imported messages")
    return removed


def export(conn: sqlite3.Connection, everything: bool = False, now: dt.datetime | None = None) -> str:
    """The board as the old Markdown table: the active messages, or every one."""
    now = now or db.utcnow()
    lines = [
        "# Session board (exported from ShineBoard)",
        "",
        f"Exported {db.iso(now)}; the board of record is ShineBoard. "
        + ("Every message." if everything else "The active messages: open, in force, or changed in the last seven days."),
        "",
        "| Date | From | To | Notice | Status |",
        "| --- | --- | --- | --- | --- |",
    ]

    def cell(s: str) -> str:
        return board.one_line(s).replace(" | ", " / ").replace("|", "/")

    for m in conn.execute("SELECT * FROM messages ORDER BY id DESC").fetchall():
        parts = board._parts(conn, m["id"])
        if not everything and not board.is_active(m, parts, now):
            continue
        status = "; ".join(f"{p['session']}: {p['state']}" + (f" ({p['note']})" if p["note"] else "") for p in parts)
        lines.append(
            f"| {m['day']} | {m['sender']} | {', '.join(p['session'] for p in parts)} | "
            f"#{m['id']} **{cell(m['title'])}** {cell(m['body'])} | {cell(status)} |"
        )
    return "\n".join(lines) + "\n"
