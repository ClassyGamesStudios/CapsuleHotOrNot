#!/usr/bin/env bash
# Provision Ubuntu for CapsuleHotOrNot. Works the same in WSL2 and on a VPS.
#
#   bash deploy/setup-ubuntu.sh
#
# Installs MySQL 8 + Python deps + Caddy, creates the database and a scoped
# user, writes .env if missing, and initialises the schema. Idempotent: safe to
# re-run. Does NOT import your data — see deploy/import-data.sh for that.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="${DB_NAME:-game_profiles_db}"
DB_USER="${DB_USER:-chon}"
cd "$APP_DIR"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

# WSL has no systemd unless you enable it; fall back to `service` there.
if [ -d /run/systemd/system ]; then HAS_SYSTEMD=1; else HAS_SYSTEMD=0; fi
start_mysql() {
  if [ "$HAS_SYSTEMD" = 1 ]; then sudo systemctl enable --now mysql
  else sudo service mysql start; fi
}

say "Installing packages (apt)"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip \
  mysql-server \
  debian-keyring debian-archive-keyring apt-transport-https curl ca-certificates

say "Starting MySQL"
start_mysql
sudo mysqladmin ping --silent || { echo "MySQL did not start"; exit 1; }

say "Creating database and user"
# MySQL 8 on Ubuntu authenticates root over the unix socket, so `sudo mysql`
# needs no password.
DB_PASSWORD="${DB_PASSWORD:-}"
if [ -z "$DB_PASSWORD" ]; then
  if [ -f .env ] && grep -q '^DB_PASSWORD=.\+' .env; then
    DB_PASSWORD="$(grep '^DB_PASSWORD=' .env | cut -d= -f2-)"
  else
    DB_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
  fi
fi
sudo mysql <<SQL
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';
-- selftest.py builds and drops its own throwaway database; a grant on a
-- not-yet-existing name is enough to CREATE it, and keeps the app user scoped.
GRANT ALL PRIVILEGES ON \`chon_selftest\`.* TO '${DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

say "Python virtualenv + dependencies"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
# A lock file (if present) wins, so the VPS gets exactly what was tested here.
if [ -f deploy/requirements-lock.txt ]; then
  echo "    installing from deploy/requirements-lock.txt"
  ./.venv/bin/pip install -q -r deploy/requirements-lock.txt
else
  ./.venv/bin/pip install -q -r requirements.txt
  ./.venv/bin/pip freeze > deploy/requirements-lock.txt
  echo "    wrote deploy/requirements-lock.txt"
fi
./.venv/bin/python -c "
import numpy, scipy, sklearn, flask
print('    numpy', numpy.__version__, '| scipy', scipy.__version__,
      '| sklearn', sklearn.__version__, '| flask', flask.__version__)
"

say "Writing .env (only if absent)"
if [ ! -f .env ]; then
  cp deploy/env.example .env
  python3 - "$DB_PASSWORD" "$DB_USER" "$DB_NAME" <<'PY'
import re, secrets, sys
pw, user, name = sys.argv[1:4]
env = open('.env').read()
env = re.sub(r'^SECRET_KEY=.*$', 'SECRET_KEY=' + secrets.token_urlsafe(48), env, flags=re.M)
env = re.sub(r'^ADMIN_PASSWORD=.*$', 'ADMIN_PASSWORD=' + secrets.token_urlsafe(12), env, flags=re.M)
env = re.sub(r'^DB_PASSWORD=.*$', 'DB_PASSWORD=' + pw, env, flags=re.M)
env = re.sub(r'^DB_USER=.*$', 'DB_USER=' + user, env, flags=re.M)
env = re.sub(r'^DB_NAME=.*$', 'DB_NAME=' + name, env, flags=re.M)
# Local rehearsal: keep the test-login helper usable. Flip to production on the VPS.
env = re.sub(r'^CHON_ENV=.*$', 'CHON_ENV=development', env, flags=re.M)
open('.env', 'w').write(env)
PY
  chmod 600 .env
  echo "    wrote .env — put your STEAM_API_KEY in it"
else
  echo "    .env already exists, leaving it alone"
fi

say "Creating schema and seeding"
set -a; . ./.env; set +a
./.venv/bin/python -c "
import db
db.init_db(); print('tags:', db.seed_tags(), '| categories:', db.seed_image_categories())
"

say "Installing Caddy"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy
  [ "$HAS_SYSTEMD" = 1 ] && sudo systemctl disable --now caddy || true  # we run it by hand locally
fi

cat <<EOF

Done. Next:
  1. Add STEAM_API_KEY to $APP_DIR/.env
  2. Import your data:   bash deploy/import-data.sh /path/to/chon-export
  3. Run the app:        ./.venv/bin/gunicorn -c deploy/gunicorn.conf.py wsgi:application
  4. Run Caddy:          sudo caddy run --config deploy/Caddyfile
     then browse http://localhost:8080 from Windows.

  systemd available here: $( [ "$HAS_SYSTEMD" = 1 ] && echo yes || echo "no — add '[boot]\\nsystemd=true' to /etc/wsl.conf and run 'wsl --shutdown'" )
EOF
