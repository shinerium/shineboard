# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

import datetime as dt

import pytest

from shineboard import board, db

T0 = dt.datetime(2026, 11, 18, 6, 0, tzinfo=dt.UTC)


def at(hours: float) -> dt.datetime:
    return T0 + dt.timedelta(hours=hours)


def mid(text: str) -> int:
    return int(text.split("#")[1].split(":")[0].split()[0])


def test_post_and_parts(conn):
    out = board.post(conn, "pq", ["COM", "apps", "all"], "ShineKey's worker is built", "The API, version 1.", now=at(0))
    m = mid(out)
    assert out.startswith(f"Posted #{m}: PQ → COM, APPS, all")
    text = board.read(conn, [m])
    assert "- all: no-answer" in text and "- COM: open" in text and "- APPS: open" in text
    with pytest.raises(board.BoardError, match="unknown session"):
        board.post(conn, "XYZ", ["COM"], "a title", "b")
    with pytest.raises(board.BoardError, match="does not address itself"):
        board.post(conn, "COM", ["COM"], "a title", "b")
    with pytest.raises(board.BoardError, match="at least one recipient"):
        board.post(conn, "COM", [], "a title", "b")
    with pytest.raises(board.BoardError, match="all_until"):
        board.post(conn, "COM", ["FHS"], "a title", "b", all_until="the reset")


def test_set_part_rules(conn):
    m = mid(board.post(conn, "FHS", ["COM", "TST"], "Delivery 65", "FHS-172.", now=at(0)))
    assert board.set_part(conn, m, "com", "done", "FHS-172 in the planned 0.2.4 row", now=at(1)).startswith(f"#{m} COM: done")
    with pytest.raises(board.BoardError, match="unknown state"):
        board.set_part(conn, m, "TST", "finished")
    with pytest.raises(board.BoardError, match="keeps its own part"):
        board.set_part(conn, m, "TST", "done", part="COM")
    with pytest.raises(board.BoardError, match="only the sender"):
        board.set_part(conn, m, "TST", "withdrawn")
    with pytest.raises(board.BoardError, match="the part of all"):
        board.set_part(conn, m, "TST", "no-answer", part="all")
    # A session that was not addressed may still answer: its part is added.
    board.set_part(conn, m, "BX", "noted", "for the explorer", now=at(2))
    assert "- BX: noted — for the explorer" in board.read(conn, [m])
    # The sender withdraws: every open part closes.
    board.set_part(conn, m, "FHS", "withdrawn", "overtaken by delivery 66", now=at(3))
    text = board.read(conn, [m])
    assert "- TST: withdrawn — overtaken by delivery 66" in text and "- COM: done" in text


def test_notice_in_force(conn):
    m = mid(board.post(conn, "COM", ["all"], "Chain IDs", "1001 and 1002.", all_until="the next genesis", now=at(0)))
    assert "- all: in-force — until the next genesis" in board.read(conn, [m])
    brief = board.brief(conn, "FHS", now=at(24 * 30))
    assert f"#{m}" in brief and "in force until the next genesis" in brief
    board.set_part(conn, m, "COM", "no-answer", "the genesis came", part="all", now=at(24 * 31))
    assert f"#{m}" not in board.brief(conn, "FHS", now=at(24 * 32))


def test_brief_sections_and_last_brief(conn):
    a = mid(board.post(conn, "PQ", ["COM"], "ShineKey's repository", "Rows for the working agreement.", now=at(0)))
    n = mid(board.post(conn, "GLOW", ["all"], "The new site is live", "shinerium.org.", now=at(1)))
    mine = mid(board.post(conn, "COM", ["FHS", "TST"], "The watch", "[Shn.Watch].", now=at(2)))
    board.set_part(conn, mine, "TST", "noted", "tests from 0.2.4", now=at(3))
    board.set_task(conn, "COM", "Write SECURITY.md", ref="COM-1", now=at(3))
    board.record_decision(conn, "COM", "security-contact", "Security reports to security@shinerium.org", "RFC 2142's mailbox.", "2026-11-16", ["SEC", "GLOW"], now=at(3))
    first = board.brief(conn, "COM", now=at(4))
    assert "first brief" in first
    assert f"#{a} 2026-11-18 PQ → COM — ShineKey's repository" in first
    assert f"#{n} 2026-11-18 GLOW — The new site is live" in first
    assert f"#{mine} The watch · TST: noted — tests from 0.2.4" in first
    assert f"#{mine} The watch · open: FHS" in first
    assert "COM-1 [open] Write SECURITY.md" in first
    assert "security-contact (2026-11-16, user)" in first
    # The next brief has only what came since: the inbox stays until answered.
    second = board.brief(conn, "COM", now=at(5))
    assert "last brief 2026-11-18T10:00:00Z" in second
    assert f"#{a}" in second and f"#{n}" not in second and "TST: noted" not in second and "security-contact" not in second
    board.set_part(conn, a, "COM", "done", "in the working agreement", now=at(6))
    assert "Waiting for COM: nothing." in board.brief(conn, "COM", now=at(7))
    assert f"#{a} ShineKey's repository · COM: done — in the working agreement" in board.brief(conn, "PQ", now=at(8))


def test_find(conn):
    m1 = mid(board.post(conn, "PQ", ["COM"], "ShineKey gets a repository of its own", "Apache-2.0 for its own code.", now=at(0)))
    m2 = mid(board.post(conn, "FHS", ["COM"], "Delivery 65", "Status calls answer during imports.", now=at(1)))
    board.set_part(conn, m2, "COM", "done", "FHS-172 in the planned 0.2.4 row", now=at(2))
    hits = board.find(conn, "repository")
    assert f"#{m1}" in hits and f"#{m2}" not in hits
    assert f"#{m2}" in board.find(conn, "0.2.4"), "the parts' notes are searched"
    assert f"#{m1}" in board.find(conn, "apache-2.0 (own)"), "punctuation does not break the query"
    listing = board.find(conn, sender="FHS")
    assert f"#{m2}" in listing and f"#{m1}" not in listing
    assert f"#{m1}" in board.find(conn, to="COM", state="open") and f"#{m2}" not in board.find(conn, to="COM", state="open")
    assert board.find(conn, "nothingmatches") == "No messages found."
    with pytest.raises(board.BoardError):
        board.find(conn, since="18 November")


def test_active_rule(conn):
    m = mid(board.post(conn, "BX", ["COM"], "Explorer note", "x", now=at(0)))
    board.set_part(conn, m, "COM", "noted", now=at(1))
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (m,)).fetchone()
    parts = board._parts(conn, m)
    assert board.is_active(msg, parts, at(24 * 6))
    assert not board.is_active(msg, parts, at(24 * 8))
    assert f"#{m}" not in board.find(conn, active_only=True, now=at(24 * 8))
    assert f"#{m}" in board.find(conn, now=at(24 * 8))


def test_tasks(conn):
    out = board.set_task(conn, "COM", "ShineBoard cutover", ref="COM-7", detail="after the user's yes")
    tid = int(out.split()[1])
    assert "COM-7 [open]" in board.tasks(conn, "COM")
    board.set_task(conn, "COM", status="done", ref="COM-7")
    assert board.tasks(conn, "COM") == "No tasks."
    assert "[done]" in board.tasks(conn, "COM", include_closed=True)
    with pytest.raises(board.BoardError, match="is COM's"):
        board.set_task(conn, "FHS", status="open", task_id=tid)
    with pytest.raises(board.BoardError, match="needs a title"):
        board.set_task(conn, "FHS")


def test_decisions(conn):
    board.record_decision(conn, "COM", "shinekey-licence", "ShineKey under Apache-2.0", "ShineKey's own code; the core stays LGPL.", "2026-11-15", ["PQ", "APPS"], "license/LicensingApproach.md section 1")
    board.record_decision(conn, "COM", "shinekey-licence-2", "ShineKey under Apache-2.0, in its repository", "As before, in `shinekey`.", "2026-11-16", ["PQ"], supersedes="shinekey-licence")
    current = board.decisions(conn)
    assert "shinekey-licence-2" in current and "· 2026-11-15" not in current
    assert "[superseded by shinekey-licence-2]" in board.decisions(conn, include_old=True)
    assert "shinekey-licence-2" in board.decisions(conn, "apache")
    assert "shinekey-licence-2" in board.decisions(conn, area="PQ")
    with pytest.raises(board.BoardError, match="the key"):
        board.record_decision(conn, "COM", "Bad Key", "t", "d", "2026-11-16")
    with pytest.raises(board.BoardError, match="decided_on"):
        board.record_decision(conn, "COM", "ok-key", "title", "d", "16 November")


def test_audit_and_overview(conn):
    m = mid(board.post(conn, "COM", ["FHS"], "A note", "x", client="client-1"))
    board.set_part(conn, m, "FHS", "done", client="client-2")
    rows = conn.execute("SELECT actor, client, action FROM audit ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("COM", "client-1", "post"), ("FHS", "client-2", "set_part")]
    assert "- COM: 0 · 0 · never" in board.overview(conn)


def test_day_zone(conn):
    late = dt.datetime(2026, 11, 17, 23, 0, tzinfo=dt.UTC)
    a = mid(board.post(conn, "COM", ["FHS"], "Posted late in UTC", "x", now=late))
    with db.write(conn):
        db.set_meta(conn, "day_zone", "Asia/Bangkok")
    b = mid(board.post(conn, "COM", ["FHS"], "Posted on the board's calendar", "x", now=late))
    days = dict(conn.execute("SELECT id, day FROM messages WHERE id IN (?, ?)", (a, b)).fetchall())
    assert days == {a: "2026-11-17", b: "2026-11-18"}
    times = {r[0] for r in conn.execute("SELECT posted_at FROM messages WHERE id IN (?, ?)", (a, b))}
    assert times == {"2026-11-17T23:00:00Z"}, "the times stay UTC"


def test_rename_session(conn):
    # As on the server before the rename: BCN on the board, GEO not yet.
    with db.write(conn):
        conn.execute("DELETE FROM sessions WHERE name = 'GEO'")
        conn.execute("INSERT INTO sessions(name) VALUES ('BCN')")
    a = mid(board.post(conn, "BCN", ["COM"], "From the beacons", "x", now=at(0)))
    b = mid(board.post(conn, "COM", ["BCN", "FHS"], "To the beacons", "y", now=at(1)))
    board.set_part(conn, b, "FHS", "done", now=at(2))
    board.set_task(conn, "BCN", "Measure", ref="BCN-3", now=at(2))
    board.record_decision(conn, "COM", "geo-test", "A decision", "d", "2026-11-20", ["PQ", "BCN", "FHS"], now=at(2))
    board.brief(conn, "BCN", now=at(3))
    out = board.rename_session(conn, "bcn", "geo", "Geolocation & latency", now=at(4))
    assert out.startswith("BCN is now GEO: 1 messages sent, 1 parts, 1 tasks, 1 decisions moved")
    assert conn.execute("SELECT sender FROM messages WHERE id = ?", (a,)).fetchone()[0] == "GEO"
    assert "- GEO: open" in board.read(conn, [b])
    assert "GEO-" not in board.tasks(conn, "GEO") and "BCN-3 [open] Measure" in board.tasks(conn, "GEO")
    assert conn.execute("SELECT areas FROM decisions WHERE key = 'geo-test'").fetchone()[0] == "PQ,GEO,FHS"
    row = conn.execute("SELECT title, last_brief_at FROM sessions WHERE name = 'GEO'").fetchone()
    assert row["title"] == "Geolocation & latency" and row["last_brief_at"] == "2026-11-18T09:00:00Z"
    assert conn.execute("SELECT count(*) FROM sessions WHERE name = 'BCN'").fetchone()[0] == 0
    # The old name still reaches the session.
    assert board.session_name(conn, "bcn") == "GEO"
    c = mid(board.post(conn, "BCN", ["COM"], "Still as BCN", "z", now=at(5)))
    assert conn.execute("SELECT sender FROM messages WHERE id = ?", (c,)).fetchone()[0] == "GEO"
    assert f"#{b}" in board.brief(conn, "BCN", now=at(6))
    assert "is a former name" in str(pytest.raises(board.BoardError, board.rename_session, conn, "COM", "BCN").value)
    with pytest.raises(board.BoardError, match="no session"):
        board.rename_session(conn, "BCN", "LOC")
    with pytest.raises(board.BoardError, match="2 to 10 letters"):
        board.rename_session(conn, "GEO", "G")
    # The settings may still list the old name: init does not bring it back.
    db.init(conn, ["BCN", "GEO", "COM"])
    assert conn.execute("SELECT count(*) FROM sessions WHERE name = 'BCN'").fetchone()[0] == 0
    assert "unknown session" not in board.overview(conn)
    assert any(r["action"] == "session_renamed" for r in conn.execute("SELECT action FROM audit"))


def test_rename_into_a_listed_name(conn):
    # GEO came from the settings first; BCN's history merges into it.
    with db.write(conn):
        conn.execute("INSERT INTO sessions(name) VALUES ('BCN')")
    m = mid(board.post(conn, "COM", ["BCN"], "To the beacons", "y", now=at(0)))
    board.brief(conn, "BCN", now=at(1))
    board.rename_session(conn, "BCN", "GEO", now=at(2))
    assert "- GEO: open" in board.read(conn, [m])
    assert conn.execute("SELECT last_brief_at FROM sessions WHERE name = 'GEO'").fetchone()[0] == "2026-11-18T07:00:00Z"
