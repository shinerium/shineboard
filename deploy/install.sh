#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright 2026 The Shinerium Authors
# Part of ShineBoard, the Shinerium sessions' board.
#
# Installs or updates ShineBoard on Debian 13 (12 and Ubuntu 24.04 work too),
# from the repository it is run in:
#
#   sudo bash deploy/install.sh --domain mcp.shinerium.org [--email ADDRESS] [--no-caddy]
#
# It installs Python, SQLite and Caddy from the distribution; makes the
# service's user `shineboard`; puts the code in /opt/shineboard/src and its
# pinned, hash-checked dependencies in /opt/shineboard/venv; the settings in
# /etc/shineboard; the database in /var/lib/shineboard and the backups in
# /var/backups/shineboard; the systemd units (the server, and an hourly backup);
# and Caddy in front, which gets the TLS certificate itself (--email is the
# address Let's Encrypt writes to). Run again, it updates: the settings, the
# database and a Caddyfile with ShineBoard's site in it stay as they are. It
# never touches SSH, and opens ports 80 and 443 only in a UFW already active.

set -euo pipefail

usage() { awk 'NR > 4 && /^#/ { sub(/^# ?/, ""); print; next } NR > 4 { exit }' "$0"; }

DOMAIN=""
EMAIL=""
CADDY=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --no-caddy) CADDY=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ "$DOMAIN" =~ ^[a-z0-9.-]+\.[a-z]{2,}$ ]] || die "give the public name: --domain mcp.shinerium.org"
[[ -z "$EMAIL" || "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] || die "--email is not an address: $EMAIL"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$SRC/shineboard/app.py" && -f "$SRC/requirements.lock" ]] || die "run it from the ShineBoard repository ($SRC has no shineboard/app.py)"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == debian || "${ID:-}" == ubuntu ]] || echo "WARNING: written for Debian; this is ${PRETTY_NAME:-unknown}" >&2

step "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PKGS=(python3 python3-venv sqlite3 curl util-linux)
[[ $CADDY -eq 1 ]] && PKGS+=(caddy)
apt-get install -y -qq "${PKGS[@]}" >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "ShineBoard needs Python 3.11 or newer"

step "User and directories"
if ! id shineboard >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/shineboard --no-create-home --shell /usr/sbin/nologin shineboard
fi
install -d -o shineboard -g shineboard -m 700 /var/lib/shineboard /var/backups/shineboard
install -d -o root -g root -m 755 /opt/shineboard /etc/shineboard

step "Code in /opt/shineboard/src"
rm -rf /opt/shineboard/src.new
mkdir -p /opt/shineboard/src.new
tar -C "$SRC" --exclude=.git --exclude=__pycache__ --exclude='*.egg-info' --exclude=.venv --exclude=.pytest_cache -cf - . \
  | tar -C /opt/shineboard/src.new -xf -
python3 -m compileall -q /opt/shineboard/src.new/shineboard >/dev/null
chown -R root:root /opt/shineboard/src.new
chmod -R go-w,a+rX /opt/shineboard/src.new
rm -rf /opt/shineboard/src.old
if [[ -d /opt/shineboard/src ]]; then mv /opt/shineboard/src /opt/shineboard/src.old; fi
mv /opt/shineboard/src.new /opt/shineboard/src

step "Python environment (pinned, hash-checked)"
[[ -x /opt/shineboard/venv/bin/python ]] || python3 -m venv /opt/shineboard/venv
/opt/shineboard/venv/bin/pip install --quiet --disable-pip-version-check --require-hashes --no-deps \
  -r /opt/shineboard/src/requirements.lock

step "The command /usr/local/bin/shineboard (it runs as the service's user)"
cat > /usr/local/bin/shineboard <<'EOF'
#!/bin/bash
# ShineBoard's administration, run as the service's user (written by deploy/install.sh).
umask 077
ENV=(SHINEBOARD_CONFIG=/etc/shineboard/shineboard.toml PYTHONPATH=/opt/shineboard/src PYTHONDONTWRITEBYTECODE=1)
PY=(/opt/shineboard/venv/bin/python -m shineboard.cli)
if [[ $EUID -eq 0 ]]; then
  exec setpriv --reuid=shineboard --regid=shineboard --init-groups --reset-env -- /usr/bin/env "${ENV[@]}" "${PY[@]}" "$@"
elif [[ "$(id -un)" == shineboard ]]; then
  exec /usr/bin/env "${ENV[@]}" "${PY[@]}" "$@"
fi
echo "shineboard: run it with sudo" >&2
exit 1
EOF
chmod 755 /usr/local/bin/shineboard

step "Settings: /etc/shineboard/shineboard.toml"
if [[ ! -f /etc/shineboard/shineboard.toml ]]; then
  sed "s|^public_url = .*|public_url = \"https://$DOMAIN\"|" /opt/shineboard/src/deploy/shineboard.toml.example \
    > /etc/shineboard/shineboard.toml
  chmod 644 /etc/shineboard/shineboard.toml
else
  grep -q "^public_url = \"https://$DOMAIN\"" /etc/shineboard/shineboard.toml \
    || echo "WARNING: /etc/shineboard/shineboard.toml has another public_url than https://$DOMAIN" >&2
  echo "    kept as it is"
fi

step "Database"
shineboard init

step "systemd units"
install -m 644 /opt/shineboard/src/deploy/shineboard.service /opt/shineboard/src/deploy/shineboard-backup.service \
  /opt/shineboard/src/deploy/shineboard-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet shineboard.service shineboard-backup.timer
systemctl restart shineboard.service
systemctl restart shineboard-backup.timer

if [[ $CADDY -eq 1 ]]; then
  step "Caddy: the TLS certificate for $DOMAIN, and the proxy"
  CF=/etc/caddy/Caddyfile
  if [[ -f "$CF" ]] && grep -q "ShineBoard" "$CF"; then
    echo "    $CF has ShineBoard's site: kept as it is"
  else
    # Only the distribution's own example (a :80 site with file_server) is replaced.
    if [[ -f "$CF" ]] && grep -vE '^[[:space:]]*(#|$)' "$CF" \
        | grep -vqE '^[[:space:]]*(:80 \{|root \* /usr/share/caddy|file_server|\})[[:space:]]*$'; then
      die "$CF serves sites of its own: add the site in deploy/Caddyfile to it by hand, then run this again with --no-caddy"
    fi
    [[ -f "$CF" ]] && cp -a "$CF" "$CF.before-shineboard"
    {
      # HTTP/1.1 and HTTP/2 only: the firewall opens TCP 443, not the UDP that HTTP/3 needs.
      printf '{\n'
      if [[ -n "$EMAIL" ]]; then printf '\temail %s\n' "$EMAIL"; fi
      printf '\tservers {\n\t\tprotocols h1 h2\n\t}\n}\n\n'
      sed "s|^mcp\.example\.org {|$DOMAIN {|" /opt/shineboard/src/deploy/Caddyfile
    } > "$CF"
  fi
  install -d -o caddy -g caddy -m 750 /var/log/caddy
  if ! out="$(caddy validate --config "$CF" --adapter caddyfile 2>&1)"; then
    echo "$out" >&2
    die "Caddy does not accept $CF"
  fi
  systemctl enable --quiet caddy
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  step "UFW: ports 80 and 443"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

step "Health"
for _ in $(seq 1 40); do
  curl -fsS http://127.0.0.1:8471/healthz >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS http://127.0.0.1:8471/healthz >/dev/null || die "the server does not answer: journalctl -u shineboard -n 50"
echo "    the server answers on 127.0.0.1:8471"
shineboard check || true

cat <<EOF

ShineBoard is installed. Next:
  1. The owner's password, if it is not set:  sudo shineboard set-password
  2. From your own machine: https://$DOMAIN/healthz answers "ok" (Caddy gets the certificate on the first request).
  3. Claude: Customize → Connectors → + Add → Add custom connector → https://$DOMAIN/mcp (README.md)
  4. The Markdown board, once:  sudo shineboard import - --source archive < session-board-archive.md
                                sudo shineboard import - --source board < session-board.md
EOF
