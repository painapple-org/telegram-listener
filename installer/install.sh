#!/usr/bin/env bash
# Standalone installer for this repo's Telegram listener: a systemd --user
# unit for the listener process itself, plus bringing up the
# telegram-bot-api Compose service it talks to.
#
# The listener runs as a real host process under systemd --user (not
# Docker) because a dispatch plugin may need genuine host identity (a
# writable uv/pip cache, full filesystem visibility, real git/SSH
# credentials) that a container's necessarily-partial view can't give it as
# cleanly - this mirrors why painapple/spoor's own telegram/listener.py
# runs the same way. telegram-bot-api has no such need (it's a stateless
# local HTTP proxy in front of api.telegram.org), so it stays a Compose
# service (see docker-compose.yml next to this script).
#
# Usage: sudo ./install.sh <run-as-user> <repo-dir>
# Requires: that user to already exist, with `uv` on its PATH. Creates
# <repo-dir>/.env from .env.example if it isn't there yet; fill it in before
# starting the service (see SETUP.md).

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (it manages a systemd --user unit + linger for another user)" >&2
  exit 1
fi

RUN_AS_USER="${1:?usage: install.sh <run-as-user> <repo-dir>}"
REPO_DIR="${2:?usage: install.sh <run-as-user> <repo-dir>}"
REPO_DIR="$(cd "$REPO_DIR" && pwd)"

if ! id "$RUN_AS_USER" &>/dev/null; then
  echo "user $RUN_AS_USER does not exist - create it first" >&2
  exit 1
fi

RUN_AS_HOME="$(getent passwd "$RUN_AS_USER" | cut -d: -f6)"
RUN_AS_UID="$(id -u "$RUN_AS_USER")"

echo "=== 1. .env ==="
# The token and API hash live here, so it must not be world-readable: the
# listener runs as $RUN_AS_USER and nothing else needs to read it.
if [[ -f "${REPO_DIR}/.env" ]]; then
  echo "ok: ${REPO_DIR}/.env already exists, leaving it alone"
else
  cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
  echo "ok: wrote ${REPO_DIR}/.env from .env.example - fill in the blanks (see SETUP.md) before starting the service"
fi
chown "$RUN_AS_USER" "${REPO_DIR}/.env"
chmod 600 "${REPO_DIR}/.env"
echo "ok: ${REPO_DIR}/.env is mode 600, owned by ${RUN_AS_USER}"

echo
echo "=== 2. telegram-bot-api (Docker Compose) ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "${SCRIPT_DIR}/data/telegram-bot-api"
# --env-file is explicit because the compose file lives here but .env lives
# at the repo root: without it, compose looks for an installer/.env that
# doesn't exist and the container comes up with no API_ID/API_HASH set.
docker compose -f "${SCRIPT_DIR}/docker-compose.yml" --env-file "${REPO_DIR}/.env" up -d telegram-bot-api
echo "ok: telegram-bot-api is up"

echo
echo "=== 3. python dependencies (uv sync) ==="
sudo -u "$RUN_AS_USER" bash -lc "cd '${REPO_DIR}' && uv sync --extra claude-agent-plugin"
echo "ok: dependencies installed into ${REPO_DIR}/.venv"

echo
echo "=== 4. linger + systemd --user unit for the listener ==="
# Deliberately a *user*-level unit, not a system-wide one under
# /etc/systemd/system - the point of running this as a host process is
# giving $RUN_AS_USER its own genuine identity, and a system-level unit
# would need root just to manage a service that's conceptually entirely
# that user's own. `loginctl enable-linger` is the one narrow, one-time
# root action needed here - everything else (write the unit, daemon-reload,
# enable, start) that user does on its own via `systemctl --user`.
loginctl enable-linger "$RUN_AS_USER"
echo "ok: linger enabled for ${RUN_AS_USER} (its systemd --user instance now survives logout/reboot)"

USER_SYSTEMD_DIR="${RUN_AS_HOME}/.config/systemd/user"
SYSTEMD_UNIT_PATH="${USER_SYSTEMD_DIR}/telegram-listener.service"
sudo -u "$RUN_AS_USER" mkdir -p "$USER_SYSTEMD_DIR"
sudo -u "$RUN_AS_USER" tee "$SYSTEMD_UNIT_PATH" > /dev/null <<EOF
[Unit]
Description=Telegram listener (long-poll bridge to a pluggable dispatch handler)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${REPO_DIR}/.env
Environment=PATH=${RUN_AS_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${REPO_DIR}/.venv/bin/python3 -m telegram_listener.listener
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
echo "ok: wrote ${SYSTEMD_UNIT_PATH} (owned by ${RUN_AS_USER}, no root needed to manage it)"

sudo -u "$RUN_AS_USER" env XDG_RUNTIME_DIR="/run/user/${RUN_AS_UID}" systemctl --user daemon-reload
sudo -u "$RUN_AS_USER" env XDG_RUNTIME_DIR="/run/user/${RUN_AS_UID}" systemctl --user enable telegram-listener.service
echo "ok: telegram-listener.service installed and enabled - NOT started yet"

echo
echo "Next steps:"
echo "  1. Make sure ${REPO_DIR}/.env has TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS,"
echo "     DISPATCH_PLUGIN, TELEGRAM_API_ID, TELEGRAM_API_HASH set (see .env.example)."
echo "  2. sudo -u ${RUN_AS_USER} env XDG_RUNTIME_DIR=/run/user/${RUN_AS_UID} systemctl --user start telegram-listener.service"
