# CapsuleHotOrNot

A Flask app with two sides:

- **Developer portal** — add a game profile by Steam App ID; the app pulls the
  game's tags (with their Steam tag ids) and main capsule image straight from
  Steam's store page, computes a lion's-share weight per tag, and lets a
  developer upload media assets per category (with dimension validation).
- **Player portal** — sign in with Steam (OpenID), pull the player's owned games
  and playtime, total playtime per tag (all-time, last 2 weeks, and a recency
  "multiplier" view), and match the player to genre clusters.

Every game gets a 448-dimension **binary tag vector**. Games are clustered
(K-means, cosine, auto-k by silhouette) into named genre categories, visualised
with a t-SNE scatter (`/dev/vectors`). Each player is matched to their best-fit
categories by cosine similarity to the cluster centroids.

## Data source note

SteamDB forbids scraping, so all data is pulled from Steam directly: tags from
the store page's `InitAppTagModal` payload (ids match SteamDB's `/tag/<id>/`),
the `capsule_616x353.jpg` asset for the capsule, and the Steam Web API
(`GetOwnedGames`, `GetRecentlyPlayedGames`, `GetPlayerSummaries`).

## Setup

1. Python deps:
   ```
   pip install -r requirements.txt
   ```
2. MySQL 8 running locally. Connection defaults to `root`/`root` on
   `127.0.0.1:3306`; override with `DB_HOST` / `DB_USER` / `DB_PASSWORD` /
   `DB_NAME` env vars.
3. Steam Web API key — set the `STEAM_API_KEY` env var, or create a
   `secrets_local.py` (gitignored) with:
   ```python
   STEAM_API_KEY = 'your_key_here'
   ```
4. Seed the tags table (from `tags_data.py`) and run:
   ```
   python db.py       # create DB + tables, seed tags + image categories
   python app.py      # http://127.0.0.1:5000
   ```

## Layout

- `app.py` — routes, Steam scraping/lookup, vectors, clustering, player matching
- `db.py` — MySQL schema, migrations, and all queries
- `tags_data.py` — the ~448 Steam tag reference dataset (seeds the `tags` table)
- `templates/` — Jinja templates
- `bulk_populate.py` — one-off: bulk-load an account's owned games + vectors

## Database (`game_profiles_db`)

`developer_profile`, `game_profile`, `tags`, `players_profile`,
`image_categories`, `media`, plus voting-scaffold tables
(`media_score_log`, `matchups`, `matchup_log`).
