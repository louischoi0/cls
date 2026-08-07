#!/usr/bin/env bash
# Start the automation server. Localhost-bound by default (README §6): expose it
# only through a reverse proxy, SSH tunnel, or Tailscale.
set -euo pipefail

cd "$(dirname "$0")"

HOST="${CC_AUTOMATION_HOST:-127.0.0.1}"
PORT="${CC_AUTOMATION_PORT:-8787}"

exec .venv/bin/uvicorn server.main:app --host "$HOST" --port "$PORT" "$@"
