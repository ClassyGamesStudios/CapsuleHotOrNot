"""One-off: download the main capsule (capsule_616x353) for every game and
record it in the media table under the 'main' category.

Source: https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/<appid>/capsule_616x353.jpg
Falls back to the game's stored capsule_url (hashed) when the flat URL 404s.
Files go to static/uploads/main_<appid>.jpg. Skips games already recorded.

Run: python collect_capsules.py
"""
import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import db

MAIN_CATEGORY_NAME = 'main'
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
FASTLY = 'https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{}/capsule_616x353.jpg'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
MAX_WORKERS = 16


def fetch_bytes(session, url):
    try:
        r = session.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except requests.RequestException:
        pass
    return None


def main():
    t0 = time.time()
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    cats = {c['name']: c['imageCategoryID'] for c in db.get_image_categories()}
    category_id = cats[MAIN_CATEGORY_NAME]

    games = db.get_all_games()  # game_appid, game_name, tag_id, vector
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute('SELECT appID FROM media WHERE categoryID=%s', (category_id,))
        done = {r['appID'] for r in cur.fetchall()}
        # need capsule_url fallback per game
        cur.execute('SELECT game_appid, capsule_url FROM game_profile')
        capsule_urls = {r['game_appid']: r['capsule_url'] for r in cur.fetchall()}
    conn.close()

    todo = [g['game_appid'] for g in games if g['game_appid'] not in done]
    print(f'games={len(games)} already_done={len(done)} to_fetch={len(todo)}', flush=True)

    results = []      # (appID, categoryID, path, developerID)
    counts = {'fastly': 0, 'fallback': 0, 'missing': 0}
    n = 0

    def work(appid):
        session = requests.Session()
        data = fetch_bytes(session, FASTLY.format(appid))
        src = 'fastly'
        if data is None:
            url = capsule_urls.get(appid)
            if url:
                data = fetch_bytes(session, url)
                src = 'fallback'
        if data is None:
            return appid, None, 'missing'
        fname = f'main_{appid}.jpg'
        with open(os.path.join(UPLOAD_DIR, fname), 'wb') as f:
            f.write(data)
        return appid, f'static/uploads/{fname}', src

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(work, a) for a in todo]
        for fut in as_completed(futures):
            appid, path, src = fut.result()
            counts[src] += 1
            if path:
                results.append((appid, category_id, path, None))
            n += 1
            if n % 200 == 0:
                print(f'  {n}/{len(todo)}  fastly={counts["fastly"]} '
                      f'fallback={counts["fallback"]} missing={counts["missing"]} '
                      f'{time.time()-t0:.0f}s', flush=True)

    conn = db.get_connection()
    with conn.cursor() as cur:
        for i in range(0, len(results), 500):
            cur.executemany(
                'INSERT INTO media (appID, categoryID, path, developerID) VALUES (%s,%s,%s,%s)',
                results[i:i + 500],
            )
        cur.execute('SELECT COUNT(*) n FROM media WHERE categoryID=%s', (category_id,))
        total = cur.fetchone()['n']
    conn.close()

    print(f'DONE inserted={len(results)} fastly={counts["fastly"]} '
          f'fallback={counts["fallback"]} missing={counts["missing"]} '
          f'total_main_media={total} elapsed={time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
