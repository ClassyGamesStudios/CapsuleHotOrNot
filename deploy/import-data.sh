#!/usr/bin/env bash
# Restore the database dump + uploaded media produced by export-from-windows.ps1.
#
#   bash deploy/import-data.sh /mnt/c/Users/SUHAIL/Desktop/steam/chon-export
#
# Safe to re-run: the dump recreates its own database, and uploads are unpacked
# over the top of whatever is already there.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-$APP_DIR/chon-export}"
cd "$APP_DIR"

[ -f .env ] || { echo "No .env — run deploy/setup-ubuntu.sh first"; exit 1; }
set -a; . ./.env; set +a

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

SQL="$SRC/game_profiles_db.sql"
if [ -f "$SQL" ]; then
  say "Importing $(du -h "$SQL" | cut -f1) of SQL (a few minutes)"
  # The dump contains CREATE DATABASE, so import as root over the socket, then
  # re-grant to the app user in case the dump predates it.
  sudo mysql < "$SQL"
  sudo mysql -e "GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost'; FLUSH PRIVILEGES;"
else
  echo "No $SQL — skipping database import"
fi

TAR="$SRC/uploads.tar.gz"
HAVE=$(ls static/uploads 2>/dev/null | wc -l)
if [ ! -f "$TAR" ]; then
  echo "No $TAR — skipping media import ($HAVE files already in static/uploads)"
elif [ "$HAVE" -gt 0 ] && [ -z "${FORCE_MEDIA:-}" ]; then
  # e.g. the whole project folder was copied off the Windows drive already.
  echo "static/uploads already has $HAVE files — skipping unpack (FORCE_MEDIA=1 to override)"
else
  say "Unpacking $(du -h "$TAR" | cut -f1) of media into static/"
  mkdir -p static
  tar -xzf "$TAR" -C static
  echo "    $(ls static/uploads | wc -l) files in static/uploads"
fi

say "Applying migrations and checking the data"
./.venv/bin/python -c "
import db
db.init_db()
s = db.get_platform_stats()
print('games: %(games)s | players: %(players)s | matches: %(matches)s | developers: %(developers)s' % s)
print('clusterable games:', db.count_games())
"
echo
echo "Now run:  ./.venv/bin/gunicorn -c deploy/gunicorn.conf.py wsgi:application"
