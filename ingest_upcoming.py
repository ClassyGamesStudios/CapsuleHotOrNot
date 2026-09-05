"""Ingest upcoming ("coming soon") Steam games so the feed has content.

For each coming-soon appid: scrape tags (store page), build vector, download the
main capsule, flag comingSoonFlag=1, and record media. Then recompute the
cluster cache so the new games are clustered and feed-eligible.

Waits for store.steampowered.com to be reachable first (it may be temporarily
rate-limited), and scrapes gently (low concurrency) to avoid re-tripping it.

Run: python ingest_upcoming.py
"""
import os
import re
import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
import app as A

STORE = 'https://store.steampowered.com'
FASTLY = 'https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{}/capsule_616x353.jpg'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
           'Accept-Language': 'en'}
COOKIES = {'birthtime': '0', 'mature_content': '1'}
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
TARGET = 300          # how many upcoming games to ingest
MAX_WORKERS = 5       # gentle, to avoid re-tripping the store block


def wait_for_store(max_wait=3600):
    start = time.time()
    while time.time() - start < max_wait:
        try:
            if requests.get(STORE + '/app/10/', headers=HEADERS, timeout=15).status_code == 200:
                return True
        except requests.RequestException:
            pass
        print(f'  store still blocked, waiting... ({time.time()-start:.0f}s)', flush=True)
        time.sleep(120)
    return False


def get_coming_soon_appids(limit):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.update(COOKIES)
    ids, seen = [], set()
    for start in range(0, limit + 100, 100):
        url = (f'{STORE}/search/results/?query&start={start}&count=100'
               f'&sort_by=Released_DESC&filter=comingsoon&infinite=1')
        try:
            html = s.get(url, timeout=20).json().get('results_html', '')
        except (requests.RequestException, ValueError):
            break
        page = re.findall(r'data-ds-appid="([\d,]+)"', html)
        if not page:
            break
        for raw in page:
            appid = int(raw.split(',')[0])   # bundles give comma lists; take first
            if appid not in seen:
                seen.add(appid)
                ids.append(appid)
        time.sleep(1)
    return ids[:limit]


def main():
    t0 = time.time()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    print('waiting for store to be reachable...', flush=True)
    if not wait_for_store():
        print('store still blocked after max wait — aborting', flush=True)
        return

    appids = get_coming_soon_appids(TARGET)
    print(f'coming-soon appids fetched: {len(appids)}', flush=True)

    main_id = A._main_category_id()
    pk_map = db.get_tag_pk_map()
    dim = db.get_tag_dimension()
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute('SELECT appID FROM media WHERE categoryID=%s', (main_id,))
        have_media = {r['appID'] for r in cur.fetchall()}
    conn.close()

    counts = {'ok': 0, 'notags': 0, 'failed': 0}
    n = 0

    def work(appid):
        data = A.fetch_game_data(appid)          # store page: name, tag_ids, capsule
        if not data:
            return appid, 'failed'
        if not data['tag_ids']:
            return appid, 'notags'
        db.insert_game(appid, data['name'], data['tag_ids'], data['capsule_image'])
        db.set_game_vector(appid, A.build_game_vector(data['tag_ids'], pk_map, dim))
        db.set_game_release(appid, True, None)   # from coming-soon list
        if appid not in have_media:
            img = None
            try:
                r = requests.get(FASTLY.format(appid), headers=HEADERS, timeout=20)
                if r.status_code == 200 and len(r.content) > 1000:
                    img = r.content
                elif data['capsule_image']:
                    r2 = requests.get(data['capsule_image'], headers=HEADERS, timeout=20)
                    if r2.status_code == 200 and len(r2.content) > 1000:
                        img = r2.content
            except requests.RequestException:
                pass
            if img:
                fname = f'main_{appid}.jpg'
                with open(os.path.join(UPLOAD_DIR, fname), 'wb') as f:
                    f.write(img)
                db.insert_media(appid, main_id, f'static/uploads/{fname}', None)
        return appid, 'ok'

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(work, a) for a in appids]
        for fut in as_completed(futures):
            _, status = fut.result()
            counts[status] += 1
            n += 1
            if n % 50 == 0:
                print(f'  {n}/{len(appids)}  ok={counts["ok"]} notags={counts["notags"]} '
                      f'failed={counts["failed"]}  {time.time()-t0:.0f}s', flush=True)

    print(f'ingest done: ok={counts["ok"]} notags={counts["notags"]} failed={counts["failed"]}',
          flush=True)

    print('recomputing cluster cache...', flush=True)
    data = A.compute_vector_scatter()
    with open(A.VECTORS_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    print(f'DONE ingested_ok={counts["ok"]} total_games={data["count"]} '
          f'k={data["best_k"]} elapsed={time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
