"""One-off bulk populate: pull an account's owned games, scrape tags for each
new game, build its binary vector, and store it. Concurrent + bulk insert.

Run: python bulk_populate.py
Progress is printed to stdout (every 200 games).
"""
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
import app as A

STEAM_ID = '76561198034957967'


def main():
    t0 = time.time()
    key = A.STEAM_API_KEY
    url = (f'https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/'
           f'?key={key}&steamid={STEAM_ID}&include_appinfo=1')
    owned = requests.get(url, timeout=30).json().get('response', {}).get('games', [])

    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute('SELECT game_appid FROM game_profile')
        existing = {r['game_appid'] for r in cur.fetchall()}
    conn.close()

    todo = [(g['appid'], g.get('name') or f"App {g['appid']}")
            for g in owned if g['appid'] not in existing]
    print(f'owned={len(owned)} existing={len(existing)} to_scrape={len(todo)}', flush=True)

    pk_map = db.get_tag_pk_map()
    dim = db.get_tag_dimension()

    results = []          # (appid, name, tag_ids, capsule, vector)
    done = 0
    with_tags = 0

    def work(item):
        appid, name = item
        data = A.fetch_game_data(appid)
        if not data:
            return (appid, name, [], None)
        return (appid, data['name'] or name, data['tag_ids'], data['capsule_image'])

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(work, it) for it in todo]
        for fut in as_completed(futures):
            appid, name, tag_ids, capsule = fut.result()
            vec = A.build_game_vector(tag_ids, pk_map, dim)
            if tag_ids:
                with_tags += 1
            results.append((appid, name, json.dumps(tag_ids), capsule, json.dumps(vec)))
            done += 1
            if done % 200 == 0:
                print(f'  scraped {done}/{len(todo)}  (with tags: {with_tags})  '
                      f'{time.time()-t0:.0f}s', flush=True)

    # Bulk insert in batches.
    conn = db.get_connection()
    with conn.cursor() as cur:
        for i in range(0, len(results), 500):
            cur.executemany(
                """INSERT INTO game_profile (game_appid, game_name, tag_id, capsule_url, vector)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                     game_name=VALUES(game_name), tag_id=VALUES(tag_id),
                     capsule_url=VALUES(capsule_url), vector=VALUES(vector)""",
                results[i:i + 500],
            )
    conn.close()

    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n FROM game_profile')
        total = cur.fetchone()['n']
    conn.close()
    print(f'DONE inserted={len(results)} with_tags={with_tags} '
          f'total_games={total} elapsed={time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
