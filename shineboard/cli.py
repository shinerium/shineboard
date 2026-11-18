# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""shineboard: the server and its administration, run as the service's user."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import re
import secrets
import sys

from . import __version__, auth, board, db, importer
from .config import load


def _conn(cfg):
    return db.connect(cfg.database)


def _apply_settings(cfg, conn) -> None:
    """The settings the board's functions read from the database."""
    with db.write(conn):
        if db.get_meta(conn, "day_zone") != cfg.day_zone:
            db.set_meta(conn, "day_zone", cfg.day_zone)


def cmd_init(cfg, args) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(cfg.database)), exist_ok=True)
    conn = _conn(cfg)
    db.init(conn, cfg.sessions)
    with db.write(conn):
        if not db.get_meta(conn, "cookie_secret"):
            db.set_meta(conn, "cookie_secret", secrets.token_hex(32))
    _apply_settings(cfg, conn)
    try:
        os.chmod(cfg.database, 0o600)
    except OSError:
        pass
    has_password = bool(db.get_meta(conn, "password_hash"))
    print(f"ShineBoard {__version__}: database {cfg.database} ready (schema {db.SCHEMA_VERSION}).")
    if not has_password:
        print("Next: shineboard set-password")
    return 0


def cmd_set_password(cfg, args) -> int:
    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != password:
            print("The two passwords differ.", file=sys.stderr)
            return 1
    try:
        hashed = auth.hash_password(password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    conn = _conn(cfg)
    with db.write(conn):
        db.set_meta(conn, "password_hash", hashed)
        if args.revoke:
            conn.execute("DELETE FROM oauth_tokens WHERE kind IN ('access', 'refresh')")
            # A new cookie key logs the owner's view out everywhere.
            db.set_meta(conn, "cookie_secret", secrets.token_hex(32))
    print("Password set." + (" Every OAuth token and web login was revoked: the apps log in again." if args.revoke else ""))
    return 0


def cmd_token(cfg, args) -> int:
    conn = _conn(cfg)
    if args.action == "create":
        name = args.name or ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", name):
            print("Name the token: 2 to 41 characters of a-z, 0-9 and '-'", file=sys.stderr)
            return 1
        token = auth.new_token("sbt_")
        expires = int(dt.datetime.now(dt.UTC).timestamp()) + args.days * 86400 if args.days else None
        with db.write(conn):
            if conn.execute("SELECT 1 FROM oauth_tokens WHERE kind = 'static' AND name = ?", (name,)).fetchone():
                print(f"A token named {name} exists: revoke it first.", file=sys.stderr)
                return 1
            conn.execute(
                "INSERT INTO oauth_tokens(token_hash, kind, client_id, scopes, family, name, expires_at, created_at) "
                "VALUES (?, 'static', ?, ?, ?, ?, ?, ?)",
                (auth.digest(token), f"static:{name}", auth.SCOPE, f"static:{name}", name, expires, db.iso(db.utcnow())),
            )
        print(token)
        print(f"(token {name}: shown once; the server keeps only its hash)", file=sys.stderr)
        return 0
    if args.action == "list":
        for r in conn.execute("SELECT name, created_at, expires_at, last_used FROM oauth_tokens WHERE kind = 'static' ORDER BY name"):
            exp = dt.datetime.fromtimestamp(r["expires_at"], dt.UTC).date().isoformat() if r["expires_at"] else "never"
            print(f"{r['name']}\tcreated {r['created_at']}\texpires {exp}\tlast used {r['last_used'] or 'never'}")
        n = conn.execute("SELECT count(*) FROM oauth_tokens WHERE kind = 'access'").fetchone()[0]
        print(f"({n} OAuth access tokens live)")
        return 0
    if args.action == "revoke":
        with db.write(conn):
            gone = conn.execute("DELETE FROM oauth_tokens WHERE kind = 'static' AND name = ?", (args.name,)).rowcount
        print(f"Revoked {args.name}." if gone else f"No token named {args.name}.")
        return 0 if gone else 1
    return 2


def cmd_clients(cfg, args) -> int:
    conn = _conn(cfg)
    if args.action == "list":
        import json

        for r in conn.execute("SELECT * FROM oauth_clients ORDER BY created_at"):
            info = json.loads(r["info"])
            n = conn.execute("SELECT count(*) FROM oauth_tokens WHERE client_id = ?", (r["client_id"],)).fetchone()[0]
            print(f"{r['client_id']}\t{info.get('client_name') or '-'}\t{', '.join(info.get('redirect_uris') or [])}\t"
                  f"created {r['created_at']}\tlast used {r['last_used'] or 'never'}\t{n} tokens")
        return 0
    if args.action == "revoke":
        with db.write(conn):
            conn.execute("DELETE FROM oauth_tokens WHERE client_id = ?", (args.client_id,))
            gone = conn.execute("DELETE FROM oauth_clients WHERE client_id = ?", (args.client_id,)).rowcount
        print("Revoked the client and its tokens." if gone else "No such client.")
        return 0 if gone else 1
    return 2


def cmd_sessions(cfg, args) -> int:
    conn = _conn(cfg)
    if args.action == "add":
        name = (args.name or "").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", name):
            print("A session's prefix is 2 to 10 letters and digits, such as APPS", file=sys.stderr)
            return 1
        with db.write(conn):
            conn.execute("INSERT OR IGNORE INTO sessions(name, title) VALUES (?, ?)", (name, args.title or ""))
        print(f"Session {name} is on the board.")
        return 0
    print(board.overview(conn))
    return 0


def cmd_import(cfg, args) -> int:
    conn = _conn(cfg)
    if args.file == "-":
        # From standard input: `sudo shineboard import - < board.md` reads a file
        # the service's user could not open.
        text = sys.stdin.read()
    else:
        try:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"cannot read {args.file}: {e.strerror}. The command runs as the service's user; "
                  f"give the file on standard input: shineboard import - < FILE", file=sys.stderr)
            return 1
    if args.replace:
        print(f"removed {importer.remove_imported(conn)} imported messages")
    counts, problems = importer.import_text(conn, text, f"import:{args.source}")
    for p in problems:
        print(f"note: {p}", file=sys.stderr)
    print(", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


def cmd_export(cfg, args) -> int:
    conn = _conn(cfg)
    text = importer.export(conn, everything=args.all)
    if args.out:
        tmp = args.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, args.out)
    else:
        sys.stdout.write(text)
    return 0


BACKUP_RE = re.compile(r"^board-(\d{8})-(\d{4})\.db$")


def cmd_backup(cfg, args) -> int:
    os.makedirs(cfg.backup_dir, mode=0o700, exist_ok=True)
    stamp = db.utcnow().strftime("%Y%m%d-%H%M")
    final = os.path.join(cfg.backup_dir, f"board-{stamp}.db")
    tmp = os.path.join(cfg.backup_dir, f".board-{stamp}.db.tmp")
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = _conn(cfg)
    conn.execute("VACUUM INTO ?", (tmp,))
    conn.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)
    # Keep the newest backup_keep_hourly, then the newest of each day for backup_keep_daily days.
    files = sorted((f for f in os.listdir(cfg.backup_dir) if BACKUP_RE.match(f)), reverse=True)
    keep = set(files[: cfg.backup_keep_hourly])
    days: set[str] = set()
    cutoff = (db.utcnow().date() - dt.timedelta(days=cfg.backup_keep_daily)).strftime("%Y%m%d")
    for f in files:
        day = BACKUP_RE.match(f).group(1)
        if day not in days and day >= cutoff:
            days.add(day)
            keep.add(f)
    removed = 0
    for f in files:
        if f not in keep:
            os.remove(os.path.join(cfg.backup_dir, f))
            removed += 1
    print(f"Backup {final}; {len(keep)} kept, {removed} removed.")
    return 0


def cmd_check(cfg, args) -> int:
    conn = _conn(cfg)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    version = db.get_meta(conn, "schema_version")
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("messages", "parts", "tasks", "decisions", "oauth_clients")}
    print(f"integrity {ok}; schema {version}; " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print("password set" if db.get_meta(conn, "password_hash") else "NO PASSWORD: run shineboard set-password")
    return 0 if ok == "ok" else 1


def cmd_serve(cfg, args) -> int:
    from .app import serve

    conn = _conn(cfg)
    db.init(conn, cfg.sessions)
    _apply_settings(cfg, conn)
    conn.close()
    serve(cfg)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shineboard", description="ShineBoard: the Shinerium sessions' board.")
    p.add_argument("--config", help="the settings file (default $SHINEBOARD_CONFIG or /etc/shineboard/shineboard.toml)")
    p.add_argument("--version", action="version", version=f"ShineBoard {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the server")
    sub.add_parser("init", help="create or upgrade the database")
    sp = sub.add_parser("set-password", help="set the owner's password")
    sp.add_argument("--stdin", action="store_true", help="read it from the first line of standard input")
    sp.add_argument("--revoke", action="store_true", help="also revoke every OAuth token")
    sp = sub.add_parser("token", help="static bearer tokens for scripts and Claude Code")
    sp.add_argument("action", choices=["create", "list", "revoke"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--days", type=int, default=0, help="expire after this many days (default never)")
    sp = sub.add_parser("clients", help="the registered OAuth clients")
    sp.add_argument("action", choices=["list", "revoke"])
    sp.add_argument("client_id", nargs="?")
    sp = sub.add_parser("sessions", help="the sessions on the board")
    sp.add_argument("action", choices=["list", "add"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--title", default="")
    sp = sub.add_parser("import", help="import a Markdown board or its archive")
    sp.add_argument("file", help="the Markdown file, or - for standard input")
    sp.add_argument("--replace", action="store_true",
                    help="first remove what earlier imports brought in (the fresh import at the cutover)")
    sp.add_argument("--source", choices=["board", "archive"], default="board")
    sp = sub.add_parser("export", help="the board as the old Markdown table")
    sp.add_argument("--all", action="store_true", help="every message, not only the active ones")
    sp.add_argument("--out", help="write to this file instead of standard output")
    sub.add_parser("backup", help="a consistent copy of the database, with retention")
    sub.add_parser("check", help="the database's integrity and counts")
    args = p.parse_args(argv)
    cfg = load(args.config)
    handlers = {
        "serve": cmd_serve, "init": cmd_init, "set-password": cmd_set_password, "token": cmd_token,
        "clients": cmd_clients, "sessions": cmd_sessions, "import": cmd_import, "export": cmd_export,
        "backup": cmd_backup, "check": cmd_check,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
