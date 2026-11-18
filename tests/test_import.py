# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

import datetime as dt

from shineboard import board, importer

BOARD = """# Session board

Intro.

| Date | From | To | Notice | Status |
| --- | --- | --- | --- | --- |
| 2026-11-16 | COM | all, PQ, GLOW | **Security reports to security@shinerium.org** (the user's message). (1) The address. For PQ: `SECURITY.md`. | all: no answer needed; PQ: done (16 November: changed); GLOW: open |
| 2026-11-14 | FHS | TST, COM | **Delivery 65** (item 172). Status calls answer. | TST: noted (14 November: TST repeats D1 (with `admin_importStatus`) later); COM: open |
| 2026-11-10 | COM (relayed) | DAO | The safety mechanism is the validator veto. COM found the question in no doc. | done (DAO-1, docs/dao-council.md 9) |
| 2026-11-05 | COM | all | Chain IDs: 1001 and 1002. | in force until the next genesis |
| 2026-11-04 | BX | COM | A note on `x | y` in code. | P2P: open; COM: waits for the user (on the reset) |
"""


def test_parts_of_and_classify():
    rows = importer.rows(BOARD)
    assert [r.sender for r in rows] == ["COM", "FHS", "COM (relayed)", "COM", "BX"]
    parts = importer.parts_of(rows[0])
    assert parts["all"].startswith("no answer needed") and parts["PQ"].startswith("done") and parts["GLOW"].strip() == "open"
    assert importer.classify("PQ", parts["PQ"]) == ("done", "16 November: changed")
    assert importer.classify("GLOW", parts["GLOW"]) == ("open", "")
    assert importer.classify("all", parts["all"]) == ("no-answer", "")
    tst = importer.parts_of(rows[1])["TST"]
    assert importer.classify("TST", tst)[0] == "noted", "an 'open' inside parentheses does not count"
    assert importer.classify("all", "in force until the next genesis") == ("in-force", "in force until the next genesis")
    assert importer.classify("COM", "waits for the user (on the reset)")[0] == "waiting"


def test_import_and_reimport(conn):
    counts, problems = importer.import_text(conn, BOARD, "import:board", today=dt.date(2026, 11, 18))
    assert counts["added"] == 5 and problems == []
    msgs = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    assert [m["day"] for m in msgs] == ["2026-11-04", "2026-11-05", "2026-11-10", "2026-11-14", "2026-11-16"]
    sec = msgs[-1]
    assert sec["title"] == "Security reports to security@shinerium.org" and sec["body"].startswith("(the user's message)")
    relayed = msgs[2]
    assert relayed["sender"] == "COM" and relayed["body"].startswith("(From: COM (relayed).)")
    assert relayed["title"] == "The safety mechanism is the validator veto."
    text = board.read(conn, [sec["id"]])
    assert "- PQ: done — 16 November: changed" in text and "- GLOW: open" in text
    force = board.read(conn, [msgs[1]["id"]])
    assert "- all: in-force" in force
    # The board changes: GLOW answers. The import updates the part, adds nothing.
    changed = BOARD.replace("GLOW: open |", "GLOW: done (17 November: on the site) |")
    counts, _ = importer.import_text(conn, changed, "import:board", today=dt.date(2026, 11, 18))
    assert counts["added"] == 0 and counts["updated"] == 1 and counts["unchanged"] == 4
    assert "- GLOW: done — 17 November: on the site (2026-11-17)" in board.read(conn, [sec["id"]])


def test_export(conn):
    importer.import_text(conn, BOARD, "import:board", today=dt.date(2026, 11, 18))
    out = importer.export(conn, now=dt.datetime(2026, 11, 18, tzinfo=dt.UTC))
    lines = [line for line in out.splitlines() if line.startswith("| 20")]
    assert len(lines) == 4, "the veto line closed more than seven days ago"
    assert all(line.count(" | ") == 4 for line in lines), "no stray separators"
    every = importer.export(conn, everything=True, now=dt.datetime(2026, 11, 18, tzinfo=dt.UTC))
    assert len([line for line in every.splitlines() if line.startswith("| 20")]) == 5


def test_cli_import_from_stdin(cfg, tmp_path, monkeypatch, capsys):
    import io
    import os

    import pytest

    from shineboard import cli, config

    monkeypatch.setattr(cli, "load", lambda path=None: cfg)
    monkeypatch.setattr("sys.stdin", io.StringIO(BOARD))
    assert cli.main(["import", "-", "--source", "board"]) == 0
    assert "added 5" in capsys.readouterr().out
    assert cli.main(["import", str(tmp_path / "missing.md")]) == 1
    assert "shineboard import - < FILE" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text('day_zone = "Mars/Olympus"\n')
    with pytest.raises(ValueError, match="day_zone"):
        config.load(str(bad))
    good = tmp_path / "good.toml"
    good.write_text('public_url = "https://mcp.example.org"\nday_zone = "Asia/Bangkok"\n')
    assert config.load(str(good)).day_zone == "Asia/Bangkok"
    assert os.path.exists(cfg.database)


def test_remove_imported(conn):
    importer.import_text(conn, BOARD, "import:board", today=dt.date(2026, 11, 18))
    first = conn.execute("SELECT min(id) FROM messages").fetchone()[0]
    native = int(board.post(conn, "TST", ["COM"], "Written on the board", "x", reply_to=first).split("#")[1].split(":")[0])
    board.set_task(conn, "COM", "Follow the imported line", ref="COM-1", message_id=first)
    board.brief(conn, "COM")
    assert importer.remove_imported(conn) == 5
    left = conn.execute("SELECT id, reply_to FROM messages").fetchall()
    assert [tuple(r) for r in left] == [(native, None)]
    assert conn.execute("SELECT message_id FROM tasks").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM parts WHERE message_id != ?", (native,)).fetchone()[0] == 0
    refs = {r[0] for r in conn.execute("SELECT ref FROM search WHERE kind = 'message'")}
    assert refs == {str(native)}
    assert conn.execute("SELECT count(*) FROM sessions WHERE last_brief_at IS NOT NULL").fetchone()[0] == 0
    counts, _ = importer.import_text(conn, BOARD, "import:board", today=dt.date(2026, 11, 18))
    assert counts["added"] == 5
