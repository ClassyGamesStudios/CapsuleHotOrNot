"""Backfill game_profile.comingSoonFlag / releaseDateFlag from Steam appdetails.

appdetails can't batch, so one request per game (concurrency 8 is tolerated).
Run: python populate_release.py
"""
import time
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import db

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
URL = 'https://store.steampowered.com/api/appdetails?appids={}&filters=release_date&cc=us&l=en'
DATE_FORMATS = ('%b %d, %Y', '%d %b, %Y', '%B %d, %Y', '%d %B, %Y',
                '%b %Y', '%B %Y', '%Y')


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def fetch_release(session, appid):
    """Return (coming_soon: bool, release_date: date|None) or None on failure."""
    for attempt in range(2):
        try:
            r = session.get(URL.format(appid), headers=HEADERS, timeout=15)
            if r.status_code == 200:
                j = r.json()
                if j is None:                       # rate-limited
                    time.sleep(1.5)
                    continue
                data = j.get(str(appid), {})
                if not data.get('success'):
                    return (False, None)            # no store entry -> treat as released/unknown
                rd = data.get('data', {}).get('release_date', {})
                return (bool(rd.get('coming_soon')), parse_date(rd.get('date')))
        except requests.RequestException:
            time.sleep(1.0)
    return None


def main():
    t0 = time.time()
    appids = [g['game_appid'] for g in db.get_all_games()]
    print(f'games to backfill: {len(appids)}', flush=True)

    results = []       # (appid, coming_soon, date)
    counts = {'coming_soon': 0, 'future': 0, 'released': 0, 'failed': 0}
    n = 0

    def work(appid):
        session = requests.Session()
        return appid, fetch_release(session, appid)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(work, a) for a in appids]
        for fut in as_completed(futures):
            appid, res = fut.result()
            n += 1
            if res is None:
                counts['failed'] += 1
            else:
                cs, d = res
                results.append((appid, cs, d))
                if cs:
                    counts['coming_soon'] += 1
                elif d and d > datetime.now().date():
                    counts['future'] += 1
                else:
                    counts['released'] += 1
            if n % 200 == 0:
                print(f'  {n}/{len(appids)}  coming_soon={counts["coming_soon"]} '
                      f'future={counts["future"]} released={counts["released"]} '
                      f'failed={counts["failed"]}  {time.time()-t0:.0f}s', flush=True)

    conn = db.get_connection()
    with conn.cursor() as cur:
        for appid, cs, d in results:
            cur.execute(
                "UPDATE game_profile SET comingSoonFlag=%s, releaseDateFlag=%s WHERE game_appid=%s",
                (1 if cs else 0, d, appid),
            )
    conn.close()

    eligible = counts['coming_soon'] + counts['future']
    print(f'DONE updated={len(results)} coming_soon={counts["coming_soon"]} '
          f'future={counts["future"]} released={counts["released"]} failed={counts["failed"]} '
          f'feed_eligible={eligible} elapsed={time.time()-t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
