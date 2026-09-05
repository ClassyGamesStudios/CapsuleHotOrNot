"""
MySQL layer for the GameDev Portal.

Database: game_profiles_db
Tables:
  developer_profile(developer_id PK auto, developer_name, game_appid JSON list)
  game_profile(game_appid PK, game_name, tag_id JSON list, capsule_url)
  tags(tag_id PK auto, steam_tag_id UNIQUE, tag_name)

List-type columns are stored as native MySQL JSON. Connection config can be
overridden with env vars DB_HOST / DB_USER / DB_PASSWORD / DB_NAME.
"""

import os
import json
import importlib.util
import pymysql

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', 'root'),
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor,
    'autocommit': True,
}
DB_NAME = os.environ.get('DB_NAME', 'game_profiles_db')


def get_connection(with_db=True):
    cfg = dict(DB_CONFIG)
    if with_db:
        cfg['database'] = DB_NAME
    return pymysql.connect(**cfg)


# --- Schema ------------------------------------------------------------------
def init_db():
    """Create the database and the three tables if they don't exist."""
    conn = get_connection(with_db=False)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {DB_NAME} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cur.execute(f"USE {DB_NAME}")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS developer_profile (
                developer_id   INT AUTO_INCREMENT PRIMARY KEY,
                developer_name VARCHAR(255) NOT NULL UNIQUE,
                game_appid     JSON NOT NULL,
                mediaIDs       JSON
            )
        """)
        _ensure_column(cur, 'developer_profile', 'mediaIDs', 'mediaIDs JSON')
        # Developer auth (email + password; email verification infra kept ready).
        _ensure_column(cur, 'developer_profile', 'email', 'email VARCHAR(255) UNIQUE')
        _ensure_column(cur, 'developer_profile', 'password_hash', 'password_hash VARCHAR(255)')
        _ensure_column(cur, 'developer_profile', 'email_verified', 'email_verified TINYINT NOT NULL DEFAULT 0')
        _ensure_column(cur, 'developer_profile', 'verification_token', 'verification_token VARCHAR(64)')
        # Names are no longer unique (email is the identity); drop legacy unique index.
        _drop_index_if_exists(cur, 'developer_profile', 'developer_name')

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_profile (
                game_appid  BIGINT PRIMARY KEY,
                game_name   VARCHAR(500),
                tag_id      JSON,
                capsule_url TEXT,
                vector      JSON
            )
        """)
        _ensure_column(cur, 'game_profile', 'vector', 'vector JSON')
        _ensure_column(cur, 'game_profile', 'comingSoonFlag', 'comingSoonFlag TINYINT')
        _ensure_column(cur, 'game_profile', 'releaseDateFlag', 'releaseDateFlag DATE')

        # Migration: old schema had tag_id (the Steam tag id) as the PK. Rename it
        # to steam_tag_id and add a new auto-increment tag_id PK. Existing rows keep
        # their current order; the new tag_id is assigned 1..N in that order.
        if _table_exists(cur, 'tags') and not _column_exists(cur, 'tags', 'steam_tag_id'):
            cur.execute("""
                ALTER TABLE tags
                    DROP PRIMARY KEY,
                    CHANGE COLUMN tag_id steam_tag_id BIGINT NOT NULL,
                    ADD COLUMN tag_id INT AUTO_INCREMENT PRIMARY KEY FIRST,
                    ADD UNIQUE KEY uniq_steam_tag_id (steam_tag_id)
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                tag_id       INT AUTO_INCREMENT PRIMARY KEY,
                steam_tag_id BIGINT UNIQUE,
                tag_name     VARCHAR(255)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS players_profile (
                player_id             INT AUTO_INCREMENT PRIMARY KEY,
                steam_id              VARCHAR(32) NOT NULL UNIQUE,
                username              VARCHAR(255),
                profileurl            VARCHAR(500),
                avatar                VARCHAR(500),
                tags_totals_alltime   JSON,
                tags_totals_2weeks    JSON,
                tags_totals_multiplier JSON
            )
        """)

        # Migration: rename the old single tags_totals column to _alltime, then
        # make sure the three current columns exist (older tables predate them).
        if _column_exists(cur, 'players_profile', 'tags_totals') \
                and not _column_exists(cur, 'players_profile', 'tags_totals_alltime'):
            cur.execute(
                "ALTER TABLE players_profile RENAME COLUMN tags_totals TO tags_totals_alltime"
            )
        _ensure_column(cur, 'players_profile', 'tags_totals_alltime', 'tags_totals_alltime JSON')
        _ensure_column(cur, 'players_profile', 'tags_totals_2weeks', 'tags_totals_2weeks JSON')
        _ensure_column(cur, 'players_profile', 'tags_totals_multiplier', 'tags_totals_multiplier JSON')

        cur.execute("""
            CREATE TABLE IF NOT EXISTS image_categories (
                imageCategoryID INT AUTO_INCREMENT PRIMARY KEY,
                name            VARCHAR(100) NOT NULL UNIQUE,
                width           INT,
                height          INT,
                match_mode      VARCHAR(10) NOT NULL DEFAULT 'exact'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS media (
                mediaID      INT AUTO_INCREMENT PRIMARY KEY,
                appID        BIGINT,
                categoryID   INT,
                path         VARCHAR(500),
                developerID  INT
            )
        """)

        # One row per mediaID; counters incremented as it appears in matchups.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS media_score_log (
                mediaScoreLogID  INT AUTO_INCREMENT PRIMARY KEY,
                mediaID          INT NOT NULL UNIQUE,
                numberTimesShown INT NOT NULL DEFAULT 0,
                numberUpVotes    INT NOT NULL DEFAULT 0,
                winrate          FLOAT NOT NULL DEFAULT 0
            )
        """)

        # Unique unordered pair: insert normalized so pic1MediaID < pic2MediaID,
        # so A-vs-B and B-vs-A collapse to one row.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS matchups (
                matchUpID   INT AUTO_INCREMENT PRIMARY KEY,
                pic1MediaID INT NOT NULL,
                pic2MediaID INT NOT NULL,
                UNIQUE KEY uniq_pair (pic1MediaID, pic2MediaID)
            )
        """)

        # Full log of every vote cast.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS matchup_log (
                logID         INT AUTO_INCREMENT PRIMARY KEY,
                playerID      INT,
                matchupID     INT,
                winnerMediaID INT,
                timestamp     DATETIME DEFAULT CURRENT_TIMESTAMP,
                playerIDVoted INT
            )
        """)

        # Matchups each player has already seen (so we don't re-show them).
        _ensure_column(cur, 'players_profile', 'matchupIDs', 'matchupIDs JSON')

        # Points / rewards system.
        _ensure_column(cur, 'players_profile', 'points', 'points INT NOT NULL DEFAULT 0')
        _ensure_column(cur, 'players_profile', 'vote_count', 'vote_count INT NOT NULL DEFAULT 0')
        _ensure_column(cur, 'players_profile', 'email_verified', 'email_verified TINYINT NOT NULL DEFAULT 0')
    conn.close()


# Category dimension rules. match_mode 'exact' -> width AND height must match;
# 'either' -> width OR height must match (used for logo).
IMAGE_CATEGORIES = [
    ('header capsule', 920, 430, 'exact'),
    ('small', 462, 174, 'exact'),
    ('main', 1232, 706, 'exact'),
    ('vertical', 748, 896, 'exact'),
    ('background', 1438, 810, 'exact'),
    ('library capsule', 600, 900, 'exact'),
    ('library header', 920, 430, 'exact'),
    ('library hero', 3840, 1240, 'exact'),
    ('logo', 1280, 720, 'either'),
    ('screenshots', 1920, 1080, 'exact'),
]


def seed_image_categories():
    """Insert the 10 image categories if not already present. Returns count."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO image_categories (name, width, height, match_mode)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                 width=VALUES(width), height=VALUES(height), match_mode=VALUES(match_mode)""",
            IMAGE_CATEGORIES,
        )
    conn.close()
    return len(IMAGE_CATEGORIES)


def _table_exists(cur, table):
    cur.execute(
        """SELECT COUNT(*) AS c FROM information_schema.TABLES
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""",
        (DB_NAME, table),
    )
    return cur.fetchone()['c'] > 0


def _drop_index_if_exists(cur, table, index):
    cur.execute(
        """SELECT COUNT(*) AS c FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s""",
        (DB_NAME, table, index),
    )
    if cur.fetchone()['c'] > 0:
        cur.execute(f"ALTER TABLE {table} DROP INDEX {index}")


def _column_exists(cur, table, column):
    cur.execute(
        """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s""",
        (DB_NAME, table, column),
    )
    return cur.fetchone()['c'] > 0


def _ensure_column(cur, table, column, ddl):
    if not _column_exists(cur, table, column):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# --- Tag reference table -----------------------------------------------------
def _load_tag_dataset():
    """Load the full tag list from tags_data.py or tags.json if present.

    Accepts, in tags_data.py, a module-level `tags` that is either the list
    itself or a dict shaped like {'tags': [...]}. tags.json may hold the same.
    Returns a list of tag dicts, or None if no file is found.
    """
    here = os.path.dirname(os.path.abspath(__file__))

    py_path = os.path.join(here, 'tags_data.py')
    if os.path.exists(py_path):
        spec = importlib.util.spec_from_file_location('tags_data', py_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data = getattr(mod, 'tags', None) or getattr(mod, 'TAGS', None)
        if isinstance(data, dict):
            data = data.get('tags')
        if data:
            return data

    json_path = os.path.join(here, 'tags.json')
    if os.path.exists(json_path):
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get('tags')
        if data:
            return data

    return None


def seed_tags():
    """Populate the tags table from the dataset file. Returns rows inserted."""
    dataset = _load_tag_dataset()
    if not dataset:
        return 0

    rows = []
    for t in dataset:
        sid = t.get('steamId') if 'steamId' in t else t.get('steam_id')
        if sid is None:
            continue
        name = t.get('name') if 'name' in t else t.get('tag_name')
        rows.append((sid, name))

    if not rows:
        return 0

    conn = get_connection()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO tags (steam_tag_id, tag_name)
               VALUES (%s, %s)
               ON DUPLICATE KEY UPDATE tag_name=VALUES(tag_name)""",
            rows,
        )
    conn.close()
    return len(rows)


def get_tags_by_ids(steam_tag_ids):
    """Return {tag_id, steam_tag_id, tag_name} for the given Steam tag ids,
    preserving input order.

    Lookups are by steam_tag_id (the value scraped from Steam and stored in
    game_profile.tag_id / players_profile.tags_totals). Ids not present come
    back with tag_id/tag_name None so pages can still render '#<steam id>'.
    """
    if not steam_tag_ids:
        return []
    conn = get_connection()
    with conn.cursor() as cur:
        placeholders = ','.join(['%s'] * len(steam_tag_ids))
        cur.execute(
            f"SELECT tag_id, steam_tag_id, tag_name FROM tags "
            f"WHERE steam_tag_id IN ({placeholders})",
            steam_tag_ids,
        )
        found = {r['steam_tag_id']: r for r in cur.fetchall()}
    conn.close()

    result = []
    for sid in steam_tag_ids:
        r = found.get(sid)
        result.append({
            'tag_id': r['tag_id'] if r else None,
            'steam_tag_id': sid,
            'tag_name': r['tag_name'] if r else None,
        })
    return result


# --- Developer auth ----------------------------------------------------------
def _decode_developer(row):
    if row:
        row['game_appid'] = _as_list(row.get('game_appid'))
        row['mediaIDs'] = _as_list(row.get('mediaIDs'))
    return row


def create_developer(name, email, password_hash, verification_token):
    """Create a developer account. Returns (developer_id, None) or (None, error)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO developer_profile
                     (developer_name, email, password_hash, verification_token, game_appid, mediaIDs)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (name, email, password_hash, verification_token, json.dumps([]), json.dumps([])),
            )
            did = cur.lastrowid
    except pymysql.err.IntegrityError:
        conn.close()
        return None, 'That email is already registered.'
    conn.close()
    return did, None


def get_developer_by_email(email):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM developer_profile WHERE email=%s", (email,))
        row = cur.fetchone()
    conn.close()
    return _decode_developer(row)


def get_developer_by_id(developer_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM developer_profile WHERE developer_id=%s", (developer_id,))
        row = cur.fetchone()
    conn.close()
    return _decode_developer(row)


def verify_developer_token(token):
    """Mark a developer's email verified from a verification token. Returns bool."""
    conn = get_connection()
    ok = False
    with conn.cursor() as cur:
        cur.execute("SELECT developer_id FROM developer_profile WHERE verification_token=%s", (token,))
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE developer_profile SET email_verified=1, verification_token=NULL WHERE developer_id=%s",
                (row['developer_id'],),
            )
            ok = True
    conn.close()
    return ok


# --- Developer dashboard / media KPIs ---------------------------------------
def get_developer_media_kpis(developer_id):
    """Per-uploaded-image stats for the developer dashboard."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.mediaID, m.appID, m.path,
                      g.game_name, c.name AS category_name,
                      COALESCE(s.numberTimesShown, 0) AS shown,
                      COALESCE(s.numberUpVotes, 0)    AS upvotes,
                      COALESCE(s.winrate, 0)          AS winrate
               FROM media m
               LEFT JOIN game_profile g ON g.game_appid = m.appID
               LEFT JOIN image_categories c ON c.imageCategoryID = m.categoryID
               LEFT JOIN media_score_log s ON s.mediaID = m.mediaID
               WHERE m.developerID = %s
               ORDER BY shown DESC, winrate DESC""",
            (developer_id,),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def get_scored_winrates(min_shown=1):
    """All win rates for images with at least min_shown appearances (for percentiles)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT winrate FROM media_score_log WHERE numberTimesShown >= %s", (min_shown,)
        )
        vals = [r['winrate'] for r in cur.fetchall()]
    conn.close()
    return vals


# --- Matchup pools -----------------------------------------------------------
def get_developer_uploaded_media(category_id):
    """Developer-uploaded images in a category (developerID not null)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.mediaID, m.appID, m.path, g.game_name
               FROM media m LEFT JOIN game_profile g ON g.game_appid = m.appID
               WHERE m.categoryID = %s AND m.developerID IS NOT NULL""",
            (category_id,),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def get_all_media_in_category(category_id):
    """All images in a category (mediaID, appID, path, game_name) — opponent pool."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.mediaID, m.appID, m.path, g.game_name
               FROM media m LEFT JOIN game_profile g ON g.game_appid = m.appID
               WHERE m.categoryID = %s""",
            (category_id,),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def get_eligible_media_in_category(category_id):
    """Images of UPCOMING games only (coming soon OR future release date), with
    developerID so callers can prefer developer-uploaded ones. This is the pool
    the feed draws from — the platform only compares brand-new games."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.mediaID, m.appID, m.path, m.developerID, g.game_name
               FROM media m JOIN game_profile g ON g.game_appid = m.appID
               WHERE m.categoryID = %s
                 AND (g.comingSoonFlag = 1
                      OR (g.releaseDateFlag IS NOT NULL AND g.releaseDateFlag > CURDATE()))""",
            (category_id,),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


# --- Developer ---------------------------------------------------------------
def get_or_create_developer(name):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM developer_profile WHERE developer_name=%s", (name,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO developer_profile (developer_name, game_appid) VALUES (%s, %s)",
                (name, json.dumps([])),
            )
            cur.execute("SELECT * FROM developer_profile WHERE developer_name=%s", (name,))
            row = cur.fetchone()
    conn.close()
    row['game_appid'] = _as_list(row['game_appid'])
    row['mediaIDs'] = _as_list(row.get('mediaIDs'))
    return row


def delete_media(media_id):
    """Delete a media row and everything referencing it (matchups, their logs,
    and its score row). Returns the deleted media row (for file cleanup) or None."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM media WHERE mediaID=%s", (media_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        cur.execute(
            "SELECT matchUpID FROM matchups WHERE pic1MediaID=%s OR pic2MediaID=%s",
            (media_id, media_id),
        )
        mids = [r['matchUpID'] for r in cur.fetchall()]
        if mids:
            ph = ','.join(['%s'] * len(mids))
            cur.execute(f"DELETE FROM matchup_log WHERE matchupID IN ({ph})", mids)
            cur.execute(f"DELETE FROM matchups WHERE matchUpID IN ({ph})", mids)
        cur.execute("DELETE FROM media_score_log WHERE mediaID=%s", (media_id,))
        cur.execute("DELETE FROM media WHERE mediaID=%s", (media_id,))
    conn.close()
    return row


def remove_mediaid_from_developer(developer_id, media_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT mediaIDs FROM developer_profile WHERE developer_id=%s", (developer_id,))
        row = cur.fetchone()
        ids = [i for i in (_as_list(row['mediaIDs']) if row else []) if i != media_id]
        cur.execute("UPDATE developer_profile SET mediaIDs=%s WHERE developer_id=%s",
                    (json.dumps(ids), developer_id))
    conn.close()


def remove_appid_from_developer(developer_id, appid):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT game_appid FROM developer_profile WHERE developer_id=%s", (developer_id,))
        row = cur.fetchone()
        ids = [a for a in (_as_list(row['game_appid']) if row else []) if a != appid]
        cur.execute("UPDATE developer_profile SET game_appid=%s WHERE developer_id=%s",
                    (json.dumps(ids), developer_id))
    conn.close()


def sync_developer_mediaids(developer_id):
    """Reset the developer's mediaIDs list to the media they actually own
    (repairs stale/orphaned ids)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT mediaID FROM media WHERE developerID=%s ORDER BY mediaID", (developer_id,))
        ids = [r['mediaID'] for r in cur.fetchall()]
        cur.execute("UPDATE developer_profile SET mediaIDs=%s WHERE developer_id=%s",
                    (json.dumps(ids), developer_id))
    conn.close()
    return ids


def add_mediaid_to_developer(developer_id, media_id):
    """Append a media id to the developer's mediaIDs list (no duplicates)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mediaIDs FROM developer_profile WHERE developer_id=%s", (developer_id,)
        )
        row = cur.fetchone()
        ids = _as_list(row['mediaIDs']) if row else []
        if media_id not in ids:
            ids.append(media_id)
            cur.execute(
                "UPDATE developer_profile SET mediaIDs=%s WHERE developer_id=%s",
                (json.dumps(ids), developer_id),
            )
    conn.close()
    return ids


def add_appid_to_developer(developer_id, appid):
    """Append an app id to the developer's game_appid list (no duplicates)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_appid FROM developer_profile WHERE developer_id=%s", (developer_id,)
        )
        row = cur.fetchone()
        appids = _as_list(row['game_appid']) if row else []
        if appid not in appids:
            appids.append(appid)
            cur.execute(
                "UPDATE developer_profile SET game_appid=%s WHERE developer_id=%s",
                (json.dumps(appids), developer_id),
            )
    conn.close()
    return appids


# --- Game profile ------------------------------------------------------------
def game_exists(appid):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM game_profile WHERE game_appid=%s", (appid,))
        exists = cur.fetchone() is not None
    conn.close()
    return exists


def insert_game(appid, name, tag_ids, capsule_url):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO game_profile (game_appid, game_name, tag_id, capsule_url)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                 game_name=VALUES(game_name), tag_id=VALUES(tag_id),
                 capsule_url=VALUES(capsule_url)""",
            (appid, name, json.dumps(tag_ids), capsule_url),
        )
    conn.close()


def get_game(appid):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM game_profile WHERE game_appid=%s", (appid,))
        row = cur.fetchone()
    conn.close()
    if row:
        row['tag_id'] = _as_list(row['tag_id'])
    return row


def set_game_release(appid, coming_soon, release_date):
    """Store the release info: comingSoonFlag (bool) and releaseDateFlag (date/None)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE game_profile SET comingSoonFlag=%s, releaseDateFlag=%s WHERE game_appid=%s",
            (1 if coming_soon else 0, release_date, appid),
        )
    conn.close()


def get_feed_eligible_appids(appids):
    """Of the given appids, those eligible for the feed: coming soon, OR a
    release date strictly in the future (upcoming games only)."""
    if not appids:
        return set()
    conn = get_connection()
    with conn.cursor() as cur:
        placeholders = ','.join(['%s'] * len(appids))
        cur.execute(
            f"""SELECT game_appid FROM game_profile
                WHERE game_appid IN ({placeholders})
                  AND (comingSoonFlag = 1
                       OR (releaseDateFlag IS NOT NULL AND releaseDateFlag > CURDATE()))""",
            list(appids),
        )
        eligible = {r['game_appid'] for r in cur.fetchall()}
    conn.close()
    return eligible


def set_game_vector(appid, vector):
    """Store a game's 448-dim tag-weight vector (JSON list)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE game_profile SET vector=%s WHERE game_appid=%s",
            (json.dumps(vector), appid),
        )
    conn.close()


def get_all_games():
    """All games with tag_id list and vector decoded."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT game_appid, game_name, tag_id, vector FROM game_profile")
        rows = cur.fetchall()
    conn.close()
    for r in rows:
        r['tag_id'] = _as_list(r['tag_id'])
        r['vector'] = _as_list(r['vector'])
    return rows


def get_tag_pk_map():
    """Map steam_tag_id -> tag_id (primary key) for all tags."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT steam_tag_id, tag_id FROM tags")
        m = {r['steam_tag_id']: r['tag_id'] for r in cur.fetchall()}
    conn.close()
    return m


def get_tag_name_map():
    """Map tag_id (PK) -> tag_name for all tags."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT tag_id, tag_name FROM tags")
        m = {r['tag_id']: r['tag_name'] for r in cur.fetchall()}
    conn.close()
    return m


def get_tag_dimension():
    """Vector length = highest tag_id (PKs are 1..N contiguous)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(tag_id) AS mx FROM tags")
        mx = cur.fetchone()['mx']
    conn.close()
    return int(mx or 0)


def get_games(appids):
    """Return game_profile rows for a list of app ids, in the given order."""
    if not appids:
        return []
    conn = get_connection()
    with conn.cursor() as cur:
        placeholders = ','.join(['%s'] * len(appids))
        cur.execute(f"SELECT * FROM game_profile WHERE game_appid IN ({placeholders})", appids)
        rows = {r['game_appid']: r for r in cur.fetchall()}
    conn.close()
    ordered = []
    for a in appids:
        r = rows.get(a)
        if r:
            r['tag_id'] = _as_list(r['tag_id'])
            ordered.append(r)
    return ordered


# --- Image categories & media ------------------------------------------------
def get_image_categories():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM image_categories ORDER BY imageCategoryID")
        rows = cur.fetchall()
    conn.close()
    return rows


def get_image_category(category_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM image_categories WHERE imageCategoryID=%s", (category_id,))
        row = cur.fetchone()
    conn.close()
    return row


def insert_media(appid, category_id, path, developer_id):
    """Insert a media row and return the new mediaID."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO media (appID, categoryID, path, developerID)
               VALUES (%s, %s, %s, %s)""",
            (appid, category_id, path, developer_id),
        )
        media_id = cur.lastrowid
    conn.close()
    return media_id


def update_media_path(media_id, path):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE media SET path=%s WHERE mediaID=%s", (path, media_id))
    conn.close()


def get_media(media_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM media WHERE mediaID=%s", (media_id,))
        row = cur.fetchone()
    conn.close()
    return row


def get_media_by_appids(appids, category_id):
    """Return {appID: media_row} for games that have media in a category."""
    if not appids:
        return {}
    conn = get_connection()
    with conn.cursor() as cur:
        placeholders = ','.join(['%s'] * len(appids))
        cur.execute(
            f"SELECT * FROM media WHERE categoryID=%s AND appID IN ({placeholders})",
            [category_id] + list(appids),
        )
        rows = cur.fetchall()
    conn.close()
    return {r['appID']: r for r in rows}


# --- Matchups / voting -------------------------------------------------------
def get_platform_stats():
    """Real counts for the landing page: games, players, matches, developers."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM game_profile")
        games = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) n FROM players_profile")
        players = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) n FROM matchup_log")
        matches = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) n FROM developer_profile")
        developers = cur.fetchone()['n']
    conn.close()
    return {'games': games, 'players': players, 'matches': matches, 'developers': developers}


def get_capsule_wall(category_id, limit=40):
    """Random capsules of UPCOMING games for the landing-page wall — never the
    big released classics, only the new games players actually vote on."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.appID, m.path, g.game_name
               FROM media m JOIN game_profile g ON g.game_appid = m.appID
               WHERE m.categoryID = %s
                 AND (g.comingSoonFlag = 1
                      OR (g.releaseDateFlag IS NOT NULL AND g.releaseDateFlag > CURDATE()))
               ORDER BY RAND() LIMIT %s""",
            (category_id, limit),
        )
        rows = cur.fetchall()
    conn.close()
    return [{'appid': r['appID'], 'path': r['path'], 'name': r['game_name'] or 'Game'} for r in rows]


def get_recent_matches(limit=12):
    """Most recent votes with both capsules, game names, and which side won."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.logID, l.winnerMediaID, mu.pic1MediaID, mu.pic2MediaID,
                      m1.path AS p1, g1.game_name AS n1,
                      m2.path AS p2, g2.game_name AS n2
               FROM matchup_log l
               JOIN matchups mu ON mu.matchUpID = l.matchupID
               JOIN media m1 ON m1.mediaID = mu.pic1MediaID
               JOIN media m2 ON m2.mediaID = mu.pic2MediaID
               LEFT JOIN game_profile g1 ON g1.game_appid = m1.appID
               LEFT JOIN game_profile g2 ON g2.game_appid = m2.appID
               ORDER BY l.logID DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall()
    conn.close()
    return [
        {'p1': r['p1'], 'n1': r['n1'] or 'Game', 'p2': r['p2'], 'n2': r['n2'] or 'Game',
         'winner_side': 1 if r['winnerMediaID'] == r['pic1MediaID'] else 2}
        for r in rows
    ]


def find_matchup(media_a, media_b):
    """Return the matchUpID for an unordered image pair, or None if not created."""
    lo, hi = sorted((media_a, media_b))
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT matchUpID FROM matchups WHERE pic1MediaID=%s AND pic2MediaID=%s", (lo, hi)
        )
        row = cur.fetchone()
    conn.close()
    return row['matchUpID'] if row else None


def get_or_create_matchup(media_a, media_b):
    """Get (or create) the matchUpID for an unordered pair (pic1 < pic2)."""
    lo, hi = sorted((media_a, media_b))
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT matchUpID FROM matchups WHERE pic1MediaID=%s AND pic2MediaID=%s", (lo, hi)
        )
        row = cur.fetchone()
        if row:
            matchup_id = row['matchUpID']
        else:
            cur.execute(
                "INSERT INTO matchups (pic1MediaID, pic2MediaID) VALUES (%s, %s)", (lo, hi)
            )
            matchup_id = cur.lastrowid
    conn.close()
    return matchup_id


def bump_media_score(media_id, upvote):
    """Increment shown (+1) for a media, upvotes (+1 if upvote), recompute winrate."""
    up = 1 if upvote else 0
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO media_score_log (mediaID, numberTimesShown, numberUpVotes, winrate)
               VALUES (%s, 1, %s, %s)
               ON DUPLICATE KEY UPDATE
                 numberTimesShown = numberTimesShown + 1,
                 numberUpVotes    = numberUpVotes + %s,
                 winrate          = numberUpVotes / numberTimesShown * 100""",
            (media_id, up, up * 100.0, up),
        )
    conn.close()


def log_matchup(player_id, matchup_id, winner_media_id, player_id_voted):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO matchup_log (playerID, matchupID, winnerMediaID, playerIDVoted)
               VALUES (%s, %s, %s, %s)""",
            (player_id, matchup_id, winner_media_id, player_id_voted),
        )
    conn.close()


def add_matchup_to_player(player_id, matchup_id):
    """Append a matchUpID to the player's seen list (no duplicates)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT matchupIDs FROM players_profile WHERE player_id=%s", (player_id,))
        row = cur.fetchone()
        ids = _as_list(row['matchupIDs']) if row else []
        if matchup_id not in ids:
            ids.append(matchup_id)
            cur.execute(
                "UPDATE players_profile SET matchupIDs=%s WHERE player_id=%s",
                (json.dumps(ids), player_id),
            )
    conn.close()


def get_media_for_game(appid, developer_id):
    """Media rows a developer has uploaded for a game, with category names."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT m.*, c.name AS category_name
               FROM media m LEFT JOIN image_categories c ON m.categoryID = c.imageCategoryID
               WHERE m.appID=%s AND m.developerID=%s
               ORDER BY m.mediaID""",
            (appid, developer_id),
        )
        rows = cur.fetchall()
    conn.close()
    return rows


# --- Player (Steam login side) -----------------------------------------------
def get_player_by_steam_id(steam_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM players_profile WHERE steam_id=%s", (steam_id,))
        row = cur.fetchone()
    conn.close()
    if row:
        for col in ('tags_totals_alltime', 'tags_totals_2weeks', 'tags_totals_multiplier'):
            if row.get(col) is not None and not isinstance(row[col], dict):
                row[col] = json.loads(row[col])
        row['matchupIDs'] = _as_list(row.get('matchupIDs'))
    return row


def update_player_tags(steam_id, alltime, twoweeks, multiplier):
    """Store the three {tag_id: minutes} maps on the player row.

    JSON object keys are strings, so tag ids come back as strings on read.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE players_profile
               SET tags_totals_alltime=%s, tags_totals_2weeks=%s, tags_totals_multiplier=%s
               WHERE steam_id=%s""",
            (json.dumps(alltime), json.dumps(twoweeks), json.dumps(multiplier), steam_id),
        )
    conn.close()


SIGNUP_BONUS = 300       # points for linking a Steam account (on sign up)
EMAIL_BONUS = 200        # points for verifying email
BASE_VOTE_POINTS = 10    # base points per vote (before multiplier)


def insert_player(steam_id, username, profileurl, avatar):
    """Insert a new player row with the Steam-link signup bonus. No-op if the
    steam_id already exists (so the bonus is only granted once)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO players_profile (steam_id, username, profileurl, avatar, points)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE steam_id=steam_id""",
            (steam_id, username, profileurl, avatar, SIGNUP_BONUS),
        )
    conn.close()


def vote_multiplier(vote_count):
    """1.0x, then +0.1x per completed 10 votes (10 -> 1.1x, 20 -> 1.2x, ...)."""
    return round(1.0 + (vote_count // 10) * 0.1, 1)


def award_vote_points(player_id):
    """Award points for one vote using the player's current multiplier, and
    increment their vote count. Returns (points_awarded, multiplier, new_total)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT vote_count, points FROM players_profile WHERE player_id=%s", (player_id,))
        row = cur.fetchone()
        vc = row['vote_count'] if row else 0
        mult = vote_multiplier(vc)
        awarded = round(BASE_VOTE_POINTS * mult)
        cur.execute(
            "UPDATE players_profile SET vote_count = vote_count + 1, points = points + %s WHERE player_id=%s",
            (awarded, player_id),
        )
        new_total = (row['points'] if row else 0) + awarded
    conn.close()
    return awarded, mult, new_total


def verify_player_email(player_id):
    """Mark email verified and grant the one-time bonus. Returns points awarded (0 if already)."""
    conn = get_connection()
    awarded = 0
    with conn.cursor() as cur:
        cur.execute("SELECT email_verified FROM players_profile WHERE player_id=%s", (player_id,))
        row = cur.fetchone()
        if row and not row['email_verified']:
            cur.execute(
                "UPDATE players_profile SET email_verified=1, points = points + %s WHERE player_id=%s",
                (EMAIL_BONUS, player_id),
            )
            awarded = EMAIL_BONUS
    conn.close()
    return awarded


# --- helpers -----------------------------------------------------------------
def _as_list(value):
    """JSON columns may come back as str (pymysql) or already-decoded list."""
    if value is None:
        return []
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


if __name__ == '__main__':
    init_db()
    n = seed_tags()
    c = seed_image_categories()
    print(f'DB initialised. Seeded {n} tags, {c} image categories.')
