#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.
#
# Updates ShineBoard on its server from this repository: a backup first, then
# install.sh with the name that /etc/shineboard/shineboard.toml already has.
#
#   cd shineboard && git pull --ff-only && sudo bash deploy/update.sh
#
# The previous code stays in /opt/shineboard/src.old; README.md tells how to go back.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/shineboard/shineboard.toml
[[ -f $CONF ]] || { echo "no $CONF: install first with: sudo bash deploy/install.sh --domain NAME" >&2; exit 1; }
DOMAIN="$(sed -n 's|^public_url = "https://\([^"/]*\)/\{0,1\}"[[:space:]]*$|\1|p' "$CONF")"
[[ -n $DOMAIN ]] || { echo "public_url in $CONF is not https://NAME" >&2; exit 1; }

echo "==> Backup before the update"
shineboard backup
exec bash "$HERE/install.sh" --domain "$DOMAIN" "$@"
