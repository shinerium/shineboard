# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.

"""The server's settings, from a TOML file (by default /etc/shineboard/shineboard.toml).

The file holds no secrets: the owner's password hash, the cookie key and the
tokens live in the database, which only the service's user reads.
"""

from __future__ import annotations

import os
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse

DEFAULT_PATH = "/etc/shineboard/shineboard.toml"

# The sessions of the working agreement (claude/working-agreement.md), by their
# ID prefixes. `shineboard sessions add` adds one later.
DEFAULT_SESSIONS = [
    "FHS", "P2P", "SVM", "PQ", "BH", "GEO", "DAO", "BX", "TST", "GLOW", "APPS", "SEC", "COM",
]

# Where Claude's apps send the owner back after the login: claude.ai's and
# claude.com's connector callback. Claude Code and other local clients use a
# localhost address, which allow_localhost_redirects admits.
DEFAULT_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]


@dataclass
class Config:
    public_url: str = "http://127.0.0.1:8471"
    listen_host: str = "127.0.0.1"
    listen_port: int = 8471
    database: str = "/var/lib/shineboard/board.db"
    backup_dir: str = "/var/backups/shineboard"
    backup_keep_hourly: int = 48
    backup_keep_daily: int = 30
    access_token_minutes: int = 60
    refresh_token_days: int = 30
    allowed_redirect_uris: list[str] = field(default_factory=lambda: list(DEFAULT_REDIRECTS))
    allow_localhost_redirects: bool = True
    max_clients: int = 100
    forwarded_allow_ips: str = "127.0.0.1"
    # The zone of the messages' dates (the day a message is posted on); the
    # times themselves are UTC.
    day_zone: str = "UTC"
    sessions: list[str] = field(default_factory=lambda: list(DEFAULT_SESSIONS))

    @property
    def base(self) -> str:
        return self.public_url.rstrip("/")

    @property
    def resource(self) -> str:
        """The MCP endpoint, which is the OAuth resource the tokens are for."""
        return self.base + "/mcp"

    @property
    def host(self) -> str:
        return urlparse(self.public_url).netloc

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")


def load(path: str | None = None) -> Config:
    """The settings from path, $SHINEBOARD_CONFIG or the default path; the
    defaults when no file is there."""
    path = path or os.environ.get("SHINEBOARD_CONFIG") or DEFAULT_PATH
    if not os.path.exists(path):
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"{path}: unknown settings: {', '.join(unknown)}")
    cfg = Config(**data)
    parsed = urlparse(cfg.public_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError(f"{path}: public_url must be a scheme and a host, such as https://mcp.shinerium.org")
    try:
        ZoneInfo(cfg.day_zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{path}: day_zone is not a time zone the system knows: {cfg.day_zone}") from None
    return cfg
