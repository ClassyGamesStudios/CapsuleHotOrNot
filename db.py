"""
MySQL layer for CapsuleHotOrNot.

Database: game_profiles_db
Tables:
  developer_profile(developer_id PK, developer_name, email UNIQUE, password_hash,
                    email_verified, verification_token, game_appid JSON, mediaIDs JSON)
  game_profile(game_appid PK, game_name, tag_id JSON, capsule_url, vector JSON,
               comingSoonFlag, releaseDateFlag)
  tags(tag_id PK auto, steam_tag_id UNIQUE, tag_name)
  players_profile(player_id PK, steam_id UNIQUE, username, profileurl, avatar,
                  tags_totals_alltime/_2weeks/_multiplier JSON, matchupIDs JSON,
                  points, vote_count, email_verified)
  image_categories(imageCategoryID PK, name UNIQUE, width, height, match_mode)
  media(mediaID PK, appID, categoryID, path, developerID)
  media_score_log(mediaScoreLogID PK, mediaID UNIQUE, numberTimesShown,
                  numberUpVotes, winrate)
  matchups(matchUpID PK, pic1MediaID, pic2MediaID)  -- UNIQUE unordered pair
  matchup_log(logID PK, playerID, matchupID, winnerMediaID, timestamp,
              playerIDVoted)                        -- UNIQUE(playerID, matchupID)

List-type columns are stored as native MySQL JSON. Connection config can be
overridden with env vars DB_HOST / DB_USER / DB_PASSWORD / DB_NAME.
init_db() is idempotent: it creates what's missing and runs the column/index
migrations, so it's safe to call on every start.
"""

import os
import json
import datetime
import secrets
import importlib.util
import pymysql

def _load_env_file(path=None):
    """Load KEY=value lines from .env into the environment, if it exists.

    Real environment variables always win, so systemd's EnvironmentFile, a
    shell `export`, or a test overriding DB_NAME are never clobbered. Done here
    (before DB_CONFIG is built) because db is imported by every entry point —
    app.py, wsgi.py, selftest.py and the one-off scripts — so all of them get
    the same configuration without each having to load it.
    """
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file()

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

        # Playtime sync cache: syncing scrapes Steam for minutes, so it runs at
        # most every SYNC_INTERVAL_DAYS instead of on a button press.
        _ensure_column(cur, 'players_profile', 'last_sync', 'last_sync DATETIME')
        # Daily login streak (day 1..10, consecutive days only).
        _ensure_column(cur, 'players_profile', 'streak_day', 'streak_day INT NOT NULL DEFAULT 0')
        _ensure_column(cur, 'players_profile', 'last_reward_date', 'last_reward_date DATE')
        # Referrals: own code to share, plus the code this account signed up with.
        _ensure_column(cur, 'players_profile', 'referral_code', 'referral_code VARCHAR(16)')
        _ensure_column(cur, 'players_profile', 'referred_by', 'referred_by VARCHAR(16)')
        _ensure_index(cur, 'players_profile', 'uniq_player_referral',
                      'UNIQUE KEY uniq_player_referral (referral_code)')

        # Developer profile picture (shown top-left) + referrals + points.
        _ensure_column(cur, 'developer_profile', 'logo_path', 'logo_path VARCHAR(500)')
        _ensure_column(cur, 'developer_profile', 'referral_code', 'referral_code VARCHAR(16)')
        _ensure_column(cur, 'developer_profile', 'points', 'points INT NOT NULL DEFAULT 0')
        _ensure_index(cur, 'developer_profile', 'uniq_dev_referral',
                      'UNIQUE KEY uniq_dev_referral (referral_code)')

        # One row per successful referral. referred_ref is the waitlist email or
        # the new player's steam id, and it's UNIQUE so a referrer can never be
        # paid twice for the same friend.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referralID    INT AUTO_INCREMENT PRIMARY KEY,
                code          VARCHAR(16) NOT NULL,
                referrer_kind VARCHAR(10) NOT NULL,
                referrer_id   INT NOT NULL,
                referred_kind VARCHAR(10) NOT NULL,
                referred_ref  VARCHAR(255) NOT NULL,
                points        INT NOT NULL DEFAULT 0,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_referred (referred_kind, referred_ref)
            )
        """)

        # One vote per player per matchup, enforced by the database (a resubmitted
        # vote form used to award points again and inflate the score counters).
        # Existing duplicates are collapsed to the earliest vote first.
        cur.execute("""
            DELETE l FROM matchup_log l
            JOIN (SELECT playerID, matchupID, MIN(logID) keep
                  FROM matchup_log GROUP BY playerID, matchupID HAVING COUNT(*) > 1) d
              ON d.playerID <=> l.playerID AND d.matchupID <=> l.matchupID
            WHERE l.logID > d.keep
        """)
        _ensure_index(cur, 'matchup_log', 'uniq_player_matchup',
                      'UNIQUE KEY uniq_player_matchup (playerID, matchupID)')

        # Indexes for the hot lookups (feed pool, dashboards, live feed).
        _ensure_index(cur, 'media', 'idx_media_app', 'KEY idx_media_app (appID)')
        _ensure_index(cur, 'media', 'idx_media_category', 'KEY idx_media_category (categoryID)')
        _ensure_index(cur, 'media', 'idx_media_developer', 'KEY idx_media_developer (developerID)')
        _ensure_index(cur, 'matchup_log', 'idx_log_player', 'KEY idx_log_player (playerID)')
        _ensure_index(cur, 'matchup_log', 'idx_log_matchup', 'KEY idx_log_matchup (matchupID)')
        _ensure_index(cur, 'matchups', 'idx_pair_pic2', 'KEY idx_pair_pic2 (pic2MediaID)')
        _ensure_index(cur, 'game_profile', 'idx_game_upcoming',
                      'KEY idx_game_upcoming (comingSoonFlag, releaseDateFlag)')
    conn.close()
    ensure_waitlist_table()


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


def _ensure_index(cur, table, index, ddl):
    """Add an index if it isn't there (MySQL has no ADD INDEX IF NOT EXISTS)."""
    cur.execute(
        """SELECT COUNT(*) AS c FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s""",
        (DB_NAME, table, index),
    )
    if cur.fetchone()['c'] == 0:
        cur.execute(f"ALTER TABLE {table} ADD {ddl}")


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
def get_developer_media_kpis(developer_id, appid=None):
    """Per-uploaded-image stats for the developer dashboard. Pass `appid` for
    one game's images only (the dashboard now lives on each game's page)."""
    sql = """SELECT m.mediaID, m.appID, m.path,
                    g.game_name, c.name AS category_name,
                    COALESCE(s.numberTimesShown, 0) AS shown,
                    COALESCE(s.numberUpVotes, 0)    AS upvotes,
                    COALESCE(s.winrate, 0)          AS winrate
             FROM media m
             LEFT JOIN game_profile g ON g.game_appid = m.appID
             LEFT JOIN image_categories c ON c.imageCategoryID = m.categoryID
             LEFT JOIN media_score_log s ON s.mediaID = m.mediaID
             WHERE m.developerID = %s"""
    args = [developer_id]
    if appid is not None:
        sql += " AND m.appID = %s"
        args.append(appid)
    sql += " ORDER BY shown DESC, winrate DESC"
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    conn.close()
    return rows


def set_developer_logo(developer_id, path):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE developer_profile SET logo_path=%s WHERE developer_id=%s",
                    (path, developer_id))
    conn.close()


def get_seen_media_for_player(player_id):
    """Every mediaID this player has already voted on — the feed prefers images
    they have never seen so the same capsule doesn't keep reappearing."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT mu.pic1MediaID a, mu.pic2MediaID b
               FROM matchup_log l JOIN matchups mu ON mu.matchUpID = l.matchupID
               WHERE l.playerID = %s""",
            (player_id,),
        )
        seen = set()
        for r in cur.fetchall():
            seen.add(r['a'])
            seen.add(r['b'])
    conn.close()
    return seen


def get_all_players():
    """Every player row (admin view), newest first."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""SELECT player_id, steam_id, username, avatar, points, vote_count,
                              email_verified, streak_day, last_reward_date, last_sync,
                              referral_code, referred_by
                       FROM players_profile ORDER BY player_id DESC""")
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
        # Decode both JSON columns, matching get_all_games (callers shouldn't
        # have to know which accessor hands back a raw string).
        row['tag_id'] = _as_list(row['tag_id'])
        row['vector'] = _as_list(row['vector'])
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


def count_games():
    """Number of game_profile rows — used to detect a stale cluster cache."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM game_profile")
        n = cur.fetchone()['n']
    conn.close()
    return n


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
         'm1': r['pic1MediaID'], 'm2': r['pic2MediaID'],
         'winner_side': 1 if r['winnerMediaID'] == r['pic1MediaID'] else 2}
        for r in rows
    ]



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
    """Log one vote. Returns False if this player already voted this matchup
    (the uniq_player_matchup index rejects it), which is how a resubmitted vote
    form is stopped from scoring twice."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO matchup_log (playerID, matchupID, winnerMediaID, playerIDVoted)
                   VALUES (%s, %s, %s, %s)""",
                (player_id, matchup_id, winner_media_id, player_id_voted),
            )
    except pymysql.err.IntegrityError:
        return False
    finally:
        conn.close()
    return True


def get_seen_pairs_for_player(player_id):
    """Every (pic1MediaID, pic2MediaID) pair this player has already voted on,
    as a set — one query, so candidate filtering happens in memory instead of a
    round trip per candidate pair."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT mu.pic1MediaID a, mu.pic2MediaID b
               FROM matchup_log l JOIN matchups mu ON mu.matchUpID = l.matchupID
               WHERE l.playerID = %s""",
            (player_id,),
        )
        pairs = {(r['a'], r['b']) for r in cur.fetchall()}
    conn.close()
    return pairs


def add_matchup_to_player(player_id, matchup_id):
    """Append a matchUpID to the player's seen list. Returns True if it was
    newly added, False if the player had already seen it."""
    conn = get_connection()
    added = False
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
            added = True
    conn.close()
    return added


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


# --- Playtime sync cache -----------------------------------------------------
SYNC_INTERVAL_DAYS = 3   # a player's tag playtime is refreshed at most this often


def mark_player_synced(steam_id):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE players_profile SET last_sync=NOW() WHERE steam_id=%s", (steam_id,))
    conn.close()


def sync_is_due(player, interval_days=SYNC_INTERVAL_DAYS):
    """True if this player has never been synced or the cache has expired."""
    last = player.get('last_sync')
    if not last:
        return True
    return (datetime.datetime.now() - last).days >= interval_days


# --- Daily login rewards -----------------------------------------------------
DAILY_REWARD_DAYS = 10           # streak length before it starts over
DAILY_REWARD_STEP = 10           # day N pays N * STEP points


def daily_reward_points(day):
    return day * DAILY_REWARD_STEP


def daily_reward_state(player):
    """What the login popup should show: the day about to be claimed, whether
    it's already claimed today, and how far the streak currently runs.

    A missed day resets the streak, so a player who logs in on days 1-3 and
    then skips a day is back to day 1.
    """
    today = datetime.date.today()
    last = player.get('last_reward_date')
    streak = player.get('streak_day') or 0

    if last == today:
        return {'day': streak, 'claimed_today': True, 'claimed_days': streak,
                'points': daily_reward_points(streak)}
    if last == today - datetime.timedelta(days=1) and 0 < streak < DAILY_REWARD_DAYS:
        day = streak + 1                      # streak continues
    else:
        day = 1                               # first visit, gap, or past day 10
    return {'day': day, 'claimed_today': False, 'claimed_days': day - 1,
            'points': daily_reward_points(day)}


def claim_daily_reward(player_id):
    """Award today's streak bonus. Returns (day, points) or (day, 0) if the
    player already claimed today."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""SELECT streak_day, last_reward_date FROM players_profile
                       WHERE player_id=%s""", (player_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return 0, 0
        state = daily_reward_state(row)
        if state['claimed_today']:
            conn.close()
            return state['day'], 0
        day, points = state['day'], state['points']
        cur.execute("""UPDATE players_profile
                       SET streak_day=%s, last_reward_date=CURDATE(), points=points+%s
                       WHERE player_id=%s""", (day, points, player_id))
    conn.close()
    return day, points


# --- Referrals ---------------------------------------------------------------
REFERRAL_BONUS = 100     # points the referrer earns per friend who signs up
_REFERRAL_TABLE = {'player': ('players_profile', 'player_id'),
                   'developer': ('developer_profile', 'developer_id')}


def _new_referral_code():
    # Uppercase, no lookalike characters — these get typed by hand.
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(8))


def get_or_create_referral_code(kind, row_id):
    """The account's own share code, minted on first use. Unique across both
    account types because a code has to identify exactly one beneficiary."""
    table, pk = _REFERRAL_TABLE[kind]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT referral_code FROM {table} WHERE {pk}=%s", (row_id,))
            row = cur.fetchone()
            if row and row['referral_code']:
                return row['referral_code']
            for _ in range(10):               # retry on the unique-index collision
                code = _new_referral_code()
                if resolve_referral_code(code):
                    continue
                try:
                    cur.execute(f"UPDATE {table} SET referral_code=%s WHERE {pk}=%s",
                                (code, row_id))
                    return code
                except pymysql.err.IntegrityError:
                    continue
    finally:
        conn.close()
    return None


def resolve_referral_code(code):
    """Who does this code belong to? -> {'kind', 'id', 'name'} or None."""
    if not code:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT player_id, username FROM players_profile
                           WHERE referral_code=%s""", (code,))
            row = cur.fetchone()
            if row:
                return {'kind': 'player', 'id': row['player_id'],
                        'name': row['username'] or 'a player'}
            cur.execute("""SELECT developer_id, developer_name FROM developer_profile
                           WHERE referral_code=%s""", (code,))
            row = cur.fetchone()
            if row:
                return {'kind': 'developer', 'id': row['developer_id'],
                        'name': row['developer_name'] or 'a developer'}
    finally:
        conn.close()
    return None


def credit_referral(code, referred_kind, referred_ref):
    """Pay the owner of `code` for bringing in `referred_ref` (a waitlist email
    or a new steam id). Returns (referrer, points) or (None, 0).

    Ignores self-referral and, thanks to UNIQUE(referred_kind, referred_ref),
    pays at most once per referred person.
    """
    owner = resolve_referral_code(code)
    if not owner:
        return None, 0
    if owner['kind'] == referred_kind and str(owner['id']) == str(referred_ref):
        return None, 0
    table, pk = _REFERRAL_TABLE[owner['kind']]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("""INSERT INTO referrals
                                 (code, referrer_kind, referrer_id, referred_kind,
                                  referred_ref, points)
                               VALUES (%s, %s, %s, %s, %s, %s)""",
                            (code, owner['kind'], owner['id'], referred_kind,
                             str(referred_ref), REFERRAL_BONUS))
            except pymysql.err.IntegrityError:
                return None, 0                # this friend was already credited
            cur.execute(f"UPDATE {table} SET points=points+%s WHERE {pk}=%s",
                        (REFERRAL_BONUS, owner['id']))
    finally:
        conn.close()
    return owner, REFERRAL_BONUS


def set_player_referred_by(player_id, code):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE players_profile SET referred_by=%s WHERE player_id=%s",
                    (code, player_id))
    conn.close()


def get_referral_stats(kind, row_id):
    """How many friends this account has brought in, and the points earned."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) n, COALESCE(SUM(points), 0) pts FROM referrals
                       WHERE referrer_kind=%s AND referrer_id=%s""", (kind, row_id))
        row = cur.fetchone()
    conn.close()
    return {'count': row['n'], 'points': int(row['pts'])}


# --- Waitlist ----------------------------------------------------------------
# --- Waitlist ----------------------------------------------------------------
# waitlist(waitlistID, emailID UNIQUE, type 'player'|'developer', timestamp UTC)
#
# Lives in Postgres when PG_HOST is set (the production server keeps its data
# there) and in this MySQL database otherwise. Only the waitlist goes through
# this switch so far; everything else in db.py is still MySQL-only.
WAITLIST_TYPES = ('player', 'developer')


def _waitlist_connection():
    """(connection, identifier quote char) for whichever database holds the waitlist."""
    if os.environ.get('PG_HOST'):
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(
            host=os.environ['PG_HOST'], port=int(os.environ.get('PG_PORT', 5432)),
            dbname=os.environ.get('PG_DB', DB_NAME), user=os.environ.get('PG_USER'),
            password=os.environ.get('PG_PASSWORD'), connect_timeout=5,
            autocommit=True, row_factory=dict_row)
        return conn, '"'
    return get_connection(), '`'


def _is_unique_violation(exc):
    if isinstance(exc, pymysql.err.IntegrityError):
        return exc.args and exc.args[0] == 1062        # ER_DUP_ENTRY
    return getattr(exc, 'sqlstate', None) == '23505'  # Postgres unique_violation


def _waitlist_unique_keys(cur, pg):
    """{name: set of columns} for the waitlist's UNIQUE keys (primary key excluded)."""
    if pg:
        cur.execute("""SELECT c.conname AS name, a.attname AS col
                       FROM pg_constraint c
                       JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                       WHERE c.conrelid = '"waitlist"'::regclass AND c.contype = 'u'""")
    else:
        cur.execute("""SELECT INDEX_NAME AS name, COLUMN_NAME AS col FROM information_schema.STATISTICS
                       WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'waitlist'
                         AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY'""", (DB_NAME,))
    keys = {}
    for r in cur.fetchall():
        keys.setdefault(r['name'], set()).add(r['col'])
    return keys


def ensure_waitlist_table():
    """Create the waitlist table, or bring an older one up to date in place.

    - The first version was waitlist(email, role, referral_code, created_at).
      Its columns are renamed rather than the table rebuilt, so no row is lost;
      referral_code goes (referrals are recorded in the referrals table anyway).
    - One email may join once per type (player AND developer), so the unique
      key is (emailID, type); an older UNIQUE on emailID alone is dropped.
    """
    conn, q = _waitlist_connection()
    pg = q == '"'
    with conn.cursor() as cur:
        cur.execute("SELECT column_name AS c FROM information_schema.columns "
                    "WHERE table_name = 'waitlist' AND table_schema = %s",
                    ('public' if pg else DB_NAME,))
        cols = {r['c'] for r in cur.fetchall()}
        if 'email' in cols:
            for old, new in (('email', 'emailID'), ('role', 'type'), ('created_at', 'timestamp')):
                cur.execute(f"ALTER TABLE {q}waitlist{q} RENAME COLUMN {q}{old}{q} TO {q}{new}{q}")
            cur.execute(f"ALTER TABLE {q}waitlist{q} DROP COLUMN {q}referral_code{q}")
        id_col = ('"waitlistID" integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY' if pg
                  else '`waitlistID` INT AUTO_INCREMENT PRIMARY KEY')
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {q}waitlist{q} (
                {id_col},
                {q}emailID{q}   VARCHAR(255) NOT NULL,
                {q}type{q}      VARCHAR(10)  NOT NULL,
                {q}timestamp{q} {'timestamp' if pg else 'DATETIME'} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT {q}uniq_waitlist_email_type{q} UNIQUE ({q}emailID{q}, {q}type{q})
            )
        """)
        keys = _waitlist_unique_keys(cur, pg)
        for name, key_cols in keys.items():
            if key_cols == {'emailID'}:
                drop = 'CONSTRAINT' if pg else 'INDEX'
                cur.execute(f"ALTER TABLE {q}waitlist{q} DROP {drop} {q}{name}{q}")
        if {'emailID', 'type'} not in keys.values():
            cur.execute(f"ALTER TABLE {q}waitlist{q} ADD CONSTRAINT {q}uniq_waitlist_email_type{q} "
                        f"UNIQUE ({q}emailID{q}, {q}type{q})")
    conn.close()


def normalize_email(email):
    return (email or '').strip().lower()


def add_to_waitlist(email, waitlist_type):
    """Add an email to the waitlist as a player or developer.

    Returns 'joined', or 'exists' if that email is already on it as that type
    (the same email may join once as a player and once as a developer). Emails
    are stored lowercased, so Bob@x.com and bob@x.com are the same entry. The
    UNIQUE (emailID, type) key backs up the check if two requests race.
    """
    email = normalize_email(email)
    if waitlist_type not in WAITLIST_TYPES:
        raise ValueError(f'waitlist type must be one of {WAITLIST_TYPES}')
    conn, q = _waitlist_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {q}waitlist{q} WHERE {q}emailID{q} = %s AND {q}type{q} = %s",
                        (email, waitlist_type))
            if cur.fetchone():
                return 'exists'
            cur.execute(
                f"INSERT INTO {q}waitlist{q} ({q}emailID{q}, {q}type{q}, {q}timestamp{q}) "
                "VALUES (%s, %s, %s)",
                (email, waitlist_type,
                 datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)))
    except Exception as exc:
        if _is_unique_violation(exc):
            return 'exists'
        raise
    finally:
        conn.close()
    return 'joined'


def get_waitlist(limit=200):
    conn, q = _waitlist_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT {q}emailID{q}, {q}type{q}, {q}timestamp{q} FROM {q}waitlist{q} "
                    f"ORDER BY {q}waitlistID{q} DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return rows


def waitlist_count():
    conn, q = _waitlist_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {q}waitlist{q}")
        n = cur.fetchone()['n']
    conn.close()
    return n


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
