#!/usr/bin/env bash
# Start the whole local stack in one terminal: gunicorn in the background,
# Caddy in the foreground. Ctrl+C stops both.
#
#   bash deploy/run-local.sh          -> http://localhost:8080
#
# On a real server you don't use this — systemd runs gunicorn and Caddy as
# services instead (see deploy/README.md section B).
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! systemctl is-active --quiet mysql 2>/dev/null; then
  echo "Starting MySQL..."
  sudo systemctl start mysql 2>/dev/null || sudo service mysql start
fi

./.venv/bin/gunicorn -c deploy/gunicorn.conf.py wsgi:application &
GUNICORN_PID=$!
trap 'kill "$GUNICORN_PID" 2>/dev/null || true' EXIT INT TERM

# Give gunicorn a moment so its startup log doesn't interleave with Caddy's.
sleep 2
if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
  echo "gunicorn failed to start — see the error above" >&2
  exit 1
fi

echo
echo "  app:   http://localhost:8080   (via Caddy)"
echo "  direct: http://localhost:8000  (gunicorn only)"
echo "  admin password: $(grep -h '^ADMIN_PASSWORD=' .env | cut -d= -f2-)"
echo "  Ctrl+C to stop both."
echo

APP_ROOT=$PWD caddy run --config deploy/Caddyfile
