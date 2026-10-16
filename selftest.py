"""Backend self-test. Run with: python selftest.py

Builds a THROWAWAY database (chon_selftest), exercises the real code paths
against it, and drops it again — game_profiles_db is never touched. Covers
schema creation from scratch, developer auth, the feed's genre matching, the
vote flow's counters/points, the one-vote-per-matchup guard, and delete cascade.
"""
import os
import sys
import tempfile

os.environ['DB_NAME'] = 'chon_selftest'
os.environ['PG_HOST'] = ''    # keep the waitlist in the scratch MySQL db, even if .env names a Postgres

import db                                     # noqa: E402  (must follow env var)
import app as A                               # noqa: E402
from werkzeug.security import generate_password_hash   # noqa: E402

assert db.DB_NAME == 'chon_selftest', 'refusing to run against ' + db.DB_NAME

# Cluster the scratch games into a scratch cache file — never the real one.
A.VECTORS_CACHE_FILE = os.path.join(tempfile.gettempdir(), 'chon_selftest_vectors.json')

ok, fail = [], []


def check(label, cond, detail=''):
    (ok if cond else fail).append(label)
    print(('PASS  ' if cond else 'FAIL  ') + label + (f'  [{detail}]' if detail else ''))


def drop_scratch():
    conn = db.get_connection(with_db=False)
    with conn.cursor() as cur:
        cur.execute('DROP DATABASE IF EXISTS chon_selftest')
    conn.close()


drop_scratch()

# --- schema from scratch -----------------------------------------------------
db.init_db()
tags = db.seed_tags()
cats = db.seed_image_categories()
check('init_db builds the schema on an empty server', True)
check('seed_tags populates tags', tags > 0, f'{tags} tags')
check('seed_image_categories populates categories', cats > 0, f'{cats} categories')
check('tag dimension matches tag count', db.get_tag_dimension() == tags)
db.init_db()   # must be idempotent (indexes, columns, dedupe migration)
check('init_db is idempotent', True)

# --- developer auth ----------------------------------------------------------
pw = generate_password_hash('pw', method='pbkdf2:sha256')
did, err = db.create_developer('Test Studio', 'a@b.com', pw, 'tok')
check('create_developer succeeds', did is not None and err is None, str(err))
check('duplicate email rejected', db.create_developer('X', 'a@b.com', pw, 't2')[1] is not None)
check('verify_developer_token works', db.verify_developer_token('tok') is True)

# --- games, vectors, media ---------------------------------------------------
# Fixture: 4 genre groups x 10 games. Each game carries its group's 8 shared
# tags (8/40 = 25% document frequency, under UBIQUITOUS_DF so the genre signal
# survives the drop) plus 1 tag unique to the game. Genre tags have to outweigh
# the unique one, which is why there are 8 of them: tf-idf gives a df=1 tag a
# high weight, so a fixture with mostly-unique tags would cluster by noise.
pk_map, dim = db.get_tag_pk_map(), db.get_tag_dimension()
steam_tags = sorted(pk_map)
GROUPS, PER_GROUP, SHARED = 4, 10, 8
plan = []                                  # [(appid, group, tag_ids)]
for g in range(GROUPS):
    shared = steam_tags[g * SHARED:(g + 1) * SHARED]
    for i in range(PER_GROUP):
        appid = 900000 + g * 100 + i
        unique = [steam_tags[100 + g * PER_GROUP + i]]
        plan.append((appid, g, shared + unique))

for appid, _, tag_ids in plan:
    db.insert_game(appid, f'Test Game {appid}', tag_ids, None)
    db.set_game_vector(appid, A.build_game_vector(tag_ids, pk_map, dim))
    db.set_game_release(appid, coming_soon=1, release_date=None)

first_appid, _, first_tags = plan[0]
game = db.get_game(first_appid)
check('game vector is binary presence of the right length',
      len(game['vector']) == dim and sum(game['vector']) == len(first_tags),
      f"len={len(game['vector'])} ones={sum(game['vector'])}")
check('vector decodes back to the same tags',
      {i + 1 for i, v in enumerate(game['vector']) if v} == {pk_map[s] for s in first_tags})

main_cat = A._main_category_id()
dev_appid = first_appid
mids = {appid: db.insert_media(appid, main_cat, f'static/uploads/t{appid}.jpg',
                               did if appid == dev_appid else None)
        for appid, _, _ in plan}
check('_main_category_id resolves', main_cat is not None)
check('eligible pool = upcoming games only',
      len(db.get_eligible_media_in_category(main_cat)) == len(plan))
released = plan[-1][0]
db.set_game_release(released, coming_soon=0, release_date='2000-01-01')
check('released games drop out of the eligible pool',
      len(db.get_eligible_media_in_category(main_cat)) == len(plan) - 1)
db.set_game_release(released, coming_soon=1, release_date=None)

# --- clustering + genre-matched feed ----------------------------------------
A.K_MIN = A.K_MAX = GROUPS                  # pin k to the fixture's genre count
A.K_STEP = 1
A.save_cluster_cache(A.compute_vector_scatter())
cluster_of, neighbours = A.cluster_index()
group_of = {appid: g for appid, g, _ in plan}
check('every test game landed in a cluster', all(a in cluster_of for a in group_of))
# each genre group should be one cluster: as many distinct clusters as groups,
# and no cluster spanning two groups
per_group = {g: {cluster_of[a] for a, gg, _ in plan if gg == g} for g in range(GROUPS)}
check('each genre group collapses to a single cluster',
      all(len(v) == 1 for v in per_group.values()), str(per_group))
check('different genres get different clusters',
      len({next(iter(v)) for v in per_group.values()}) == GROUPS, str(per_group))
check('cluster neighbours are ranked for every cluster',
      all(len(neighbours[c]) == len(set(cluster_of.values())) - 1 for c in neighbours))

db.insert_player('76500000000000001', 'tester', None, None)
player = db.get_player_by_steam_id('76500000000000001')
check('signup bonus granted once', player['points'] == db.SIGNUP_BONUS)

matchups = [A.next_matchup_for_player(player, main_cat) for _ in range(10)]
m = matchups[0]
check('feed returns a matchup', all(x is not None for x in matchups))
check('feed pairs two different games',
      all(x['media1']['appID'] != x['media2']['appID'] for x in matchups))
check('phase 1 features the developer upload',
      all(did in (x['media1']['developerID'], x['media2']['developerID']) for x in matchups))
check('every matchup is genre-matched (same cluster)',
      all(cluster_of.get(x['media1']['appID']) == cluster_of.get(x['media2']['appID'])
          for x in matchups),
      str([(x['media1']['appID'], x['media2']['appID']) for x in matchups[:3]]))
check('matchup carries its genre label',
      all(x['category'].get('name') for x in matchups), str(m['category']))

# --- vote flow ---------------------------------------------------------------
A.app.config['TESTING'] = True
client = A.app.test_client()
with client.session_transaction() as s:
    s['steam_id'] = '76500000000000001'

win_id, lose_id = mids[plan[0][0]], mids[plan[1][0]]    # two games, same genre group
vote = {'media1': win_id, 'media2': lose_id, 'winner': win_id}
check('vote POST redirects', client.post('/player/vote', data=vote).status_code == 302)


def score(media_id):
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute('SELECT * FROM media_score_log WHERE mediaID=%s', (media_id,))
        row = cur.fetchone()
    conn.close()
    return row


def counts():
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n FROM matchup_log')
        logs = cur.fetchone()['n']
        cur.execute('SELECT COUNT(*) n FROM matchups')
        pairs = cur.fetchone()['n']
    conn.close()
    return pairs, logs


w, l = score(win_id), score(lose_id)
check('winner scored shown=1 upvotes=1 winrate=100',
      (w['numberTimesShown'], w['numberUpVotes'], round(w['winrate'])) == (1, 1, 100), str(w))
check('loser scored shown=1 upvotes=0 winrate=0',
      (l['numberTimesShown'], l['numberUpVotes'], round(l['winrate'])) == (1, 0, 0), str(l))
check('one matchup row + one log row', counts() == (1, 1), str(counts()))

p1 = db.get_player_by_steam_id('76500000000000001')
check('vote awarded 10 points at 1.0x', p1['points'] == db.SIGNUP_BONUS + 10, str(p1['points']))
check('vote_count incremented', p1['vote_count'] == 1)
check('matchup marked seen', len(p1['matchupIDs']) == 1)

# --- replayed vote must not score or pay twice ------------------------------
client.post('/player/vote', data=vote)
client.post('/player/vote', data=vote)
p2 = db.get_player_by_steam_id('76500000000000001')
check('replayed vote awards no extra points', p2['points'] == p1['points'],
      f"{p1['points']} -> {p2['points']}")
check('replayed vote does not inflate shown count',
      score(win_id)['numberTimesShown'] == 1, str(score(win_id)['numberTimesShown']))
check('replayed vote does not duplicate log rows', counts()[1] == 1, str(counts()))
check('replayed vote does not bump vote_count', p2['vote_count'] == 1)
check('a voted pair is never offered again',
      (min(win_id, lose_id), max(win_id, lose_id))
      in db.get_seen_pairs_for_player(p2['player_id']))

# --- email bonus -------------------------------------------------------------
check('email bonus granted once only',
      (db.verify_player_email(p2['player_id']),
       db.verify_player_email(p2['player_id'])) == (db.EMAIL_BONUS, 0))

# --- delete ------------------------------------------------------------------
db.add_mediaid_to_developer(did, win_id)
db.delete_media(win_id)
conn = db.get_connection()
with conn.cursor() as cur:
    cur.execute('SELECT COUNT(*) n FROM media WHERE mediaID=%s', (win_id,))
    gone = cur.fetchone()['n'] == 0
    cur.execute('SELECT COUNT(*) n FROM media_score_log WHERE mediaID=%s', (win_id,))
    no_score = cur.fetchone()['n'] == 0
conn.close()
db.remove_mediaid_from_developer(did, win_id)
check('delete_media removes the row and its score', gone and no_score)
check('developer mediaIDs no longer lists it',
      win_id not in db.get_developer_by_id(did)['mediaIDs'])

# --- waitlist ----------------------------------------------------------------
# Old shape (email, role, referral_code, created_at) is renamed in place, rows kept.
conn = db.get_connection()
with conn.cursor() as cur:
    cur.execute('DROP TABLE waitlist')
    cur.execute("""CREATE TABLE waitlist (waitlistID INT AUTO_INCREMENT PRIMARY KEY,
                   email VARCHAR(255) NOT NULL UNIQUE, role VARCHAR(20),
                   referral_code VARCHAR(16), created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    cur.execute("INSERT INTO waitlist (email, role) VALUES ('old@x.com', 'developer')")
conn.close()
db.ensure_waitlist_table()
db.ensure_waitlist_table()   # idempotent
rows = db.get_waitlist()
check('old waitlist migrated to emailID/type/timestamp, row kept',
      [(r['emailID'], r['type']) for r in rows] == [('old@x.com', 'developer')], str(rows))

check('new email joins', db.add_to_waitlist('New@Example.com ', 'player') == 'joined')
check('same email + same type is reported as already on it',
      db.add_to_waitlist('new@example.com', 'player') == 'exists')
check('same email can also join as the other type',
      db.add_to_waitlist('new@example.com', 'developer') == 'joined')
check('email match ignores case and spaces', db.add_to_waitlist(' NEW@EXAMPLE.COM', 'developer') == 'exists')
check('email stored lowercased, once per type',
      sorted(r['type'] for r in db.get_waitlist() if r['emailID'] == 'new@example.com')
      == ['developer', 'player'])
conn = db.get_connection()
try:
    with conn.cursor() as cur:   # bypass the pre-check: the (emailID, type) key must still refuse it
        cur.execute("INSERT INTO waitlist (emailID, type) VALUES ('new@example.com', 'player')")
    check('UNIQUE (emailID, type) rejects a duplicate insert', False)
except Exception as e:
    check('UNIQUE (emailID, type) rejects a duplicate insert', db._is_unique_violation(e))
conn.close()
try:
    db.add_to_waitlist('z@x.com', 'press')
    check('unknown waitlist type rejected', False)
except ValueError:
    check('unknown waitlist type rejected', True)

client = A.app.test_client()
r = client.post('/api/waitlist', json={'email': 'api@x.com', 'type': 'developer'})
check('API: new email -> 201 joined', r.status_code == 201 and r.json['status'] == 'joined', r.data)
r = client.post('/api/waitlist', json={'email': 'API@x.com', 'type': 'developer'})
check('API: repeat email + type -> 409 with already-joined message',
      r.status_code == 409 and r.json['message'] == A.WAITLIST_EXISTS_MSG.format('developer'), r.data)
r = client.post('/api/waitlist', json={'email': 'api@x.com', 'type': 'player'})
check('API: same email as the other type -> 201', r.status_code == 201, r.data)
check('API: bad email -> 400',
      client.post('/api/waitlist', json={'email': 'nope', 'type': 'player'}).status_code == 400)
check('API: bad type -> 400',
      client.post('/api/waitlist', json={'email': 'ok@x.com', 'type': 'press'}).status_code == 400)
before = db.waitlist_count()
r = client.post('/api/waitlist', json={'email': 'bot@x.com', 'type': 'player', 'website': 'spam'})
check('API: honeypot looks successful but stores nothing',
      r.status_code == 201 and db.waitlist_count() == before)
check('waitlist_count matches rows', db.waitlist_count() == 5, str(db.waitlist_count()))

print(f'\n{len(ok)} passed, {len(fail)} failed')
for f in fail:
    print('  FAILED:', f)
drop_scratch()
if os.path.exists(A.VECTORS_CACHE_FILE):
    os.remove(A.VECTORS_CACHE_FILE)
print('scratch database dropped')
sys.exit(1 if fail else 0)
