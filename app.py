"""
GameDev Portal — Flask app (MySQL-backed).

Scope so far:
  * Assume the user is already logged in (no login/signup yet); a developer
    row is created for CURRENT_USER on startup.
  * Profile page lists the developer's game profiles (or "none yet"). Each
    card is clickable and opens a dedicated game page showing its tags.
  * "Add new game profile" -> enter a Steam App ID -> we look up the game's
    tags (with numeric tag ids) and its main capsule image, store them in
    MySQL, and link the app id to the developer.

Player side (separate portal at /player/*):
  * Sign in with Steam via Steam OpenID. On the callback we verify with Steam,
    extract the SteamID64, and — only for a steam_id we haven't seen before —
    call GetPlayerSummaries to fetch username/profileurl/avatar and insert a
    players_profile row. Returning players are looked up, not re-inserted.
  * "Sync" pulls the whole owned-games library (GetOwnedGames), and for every
    game with playtime > 0 sums that playtime into each of the game's tags,
    storing the {tag_id: total_minutes} map in players_profile.tags_totals.

Persistence (MySQL, db=game_profiles_db):
  developer_profile : who owns which game app ids (JSON list)
  game_profile      : one row per app id (name, ordered tag_id JSON list, capsule_url)
  tags              : reference table (tag_id -> tag_name), seeded from tags_data.py
  players_profile   : one row per Steam player (steam_id, username, profileurl, avatar)

Data source note:
  SteamDB forbids scraping, so we pull the same data straight from Steam's store
  page: tag ids from the embedded InitAppTagModal(...) payload, and the main
  capsule from the capsule_616x353.jpg asset. Tag ids match SteamDB's /tag/<id>/.
"""

from flask import Flask, render_template, request, redirect, url_for, flash, abort, session
import requests
import re
import json
import io
import os
import random
from urllib.parse import urlencode
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import secrets as secrets_lib
from PIL import Image
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.manifold import TSNE

import db

app = Flask(__name__)
app.secret_key = 'dev_secret_key_change_in_production'

# Uploaded media lives under static/uploads so Flask can serve it directly.
UPLOAD_FOLDER = os.path.join(app.static_folder, 'uploads')
ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ENFORCE_IMAGE_DIMENSIONS = False   # size validation disabled for now; flip on later
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB per upload
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Folder-driven artwork: drop files in and they show up (no DB row, no code
# change) — studio logos on the landing wall, gift cards in the join carousel.
DEV_LOGO_FOLDER = os.path.join(app.static_folder, 'dev_logos')
GIFT_CARD_FOLDER = os.path.join(app.static_folder, 'gift_cards')
WALL_IMAGE_EXT = ALLOWED_IMAGE_EXT | {'svg'}
os.makedirs(DEV_LOGO_FOLDER, exist_ok=True)
os.makedirs(GIFT_CARD_FOLDER, exist_ok=True)

# --- Fake "logged in" developer for now --------------------------------------
CURRENT_USER = 'dev_user'

# --- Steam Web API / OpenID (player side) ------------------------------------
# Key is loaded from secrets_local.py (gitignored) or the STEAM_API_KEY env var,
# so the real key never lives in version control.
try:
    from secrets_local import STEAM_API_KEY
except ImportError:
    STEAM_API_KEY = os.environ.get('STEAM_API_KEY', '')
STEAM_OPENID_URL = 'https://steamcommunity.com/openid/login'

# Multiplier tab boost: recent (2-week) playtime is weighted up before being
# added on top of all-time. value = alltime + twoweeks * (RECENCY_MULTIPLIER - 1)
RECENCY_MULTIPLIER = 1.5


# --- Steam lookup -------------------------------------------------------------
STORE_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'),
    'Cookie': 'birthtime=0; mature_content=1',
    'Accept-Language': 'en',
}


def fetch_game_data(appid):
    """Look up a Steam app -> {appid, name, capsule_image, tag_ids:[...]} or None.

    Only the numeric tag ids are scraped; tag names come from the tags table.
    """
    appid = str(appid).strip()
    if not appid.isdigit():
        return None

    url = f'https://store.steampowered.com/app/{appid}/?cc=us&l=english'
    try:
        resp = requests.get(url, headers=STORE_HEADERS, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None

    html = resp.text

    tag_ids = []
    m = re.search(r'InitAppTagModal\(\s*\d+\s*,\s*(\[.*?\])\s*,', html, re.S)
    if m:
        try:
            for t in json.loads(m.group(1)):
                if t.get('tagid') is not None:
                    tag_ids.append(t['tagid'])
        except json.JSONDecodeError:
            pass

    capsule_image = None
    cap = re.search(
        r'https://[^"\']*?/apps/' + re.escape(appid) + r'/[0-9a-f]+/capsule_616x353\.jpg[^"\']*',
        html,
    )
    if cap:
        capsule_image = cap.group(0)

    name = None
    nm = re.search(r'<div[^>]*class="apphub_AppName"[^>]*>([^<]+)</div>', html)
    if nm:
        name = nm.group(1).strip()

    if not tag_ids and not capsule_image and not name:
        return None

    return {
        'appid': int(appid),
        'name': name or f'App {appid}',
        'capsule_image': capsule_image,
        'tag_ids': tag_ids,
    }


def current_developer():
    """The logged-in developer (from session), or None."""
    did = session.get('developer_id')
    return db.get_developer_by_id(did) if did else None


def tag_weights(n):
    """Lion's-share weight distribution (percent) across n tags, in tag order.

    100% total. Small cases are fixed; for 4+ tags the top three take
    40/30/20 and the remaining 10% is split evenly among the rest.
      1 -> [100]
      2 -> [60, 40]
      3 -> [50, 30, 20]
      n -> [40, 30, 20, then 10/(n-3) each]
    """
    if n <= 0:
        return []
    if n == 1:
        return [100.0]
    if n == 2:
        return [60.0, 40.0]
    if n == 3:
        return [50.0, 30.0, 20.0]
    rest = 10.0 / (n - 3)
    return [40.0, 30.0, 20.0] + [rest] * (n - 3)


# --- Game tag vector ---------------------------------------------------------
def build_game_vector(steam_tag_ids, pk_map, dim):
    """A length-`dim` binary presence vector for a game.

    Index (tag_id - 1) is 1.0 if the game has that tag, else 0.0 — so a 20-tag
    game has exactly 20 ones. Order-independent (scraped tag order doesn't
    matter) and every tag counts equally, which suits genre clustering.
    `pk_map` maps steam_tag_id -> tag_id (the tags-table PK that fixes vector
    order). Steam tag ids not in our tags table are skipped.
    """
    vec = [0.0] * dim
    for steam_id in steam_tag_ids:
        pk = pk_map.get(steam_id)
        if pk and 1 <= pk <= dim:
            vec[pk - 1] = 1.0
    return vec


def store_game(appid, name, tag_ids, capsule_url):
    """Insert/update a game and (re)compute its tag vector."""
    db.insert_game(appid, name, tag_ids, capsule_url)
    pk_map = db.get_tag_pk_map()
    dim = db.get_tag_dimension()
    db.set_game_vector(appid, build_game_vector(tag_ids, pk_map, dim))


def rebuild_game_vectors():
    """Recompute vectors for every game (backfills games added before vectors)."""
    pk_map = db.get_tag_pk_map()
    dim = db.get_tag_dimension()
    count = 0
    for game in db.get_all_games():
        db.set_game_vector(game['game_appid'], build_game_vector(game['tag_id'], pk_map, dim))
        count += 1
    return count


# --- Player side (Steam login) -----------------------------------------------
def fetch_player_summary(steam_id):
    """Call GetPlayerSummaries and return {username, profileurl, avatar} or None."""
    url = (f'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/'
           f'?key={STEAM_API_KEY}&steamids={steam_id}')
    try:
        resp = requests.get(url, timeout=15)
        players = resp.json().get('response', {}).get('players', [])
    except (requests.RequestException, ValueError):
        return None
    if not players:
        return None
    p = players[0]
    return {
        'username': p.get('personaname'),
        'profileurl': p.get('profileurl'),
        'avatar': p.get('avatar'),
    }


def current_player():
    steam_id = session.get('steam_id')
    return db.get_player_by_steam_id(steam_id) if steam_id else None


def _load_cluster_cache():
    """Load the cached clustering (centroids + names). Compute if missing/stale."""
    if os.path.exists(VECTORS_CACHE_FILE):
        with open(VECTORS_CACHE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if data.get('centroids') and data.get('idf'):
            return data
    data = compute_vector_scatter()
    with open(VECTORS_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return data


def player_tag_vector(player):
    """Build the player's 448-dim tag vector from tags_totals_multiplier
    (index = tag_id PK - 1, value = weighted minutes for that tag)."""
    totals = player.get('tags_totals_multiplier') or {}
    pk_map = db.get_tag_pk_map()
    dim = db.get_tag_dimension()
    vec = np.zeros(dim)
    for steam_id, minutes in totals.items():
        pk = pk_map.get(int(steam_id))
        if pk and 1 <= pk <= dim:
            vec[pk - 1] = float(minutes)
    return vec


def player_category_ranking(player):
    """Rank the genre clusters by cosine similarity to the player's tag vector.
    Returns a list of {cluster, name, score} sorted best-first."""
    if not player or not player.get('tags_totals_multiplier'):
        return []
    pv = player_tag_vector(player)
    if np.linalg.norm(pv) == 0:
        return []

    data = _load_cluster_cache()
    centroids = data.get('centroids', {})
    names = data.get('cluster_names', {})

    # Apply the same tf-idf weighting used for the clusters, then normalize.
    idf = np.array(data['idf'], dtype=float)
    pv = pv * idf
    norm = np.linalg.norm(pv)
    if norm == 0:
        return []
    pv = pv / norm
    ranking = []
    for cid, cen in centroids.items():
        c = np.array(cen, dtype=float)
        cn = np.linalg.norm(c)
        sim = float(pv @ (c / cn)) if cn > 0 else 0.0
        ranking.append({'cluster': int(cid),
                        'name': names.get(str(cid)) or names.get(cid) or f'Cluster {cid}',
                        'score': round(sim, 4)})
    ranking.sort(key=lambda r: r['score'], reverse=True)
    return ranking


def _main_category_id():
    cats = {c['name']: c['imageCategoryID'] for c in db.get_image_categories()}
    return cats.get('main')


def _label(media, name_by_appid=None):
    m = dict(media)
    nm = m.get('game_name') or (name_by_appid or {}).get(m['appID'])
    m['game_name'] = nm or f"App {m['appID']}"
    return m


def next_matchup_for_player(player, category_id=None):
    """Pick an unseen matchup — ALWAYS between two UPCOMING games (the platform
    only compares brand-new games).

    PHASE 1: a developer-uploaded upcoming image vs any other upcoming image
             (developer submissions get priority for exposure/dashboard data).
    PHASE 2: upcoming games from the player's best-fit genre clusters.
    PHASE 3: any two upcoming games (if clusters are exhausted).
    """
    if category_id is None:
        category_id = _main_category_id()
    seen = set(player.get('matchupIDs') or [])

    eligible = db.get_eligible_media_in_category(category_id)   # upcoming games only
    if len(eligible) < 2:
        return None

    def unseen(m1, m2):
        existing = db.find_matchup(m1['mediaID'], m2['mediaID'])
        return existing is None or existing not in seen

    # PHASE 1 — developer-uploaded (upcoming) images take precedence.
    dev_eligible = [m for m in eligible if m['developerID'] is not None]
    random.shuffle(dev_eligible)
    for m1 in dev_eligible[:40]:
        for _ in range(15):
            m2 = random.choice(eligible)
            if m2['mediaID'] == m1['mediaID'] or m2['appID'] == m1['appID']:
                continue
            if unseen(m1, m2):
                return {'category': {'name': 'Featured developer submissions', 'score': None},
                        'media1': _label(m1), 'media2': _label(m2)}

    # PHASE 2 — player's best-fit genre clusters (upcoming games only).
    elig_by_appid = {}
    for m in eligible:
        elig_by_appid.setdefault(m['appID'], m)
    ranking = player_category_ranking(player)
    if ranking:
        data = _load_cluster_cache()
        points = data.get('points', [])
        name_by_appid = {p['appid']: p['name'] for p in points}
        for cat in ranking:
            pool = [elig_by_appid[p['appid']] for p in points
                    if p['cluster'] == cat['cluster'] and p['appid'] in elig_by_appid]
            if len(pool) < 2:
                continue
            for _ in range(50):
                a, b = random.sample(pool, 2)
                if unseen(a, b):
                    return {'category': cat, 'media1': _label(a, name_by_appid),
                            'media2': _label(b, name_by_appid)}

    # PHASE 3 — any two upcoming games.
    for _ in range(50):
        a, b = random.sample(eligible, 2)
        if a['appID'] == b['appID']:
            continue
        if unseen(a, b):
            return {'category': {'name': 'Upcoming games', 'score': None},
                    'media1': _label(a), 'media2': _label(b)}
    return None


def fetch_owned_games(steam_id):
    """Return the player's owned games: [{appid, name, playtime_forever}] (minutes).

    Uses GetOwnedGames, which gives the full library with lifetime playtime
    (games never played come back with playtime_forever 0).
    """
    url = (f'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/'
           f'?key={STEAM_API_KEY}&steamid={steam_id}'
           f'&include_appinfo=1&include_played_free_games=1')
    try:
        resp = requests.get(url, timeout=20)
        games = resp.json().get('response', {}).get('games', [])
    except (requests.RequestException, ValueError):
        return []
    return [
        {'appid': g['appid'], 'name': g.get('name'),
         'playtime_forever': g.get('playtime_forever', 0)}
        for g in games
    ]


def fetch_recently_played(steam_id):
    """Return recently played games: [{appid, name, playtime_2weeks}] (minutes).

    Uses GetRecentlyPlayedGames (last 2 weeks); each game carries a
    playtime_2weeks value used for the 2-week tab.
    """
    url = (f'https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/'
           f'?key={STEAM_API_KEY}&steamid={steam_id}&count=1000')
    try:
        resp = requests.get(url, timeout=15)
        games = resp.json().get('response', {}).get('games', [])
    except (requests.RequestException, ValueError):
        return []
    return [
        {'appid': g['appid'], 'name': g.get('name'),
         'playtime_2weeks': g.get('playtime_2weeks', 0)}
        for g in games
    ]


def get_game_tag_ids(appid):
    """Tag ids for an app id, scraped the same way as the developer side.

    Cached in game_profile so repeated syncs don't re-scrape Steam.
    """
    game = db.get_game(appid)
    if game:
        return game['tag_id']
    data = fetch_game_data(appid)
    if not data:
        return []
    store_game(appid, data['name'], data['tag_ids'], data['capsule_image'])
    return data['tag_ids']


def _sum_tags(games, playtime_key):
    """Sum a playtime field per tag across games. Returns (totals, counted).

    For each game with playtime > 0, add that playtime to every one of the
    game's tags. Games with 0 playtime are skipped (add nothing, cost a scrape).
    """
    totals = {}
    counted = 0
    for game in games:
        playtime = game.get(playtime_key, 0) or 0
        if playtime <= 0:
            continue
        counted += 1
        for tag_id in get_game_tag_ids(game['appid']):
            totals[tag_id] = totals.get(tag_id, 0) + playtime
    return totals, counted


def compute_multiplier_totals(alltime, twoweeks, multiplier=RECENCY_MULTIPLIER):
    """Boost recent playtime and add it on top of all-time, per tag.

    value = alltime + twoweeks * (multiplier - 1)
    Tags with no recent playtime keep just their all-time value.
    All maths in raw minutes; keys are tag ids (ints here).
    """
    result = {}
    for tag_id in set(alltime) | set(twoweeks):
        a = alltime.get(tag_id, 0)
        w = twoweeks.get(tag_id, 0)
        result[tag_id] = a + w * (multiplier - 1)
    return result


def compute_player_tags(steam_id):
    """Compute all three tag maps for a player. Returns (alltime, twoweeks, multiplier)."""
    alltime, _ = _sum_tags(fetch_owned_games(steam_id), 'playtime_forever')
    twoweeks, _ = _sum_tags(fetch_recently_played(steam_id), 'playtime_2weeks')
    multiplier = compute_multiplier_totals(alltime, twoweeks)
    return alltime, twoweeks, multiplier


def build_tag_rows(totals, with_percent=False):
    """Sorted (highest first) tag rows for display; hours from minutes.

    `totals` is a {tag_id: minutes} map (keys may be str from JSON or int).
    If with_percent, each row also gets `percent`: its share of the column
    total (computed on raw minutes).
    """
    if not totals:
        return []
    items = sorted(((int(tid), mins) for tid, mins in totals.items()),
                   key=lambda x: x[1], reverse=True)
    names = {t['steam_tag_id']: t['tag_name'] for t in db.get_tags_by_ids([tid for tid, _ in items])}
    column_total = sum(mins for _, mins in items)
    rows = []
    for tid, mins in items:
        row = {'steam_tag_id': tid,
               'tag_name': names.get(tid) or f'#{tid}',
               'minutes': round(mins),
               'hours': round(mins / 60, 1)}
        if with_percent:
            row['percent'] = round(mins / column_total * 100, 1) if column_total else 0
        rows.append(row)
    return rows


# --- Routes -------------------------------------------------------------------
# Landing-page vanity boost: displayed = real * mult + base (real still grows it).
STAT_BOOST = {
    'games':      {'mult': 4,    'base': 2000},
    'players':    {'mult': 1200, 'base': 8000},
    'matches':    {'mult': 300,  'base': 40000},
    'developers': {'mult': 40,   'base': 120},
}


def _folder_images(folder, web_prefix):
    """Web paths for every image in a static subfolder, in filename order."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [web_prefix + n for n in names
            if '.' in n and n.rsplit('.', 1)[1].lower() in WALL_IMAGE_EXT]


def developer_logos():
    """The developer wall is folder-driven: drop a logo in static/dev_logos and
    it appears; delete it and it's gone."""
    return _folder_images(DEV_LOGO_FOLDER, '/static/dev_logos/')


def gift_card_art():
    """Gift cards for the player join carousel, same folder-driven deal. The
    caption comes from the filename: 01_steam-wallet-10.svg -> Steam Wallet $10."""
    cards = []
    for src in _folder_images(GIFT_CARD_FOLDER, '/static/gift_cards/'):
        stem = src.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        head, _, rest = stem.partition('_')
        if head.isdigit() and rest:
            stem = rest                      # strip the ordering prefix
        words = stem.replace('-', ' ').replace('_', ' ').title().split()
        if words and words[-1].isdigit():
            words[-1] = '$' + words[-1]      # trailing number is an amount
        cards.append({'src': src, 'label': ' '.join(words)})
    return cards


@app.route('/')
def index():
    real = db.get_platform_stats()
    stats = {k: real[k] * STAT_BOOST[k]['mult'] + STAT_BOOST[k]['base'] for k in real}
    recent = db.get_recent_matches(12)
    wall = db.get_capsule_wall(_main_category_id(), 40)
    return render_template('home.html', stats=stats, recent=recent,
                           wall=wall, dev_logos=developer_logos())


@app.route('/profile')
def profile():
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    games = db.get_games(dev['game_appid'])
    return render_template('profile.html', developer=dev, games=games)


@app.route('/game/<int:appid>')
def game_detail(appid):
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    game = db.get_game(appid)
    if not game or appid not in dev['game_appid']:
        abort(404)
    tags = db.get_tags_by_ids(game['tag_id'])
    weights = tag_weights(len(tags))
    for tag, weight in zip(tags, weights):
        tag['weight'] = weight
    # Temporary: only offer the 'main' category in the upload dropdown
    # (the image_categories table is unchanged).
    categories = [c for c in db.get_image_categories() if c['name'] == 'main']
    media = db.get_media_for_game(appid, dev['developer_id'])
    return render_template('game_detail.html', game=game, tags=tags,
                           categories=categories, media=media)


def _dimensions_ok(width, height, category):
    """True if (width, height) satisfies the category's dimension rule."""
    if category['match_mode'] == 'either':
        return width == category['width'] or height == category['height']
    return width == category['width'] and height == category['height']


def _dimension_requirement_text(category):
    if category['match_mode'] == 'either':
        return f"width {category['width']}px OR height {category['height']}px"
    return f"{category['width']}x{category['height']}"


@app.route('/game/<int:appid>/upload', methods=['POST'])
def upload_media(appid):
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    if appid not in dev['game_appid']:
        abort(404)

    category_id = request.form.get('category_id', '')
    file = request.files.get('image')

    if not category_id.isdigit():
        flash('Please choose an image category.')
        return redirect(url_for('game_detail', appid=appid))
    category = db.get_image_category(int(category_id))
    if not category:
        flash('Unknown image category.')
        return redirect(url_for('game_detail', appid=appid))
    if not file or not file.filename:
        flash('Please choose an image file to upload.')
        return redirect(url_for('game_detail', appid=appid))

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_IMAGE_EXT:
        flash(f'Unsupported file type. Allowed: {", ".join(sorted(ALLOWED_IMAGE_EXT))}.')
        return redirect(url_for('game_detail', appid=appid))

    data = file.read()
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception:
        flash("Couldn't read that file as an image.")
        return redirect(url_for('game_detail', appid=appid))

    if ENFORCE_IMAGE_DIMENSIONS and not _dimensions_ok(width, height, category):
        flash(f"Image is {width}x{height}, but '{category['name']}' requires "
              f"{_dimension_requirement_text(category)}.")
        return redirect(url_for('game_detail', appid=appid))

    # Insert first to get a mediaID, then name the file after it and save.
    media_id = db.insert_media(appid, category['imageCategoryID'], '', dev['developer_id'])
    filename = f'media_{media_id}.{ext}'
    with open(os.path.join(UPLOAD_FOLDER, filename), 'wb') as f:
        f.write(data)
    rel_path = f'static/uploads/{filename}'
    db.update_media_path(media_id, rel_path)
    db.add_mediaid_to_developer(dev['developer_id'], media_id)

    flash(f"Uploaded {category['name']} image ({width}x{height}).")
    return redirect(url_for('game_detail', appid=appid))


def _delete_media_and_file(dev_id, media_id):
    """Delete one owned media row + its file + all references. Returns True if done."""
    row = db.get_media(media_id)
    if not row or row['developerID'] != dev_id:
        return False
    db.delete_media(media_id)
    db.remove_mediaid_from_developer(dev_id, media_id)
    if row.get('path'):
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)), row['path']))
        except OSError:
            pass
    return True


@app.route('/media/<int:media_id>/delete', methods=['POST'])
def delete_media_route(media_id):
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    ok = _delete_media_and_file(dev['developer_id'], media_id)
    flash('Image deleted.' if ok else "Can't delete that image.")
    return redirect(request.referrer or url_for('profile'))


@app.route('/game/<int:appid>/delete', methods=['POST'])
def delete_game_route(appid):
    """Remove a game from THIS developer's profile (+ delete their images for it).
    The shared game_profile row is kept (used for clustering)."""
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    if appid in dev['game_appid']:
        for m in db.get_media_for_game(appid, dev['developer_id']):
            _delete_media_and_file(dev['developer_id'], m['mediaID'])
        db.remove_appid_from_developer(dev['developer_id'], appid)
        flash('Game removed from your profile.')
    return redirect(url_for('profile'))


@app.route('/profile/add', methods=['GET', 'POST'])
def add_profile():
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    if request.method == 'POST':
        raw = request.form.get('appid', '').strip()
        if not raw.isdigit():
            flash('Please enter a valid numeric Steam App ID.')
            return redirect(url_for('add_profile'))
        appid = int(raw)

        if appid in dev['game_appid']:
            flash('That game profile has already been added.')
            return redirect(url_for('profile'))

        # Only scrape/insert into game_profile if we've never seen this app id.
        if not db.game_exists(appid):
            game = fetch_game_data(appid)
            if not game:
                flash(f"Couldn't find a Steam app with ID {appid}. Double-check the App ID.")
                return redirect(url_for('add_profile'))
            store_game(appid, game['name'], game['tag_ids'], game['capsule_image'])
            name = game['name']
        else:
            name = db.get_game(appid)['game_name']

        db.add_appid_to_developer(dev['developer_id'], appid)
        flash(f'Added game profile: {name}')
        return redirect(url_for('profile'))

    return render_template('add_profile.html')


# --- Developer auth ----------------------------------------------------------
@app.route('/developer/signup', methods=['GET', 'POST'])
def developer_signup():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not (name and email and password):
            flash('Name, email and password are all required.')
            return redirect(url_for('developer_signup'))
        token = secrets_lib.token_urlsafe(24)   # email-verification infra (not sent yet)
        pw_hash = generate_password_hash(password, method='pbkdf2:sha256')
        did, err = db.create_developer(name, email, pw_hash, token)
        if err:
            flash(err)
            return redirect(url_for('developer_signup'))
        session['developer_id'] = did
        flash('Account created. (Email verification will be enabled later.)')
        return redirect(url_for('profile'))
    return render_template('developer_signup.html')


@app.route('/developer/login', methods=['GET', 'POST'])
def developer_login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        dev = db.get_developer_by_email(email)
        if dev and dev.get('password_hash') and check_password_hash(dev['password_hash'], password):
            session['developer_id'] = dev['developer_id']
            return redirect(url_for('profile'))
        flash('Invalid email or password.')
        return redirect(url_for('developer_login'))
    return render_template('developer_login.html')


@app.route('/developer/logout')
def developer_logout():
    session.pop('developer_id', None)
    return redirect(url_for('developer_login'))


@app.route('/developer/verify-email/<token>')
def developer_verify_email(token):
    """Email-verification landing (infra ready; links aren't emailed yet)."""
    ok = db.verify_developer_token(token)
    flash('Email verified!' if ok else 'Invalid or expired verification link.')
    dest = 'profile' if session.get('developer_id') else 'developer_login'
    return redirect(url_for(dest))


@app.route('/developer/dashboard')
def developer_dashboard():
    dev = current_developer()
    if not dev:
        return redirect(url_for('developer_login'))
    kpis = db.get_developer_media_kpis(dev['developer_id'])
    winrates = sorted(db.get_scored_winrates(min_shown=1))
    import bisect
    n = len(winrates)
    for k in kpis:
        k['losses'] = k['shown'] - k['upvotes']
        k['percentile'] = (round(bisect.bisect_right(winrates, k['winrate']) / n * 100)
                           if (k['shown'] > 0 and n > 0) else None)
    summary = {
        'images': len(kpis),
        'matchups': sum(k['shown'] for k in kpis),
        'wins': sum(k['upvotes'] for k in kpis),
        'avg_winrate': round(sum(k['winrate'] for k in kpis) / len(kpis), 1) if kpis else 0,
    }
    return render_template('dashboard.html', developer=dev, kpis=kpis, summary=summary)


@app.route('/player')
def player_home():
    player = current_player()
    tables = {'alltime': [], 'twoweeks': [], 'multiplier': []}
    if player:
        tables['alltime'] = build_tag_rows(player.get('tags_totals_alltime'))
        tables['twoweeks'] = build_tag_rows(player.get('tags_totals_2weeks'))
        tables['multiplier'] = build_tag_rows(player.get('tags_totals_multiplier'), with_percent=True)
    ranking = player_category_ranking(player)
    vote_mult = db.vote_multiplier(player['vote_count']) if player else 1.0
    return render_template('player.html', player=player, tables=tables,
                           multiplier=RECENCY_MULTIPLIER, ranking=ranking,
                           vote_mult=vote_mult)


@app.route('/player/sync', methods=['POST'])
def player_sync():
    """Compute all-time, 2-week and multiplier tag totals; store on the row."""
    steam_id = session.get('steam_id')
    if not steam_id:
        return redirect(url_for('player_home'))
    alltime, twoweeks, multiplier = compute_player_tags(steam_id)
    db.update_player_tags(steam_id, alltime, twoweeks, multiplier)
    flash(f'Synced — {len(alltime)} all-time tags, {len(twoweeks)} from the last 2 weeks.')
    return redirect(url_for('player_home'))


@app.route('/player/feed')
def player_feed():
    """Show the player one unseen same-category image matchup to vote on."""
    player = current_player()
    if not player:
        return redirect(url_for('player_home'))
    matchup = next_matchup_for_player(player)
    return render_template('feed.html', player=player, matchup=matchup)


@app.route('/player/vote', methods=['POST'])
def player_vote():
    """Record a vote: score both images, log the matchup, mark it seen."""
    player = current_player()
    if not player:
        return redirect(url_for('player_home'))

    try:
        media1 = int(request.form['media1'])
        media2 = int(request.form['media2'])
        winner = int(request.form['winner'])
    except (KeyError, ValueError):
        flash('Invalid vote.')
        return redirect(url_for('player_feed'))

    if winner not in (media1, media2) or media1 == media2:
        flash('Invalid vote.')
        return redirect(url_for('player_feed'))
    loser = media2 if winner == media1 else media1

    matchup_id = db.get_or_create_matchup(media1, media2)
    db.bump_media_score(winner, upvote=True)   # shown +1, upvote +1
    db.bump_media_score(loser, upvote=False)   # shown +1
    db.log_matchup(player['player_id'], matchup_id, winner, player['player_id'])
    db.add_matchup_to_player(player['player_id'], matchup_id)

    awarded, mult, total = db.award_vote_points(player['player_id'])
    flash(f'+{awarded} points (x{mult}) — you now have {total}.')
    return redirect(url_for('player_feed'))


@app.route('/player/verify-email', methods=['POST'])
def player_verify_email():
    """Stub email verification: grants the one-time email bonus."""
    player = current_player()
    if not player:
        return redirect(url_for('player_home'))
    awarded = db.verify_player_email(player['player_id'])
    flash(f'Email verified — +{awarded} points!' if awarded else 'Email already verified.')
    return redirect(url_for('player_home'))


# Reward catalogue (display only — nothing is actually redeemable yet).
REWARDS = [
    {'name': 'Steam Gift Card $5', 'cost': 5000, 'icon': '🎮'},
    {'name': 'Steam Gift Card $10', 'cost': 9500, 'icon': '🎮'},
    {'name': 'Steam Gift Card $20', 'cost': 18000, 'icon': '🎮'},
    {'name': 'Amazon Gift Card $5', 'cost': 5200, 'icon': '📦'},
    {'name': 'Amazon Gift Card $10', 'cost': 10000, 'icon': '📦'},
    {'name': 'PayPal Cash $10', 'cost': 11000, 'icon': '💵'},
    {'name': 'Visa Prepaid $25', 'cost': 24000, 'icon': '💳'},
    {'name': 'Discord Nitro (1 month)', 'cost': 8000, 'icon': '🕹️'},
    {'name': 'Xbox Game Pass (1 month)', 'cost': 9000, 'icon': '🟢'},
    {'name': 'Humble Bundle Credit $10', 'cost': 10500, 'icon': '🎁'},
]


# Player testimonials for the join page (placeholder copy + placeholder avatars —
# swap the files in static/avatars and the text here for real ones later).
TESTIMONIALS = [
    {'name': 'vexx.exe', 'avatar': '/static/avatars/vexx.svg', 'meta': '1,140 votes · cashed out twice',
     'text': "I vote on capsules while I wait for a queue to pop. Cashed out a $10 Steam card last "
             "month — free games for looking at art, basically."},
    {'name': 'PixelPint', 'avatar': '/static/avatars/pixelpint.svg', 'meta': '860 votes · 9,300 points',
     'text': "Weirdly addictive. Two capsules, pick the better one, next one loads. I'll do fifty in "
             "a sitting without noticing."},
    {'name': 'Nyra', 'avatar': '/static/avatars/nyra.svg', 'meta': '2,405 votes · 1.8x multiplier',
     'text': "The multiplier is what hooked me. Every ten votes it ticks up, so the same ten minutes "
             "is worth way more than it was in my first week."},
    {'name': 'Frostbyte', 'avatar': '/static/avatars/frostbyte.svg', 'meta': '640 votes · 7 wishlist adds',
     'text': "Found three games I'd never have seen on Steam and wishlisted all of them. Getting paid "
             "on top of that still feels like a bug."},
    {'name': 'MoonlitJay', 'avatar': '/static/avatars/moonlitjay.svg', 'meta': '312 votes · joined in June',
     'text': "Signed in with Steam and had 300 points before I'd voted once. Whole sign-up took about "
             "four seconds."},
    {'name': 'Rook', 'avatar': '/static/avatars/rook.svg', 'meta': '1,980 votes · 14,200 points',
     'text': "It figured out I'm all grim pixel roguelikes in about a day, and now that's most of what "
             "it shows me. Voting on stuff I actually care about."},
]


@app.route('/player/join')
def player_join():
    """Sign-up pitch shown before Steam login: what you can win, how many players
    are already in, and what other players say. Steam login IS the sign-up."""
    real = db.get_platform_stats()
    players = real['players'] * STAT_BOOST['players']['mult'] + STAT_BOOST['players']['base']
    return render_template('player_join.html', players=players,
                           cards=gift_card_art(), testimonials=TESTIMONIALS)


@app.route('/player/rewards')
def player_rewards():
    """Show the player their balance and the (not-yet-active) cash-out catalogue."""
    player = current_player()
    if not player:
        return redirect(url_for('player_home'))
    balance = player.get('points', 0)
    mult = db.vote_multiplier(player.get('vote_count', 0))
    return render_template('rewards.html', player=player, balance=balance,
                           multiplier=mult, rewards=REWARDS)


@app.route('/player/login')
def player_login():
    """Kick off Steam OpenID login."""
    params = {
        'openid.ns': 'http://specs.openid.net/auth/2.0',
        'openid.mode': 'checkid_setup',
        'openid.return_to': url_for('player_callback', _external=True),
        'openid.realm': request.url_root,
        'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
        'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select',
    }
    return redirect(STEAM_OPENID_URL + '?' + urlencode(params))


@app.route('/player/callback')
def player_callback():
    """Verify Steam's response, extract SteamID64, upsert the player, log in."""
    steam_response = request.args.to_dict()
    steam_response['openid.mode'] = 'check_authentication'
    try:
        verification = requests.post(STEAM_OPENID_URL, data=steam_response, timeout=15)
    except requests.RequestException:
        flash('Could not reach Steam for verification. Try again.')
        return redirect(url_for('player_home'))

    if 'is_valid:true' not in verification.text:
        flash('Steam verification failed.')
        return redirect(url_for('player_home'))

    claimed_id = request.args.get('openid.claimed_id', '')
    steam_id = claimed_id.rsplit('/', 1)[-1]
    if not steam_id.isdigit():
        flash('Steam verification failed.')
        return redirect(url_for('player_home'))

    # Only fetch summary + insert for a steam_id we haven't seen before.
    if not db.get_player_by_steam_id(steam_id):
        summary = fetch_player_summary(steam_id)
        if summary:
            db.insert_player(steam_id, summary['username'],
                             summary['profileurl'], summary['avatar'])

    session['steam_id'] = steam_id
    return redirect(url_for('player_home'))


@app.route('/player/logout')
def player_logout():
    session.pop('steam_id', None)
    return redirect(url_for('player_home'))


@app.route('/player/test-login/<steam_id>')
def player_test_login(steam_id):
    """TEMPORARY: log in as a specific steam id without Steam OpenID, for testing.
    Remove this route once real Steam login testing is done."""
    if not steam_id.isdigit():
        abort(404)
    if not db.get_player_by_steam_id(steam_id):
        summary = fetch_player_summary(steam_id)
        if summary:
            db.insert_player(steam_id, summary['username'],
                             summary['profileurl'], summary['avatar'])
    session['steam_id'] = steam_id
    return redirect(url_for('player_home'))


# Tags present in at least this fraction of games are dropped before clustering
# (too generic to distinguish genres, e.g. Indie/Action/Singleplayer).
UBIQUITOUS_DF = 0.30


def _tfidf_weight(X):
    """Per-tag IDF weights for a binary game-tag matrix (down-weights common
    tags, up-weights rare/discriminative ones). Returns the idf vector."""
    n = X.shape[0]
    df = X.sum(axis=0)
    return np.log((1.0 + n) / (1.0 + df)) + 1.0


def _l2_normalize(M):
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return M / norms


def cluster_vectors(matrix, k_min=2, k_max=20):
    """K-means on L2-normalized vectors (so Euclidean ~ cosine); pick k by the
    best silhouette (cosine). Returns (labels, best_k, best_score, scores)."""
    X = np.array(matrix, dtype=float)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    Xn = X / norms

    n = len(Xn)
    k_hi = min(k_max, n - 1)
    best = None
    scores = {}
    for k in range(k_min, k_hi + 1):
        labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(Xn)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(Xn, labels, metric='cosine'))
        scores[k] = round(score, 4)
        if best is None or score > best[2]:
            best = (labels.tolist(), k, score)
    if best is None:
        return [0] * n, 1, 0.0, scores
    return best[0], best[1], best[2], scores


VECTORS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'vectors_cache.json')


def name_clusters(X, labels, k, top_n=3, min_support=0.25):
    """Name each cluster by its most distinctive tags (lift vs global freq).

    For each cluster, a tag scores high if it's common *within* the cluster
    relative to how common it is overall. Requires the tag to appear in at
    least `min_support` of the cluster's games so names aren't driven by noise.
    """
    name_map = db.get_tag_name_map()          # tag_id (pk) -> tag_name
    n = X.shape[0]
    global_p = (X.sum(axis=0) + 1) / (n + 1)  # smoothed global tag frequency
    cluster_names = {}
    for c in range(k):
        mask = labels == c
        size = int(mask.sum())
        if size == 0:
            cluster_names[c] = f'Cluster {c}'
            continue
        p_c = X[mask].mean(axis=0)             # fraction of cluster games w/ tag
        lift = p_c / global_p
        order = np.argsort(lift)[::-1]
        picks = [i for i in order if p_c[i] >= min_support][:top_n]
        if not picks:                          # fallback: just most common in cluster
            picks = list(np.argsort(p_c)[::-1][:top_n])
        labels_txt = [name_map.get(i + 1) or f'#{i + 1}' for i in picks]
        cluster_names[c] = ' / '.join(labels_txt)
    return cluster_names


def compute_vector_scatter():
    """Rebuild vectors, cluster (auto-k), name clusters, t-SNE layout to 2D."""
    rebuild_game_vectors()
    names, appids, matrix = [], [], []
    for game in db.get_all_games():
        vec = game['vector']
        if vec and any(vec):  # skip games with no known tags (all-zero)
            names.append(game['game_name'] or f"App {game['game_appid']}")
            appids.append(game['game_appid'])
            matrix.append(vec)

    dim = db.get_tag_dimension()
    if len(matrix) < 3:
        return {'points': [], 'count': len(matrix), 'dim': dim,
                'best_k': 0, 'silhouette': 0, 'scores': {}, 'cluster_names': {},
                'dropped_tags': []}

    X = np.array(matrix, dtype=float)               # binary presence
    n = X.shape[0]
    df = X.sum(axis=0)
    idf = _tfidf_weight(X)                            # down-weight common tags

    # Drop ubiquitous tags entirely: zero their idf so they carry no weight in
    # clustering, matching, or names (they're too generic to define a genre).
    name_map = db.get_tag_name_map()
    ubiquitous = (df / n) >= UBIQUITOUS_DF
    idf[ubiquitous] = 0.0
    dropped_tags = [name_map.get(i + 1) for i in np.where(ubiquitous)[0]]

    Xw_raw = X * idf
    keep = Xw_raw.sum(axis=1) > 0                     # drop games left with no signal
    Xw_raw, X = Xw_raw[keep], X[keep]
    names = [names[i] for i in range(len(names)) if keep[i]]
    appids = [appids[i] for i in range(len(appids)) if keep[i]]
    Xw = _l2_normalize(Xw_raw)                        # tf-idf, L2-normalized

    labels_list, best_k, score, scores = cluster_vectors(Xw)  # cluster in tf-idf space
    labels = np.array(labels_list)

    cluster_names = name_clusters(X * (idf > 0), labels, best_k)  # names exclude dropped tags

    # 2D layout via t-SNE on the tf-idf space (PCA-reduced to ~50 dims first).
    Xc = Xw - Xw.mean(axis=0)
    _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
    n_comp = min(50, Vt.shape[0])
    X_pca = Xc @ Vt[:n_comp].T
    perplexity = min(30, max(5, (len(X_pca) - 1) // 3))
    coords = TSNE(n_components=2, init='pca', perplexity=perplexity,
                  random_state=0).fit_transform(X_pca)

    # Cluster centroids in tf-idf space — used to match players (same weighting).
    centroids = {int(c): Xw[labels == c].mean(axis=0).round(6).tolist()
                 for c in range(best_k)}

    points = [{'x': round(float(coords[i, 0]), 3), 'y': round(float(coords[i, 1]), 3),
               'name': names[i], 'appid': int(appids[i]), 'cluster': int(labels[i])}
              for i in range(len(names))]
    return {'points': points, 'count': len(points), 'dim': dim,
            'best_k': best_k, 'silhouette': round(score, 4), 'scores': scores,
            'cluster_names': cluster_names, 'centroids': centroids,
            'idf': idf.round(6).tolist(), 'dropped_tags': dropped_tags}


@app.route('/dev/vectors')
def dev_vectors():
    """TESTING ONLY: scatter of every game's binary tag vector (PCA to 2D),
    color-coded by K-means cluster (auto-k via silhouette). Result is cached to
    disk since it takes minutes to compute; add ?refresh=1 to recompute."""
    if request.args.get('refresh') or not os.path.exists(VECTORS_CACHE_FILE):
        data = compute_vector_scatter()
        with open(VECTORS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    else:
        with open(VECTORS_CACHE_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('vectors.html', **data)


if __name__ == '__main__':
    db.init_db()
    seeded = db.seed_tags()
    cats = db.seed_image_categories()
    if seeded:
        print(f'Seeded {seeded} tags from dataset file.')
    else:
        print('No tags_data.py / tags.json found yet — tags table left empty '
              '(game pages will show tag ids until you seed it).')
    print(f'Seeded {cats} image categories.')
    app.run(debug=True)
