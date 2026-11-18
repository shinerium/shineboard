# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

import socket
import threading
import time

import pytest
import uvicorn

from shineboard import app, auth, db
from shineboard.config import Config

PASSWORD = "correct horse battery staple"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg(tmp_path, port: int = 8471) -> Config:
    cfg = Config(
        public_url=f"http://127.0.0.1:{port}",
        listen_port=port,
        database=str(tmp_path / "board.db"),
        backup_dir=str(tmp_path / "backups"),
    )
    conn = db.connect(cfg.database)
    db.init(conn, cfg.sessions)
    with db.write(conn):
        db.set_meta(conn, "cookie_secret", "k" * 64)
        db.set_meta(conn, "password_hash", auth.hash_password(PASSWORD))
    conn.close()
    return cfg


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.database)
    yield c
    c.close()


@pytest.fixture
def live(tmp_path):
    port = free_port()
    cfg = make_cfg(tmp_path, port)
    server = uvicorn.Server(uvicorn.Config(app.build(cfg), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the server did not start"
    yield cfg
    server.should_exit = True
    thread.join(timeout=10)
