#!/usr/bin/env bash
# Put the local app on a public HTTPS URL via a Cloudflare quick tunnel, so
# someone remote can look at it. No Cloudflare account, domain or VPS needed.
#
#   bash deploy/share-demo.sh           # start (prints the public URL)
#   bash deploy/share-demo.sh stop      # stop everything
#
# The URL is random and changes every restart, and it only works while this
# machine is awake — it's for demos, not production. For a permanent host see
# section B of deploy/README.md.
#
# SAFETY: a public URL means the /player/test-login/<steam_id> backdoor would
# let anyone log in as any player, so this script forces CHON_ENV=production
# (which 404s it) and refuses to start if the secrets are still defaults.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$PWD"
LOG_DIR="$APP_DIR/.demo-logs"
BIN="$HOME/.local/bin/cloudflared"
mkdir -p "$LOG_DIR" "$(dirname "$BIN")"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

stop_all() {
  for name in cloudflared caddy gunicorn; do
    pkill -f "$name.*capsulehotornot" 2>/dev/null || true
  done
  pkill -f 'cloudflared tunnel' 2>/dev/null || true
  pkill -f 'caddy run --config deploy/Caddyfile' 2>/dev/null || true
  pkill -f 'gunicorn -c deploy/gunicorn.conf.py' 2>/dev/null || true
  echo "stopped gunicorn, caddy and cloudflared (if they were running)"
}

if [ "${1:-}" = "stop" ]; then
  stop_all
  exit 0
fi

say "Installing cloudflared (user-local, no sudo)"
if [ ! -x "$BIN" ]; then
  curl -fsSL -o "$BIN" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x "$BIN"
fi
"$BIN" --version

say "Switching to production config"
# Real secrets already live in .env / secrets_local.py; this only flips the mode.
if grep -q '^CHON_ENV=' .env; then
  sed -i 's/^CHON_ENV=.*/CHON_ENV=production/' .env
else
  echo 'CHON_ENV=production' >> .env
fi
grep '^CHON_ENV=' .env
./.venv/bin/python - <<'PY'
import app
app.check_production_config()        # raises if secrets are still defaults
print('    production config OK: test-login disabled, secrets present')
PY

say "Starting the app"
stop_all
if ! systemctl is-active --quiet mysql 2>/dev/null; then
  sudo systemctl start mysql 2>/dev/null || sudo service mysql start
fi

nohup ./.venv/bin/gunicorn -c deploy/gunicorn.conf.py wsgi:application \
  > "$LOG_DIR/gunicorn.log" 2>&1 &
sleep 3
# Run from the project root: the Caddyfile's local static root is relative.
nohup caddy run --config deploy/Caddyfile > "$LOG_DIR/caddy.log" 2>&1 &
sleep 2

curl -fsS -o /dev/null http://127.0.0.1:8080/ \
  && echo "    local stack responding on :8080" \
  || { echo "local stack failed — see $LOG_DIR/*.log"; exit 1; }
# Pipe-free on purpose: `ls | head -1` makes ls die of SIGPIPE, and pipefail
# turns that into a script-killing failure.
SAMPLE=''
for f in static/uploads/*; do
  [ -f "$f" ] && SAMPLE=$(basename "$f")
  break
done
if [ -n "$SAMPLE" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8080/static/uploads/$SAMPLE")
  [ "$code" = "200" ] && echo "    static files serving ($SAMPLE)" \
    || { echo "static files return $code — capsule art would be missing"; exit 1; }
fi

say "Opening the Cloudflare tunnel"
nohup "$BIN" tunnel --no-autoupdate --url http://localhost:8080 \
  > "$LOG_DIR/cloudflared.log" 2>&1 &

URL=''
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 2
done

if [ -z "$URL" ]; then
  echo "Tunnel did not report a URL — see $LOG_DIR/cloudflared.log" >&2
  exit 1
fi

cat <<EOF

  PUBLIC URL:  $URL

  Share that link. It stays up while this machine is awake and these processes
  run; the address changes if you restart the tunnel.

  logs:  $LOG_DIR/{gunicorn,caddy,cloudflared}.log
  stop:  bash deploy/share-demo.sh stop

  Back to local development afterwards:
      sed -i 's/^CHON_ENV=.*/CHON_ENV=development/' .env
  (that re-enables /player/test-login — never leave it on while public)
EOF
