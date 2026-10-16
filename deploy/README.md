# Deploying CapsuleHotOrNot

One Ubuntu box runs everything: gunicorn (app), MySQL 8, Caddy (TLS + static
files), and the uploaded media on local disk. The WSL2 rehearsal below uses the
**same files and same commands** as a real VPS — only the last section differs.

## Requirements

- Ubuntu 22.04/24.04, **2 GB RAM minimum** (4 GB comfortable). Reclustering runs
  t-SNE and a silhouette search over ~3,500 games; 1 GB instances OOM.
- ~2 GB disk to start: 370 MB of capsules today plus the database, and the
  media folder grows with every game ingested.

## A. Rehearse locally in WSL2

```powershell
# Windows, ONE TIME, in an Administrator PowerShell — then reboot
wsl --install -d Ubuntu-24.04
```

Optional but recommended, so systemd works exactly like on a server. In Ubuntu:

```bash
printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
```
then back in Windows: `wsl --shutdown`, and reopen Ubuntu.

```powershell
# Windows: dump the dev database and archive the uploads
powershell -File deploy\export-from-windows.ps1
```

```bash
# Ubuntu — copy the working tree straight off the Windows drive...
cp -r /mnt/c/Users/SUHAIL/Desktop/steam ~/capsulehotornot
cd ~/capsulehotornot && rm -rf .venv __pycache__ chon-export static/uploads

# ...or clone it, once this deploy kit has been pushed:
# sudo apt-get install -y git
# git clone https://github.com/ClassyGamesStudios/CapsuleHotOrNot.git ~/capsulehotornot
# cd ~/capsulehotornot

bash deploy/setup-ubuntu.sh                      # MySQL, venv, Caddy, .env, schema
nano .env                                        # paste STEAM_API_KEY
bash deploy/import-data.sh /mnt/c/Users/SUHAIL/Desktop/steam/chon-export
./.venv/bin/python selftest.py                   # 38 checks against a scratch DB

bash deploy/run-local.sh          # gunicorn + Caddy in one terminal
```

Or run them separately in two terminals:

```bash
./.venv/bin/gunicorn -c deploy/gunicorn.conf.py wsgi:application   # terminal 1
APP_ROOT=$PWD caddy run --config deploy/Caddyfile                  # terminal 2
```

### After a Windows reboot

Nothing to reinstall. Opening an Ubuntu terminal boots the distro, and MySQL is
an enabled systemd service so it comes back on its own — only the app processes
need restarting:

```bash
cd ~/capsulehotornot && bash deploy/run-local.sh
```

Browse **http://localhost:8080** from Windows — WSL forwards localhost
automatically. (Skipping Caddy works too: gunicorn alone is reachable on
**http://localhost:8000**, with Flask serving `/static` itself.) Check: landing page, `/player/join`, a Steam-less login via
`/player/test-login/76561198036019909`, the feed, `/admin`, and a developer
login. `CHON_ENV=development` locally keeps that test-login helper working.

To exercise the systemd unit as well (needs `systemd=true` above):

```bash
sudo useradd -r -s /usr/sbin/nologin chon || true
sudo mkdir -p /srv && sudo cp -r ~/capsulehotornot /srv/capsulehotornot
sudo chown -R chon:chon /srv/capsulehotornot
sudo cp deploy/capsulehotornot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now capsulehotornot
systemctl status capsulehotornot && journalctl -u capsulehotornot -f
```

## B. The same thing on a real VPS

Identical, with these differences:

1. **Deploy to `/srv/capsulehotornot`** owned by the `chon` user (the systemd
   unit assumes this path).
2. **`CHON_ENV=production`** in `.env`. The app then 404s the `/player/test-login`
   backdoor and refuses to boot with a default `SECRET_KEY`/`ADMIN_PASSWORD` or a
   missing `STEAM_API_KEY`.
3. **Caddyfile**: comment out the `:8080` block, uncomment the domain block, and
   point an A record at the server. Caddy gets a Let's Encrypt certificate by
   itself — nothing else to configure.
4. **Firewall** — 80/443 and SSH only. MySQL must never be reachable:
   ```bash
   sudo ufw default deny incoming && sudo ufw allow OpenSSH
   sudo ufw allow 80,443/tcp && sudo ufw enable
   ```
   Confirm `bind-address = 127.0.0.1` in `/etc/mysql/mysql.conf.d/mysqld.cnf`.
5. **Steam OpenID** only works over the public HTTPS domain — the realm and
   return URL are derived from the request, so log in via the domain, not the IP.
6. **Backups** (cron):
   ```bash
   0 4 * * * mysqldump --single-transaction game_profiles_db | gzip > /var/backups/chon-$(date +\%F).sql.gz
   0 5 * * 0 tar -czf /var/backups/uploads-$(date +\%F).tar.gz -C /srv/capsulehotornot/static uploads
   ```

### Scraping from the server

Steam rate-limits datacenter IPs harder than residential ones, and this project
has been blocked before. Either keep running `ingest_upcoming.py` /
`collect_capsules.py` from your own machine against the server's database, or run
them on the box gently (concurrency <= 5-8, off-peak) and expect the occasional
15-60 minute cooldown.

### After adding games

Clustering is cached in `vectors_cache.json` and new games are invisible to
genre matching until it is rebuilt. `/dev/vectors` shows a staleness warning;
hit `/dev/vectors?refresh=1` (minutes) or run:

```bash
./.venv/bin/python -c "import app; app.save_cluster_cache(app.compute_vector_scatter())"
```
