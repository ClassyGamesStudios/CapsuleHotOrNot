# CapsuleHotOrNot — frontend handover

Players vote head-to-head on the capsule art of **upcoming** Steam games and earn
points; developers upload capsule variants and see which one wins. Two portals
plus an internal admin view, all in one Flask app.

**15 page screens + 1 shared partial, 33 routes.** Everything below was
generated from the live route table, not from notes.

---

## 1. What you are working with

- **Flask + Jinja2 server-rendered HTML.** No React/Vue, no bundler, no npm, no
  build step. Edit a `.html` file, reload the page.
- **No stylesheet.** `templates/base.html` holds the page shell, 6 CSS custom
  properties and the flash-message area; **every other template carries its own
  inline `<style>` block**, and several have inline `<script>`. There is a lot of
  duplicated CSS across templates as a result — extracting a real design system
  into `static/css/` is the single biggest win available here, and nothing in the
  backend depends on the current markup.
- **No external dependencies at all** except Plotly (CDN) on the internal
  `/dev/vectors` page. No icon set — emoji are used as icons throughout.
- Vanilla JS only, all inline: the landing-page match reel and stat counters, the
  gift-card carousel, the waitlist/login modal, the daily-reward popup, the
  multiplier level-up animation, and the tab switchers.

### The whole design system today (`base.html`)

```css
--primary: #00b4d8   /* buttons, links, active states */
--accent:  #22d3ee   /* numbers, emphasis */
--bg:      #0f172a   /* page background */
--card:    #1e2937   /* panels */
--text:    #e2e8f0
--muted:   #94a3b8   /* secondary text */
```

Dark theme, `Segoe UI` system stack, 1100px max-width container, `.btn` and
`.link` classes, `body { overflow-x: hidden }` (needed by the full-bleed landing
sections). Jinja blocks: `{% block title %}` and `{% block content %}`.

### Conventions to preserve

- **Flash messages**: `base.html` renders `get_flashed_messages()`. Most POST
  actions redirect back with a flash — keep that container.
- **Media URLs**: uploaded images are stored as project-relative paths in the DB
  (`static/uploads/main_4410830.jpg`) and rendered as `/{{ path }}`. Capsules are
  **616×353** (16:9-ish); the wall and feed both assume that ratio.
- **Folder-driven art** — drop a file in and it appears, no code change:
  `static/dev_logos/` (developer wall), `static/gift_cards/` (join carousel,
  caption derived from the filename), `static/avatars/` (testimonials).
- **All current artwork is placeholder SVG**, including the logo
  (`static/logo_placeholder.svg`). Replacing it is expected.
- **Do not rename form fields or routes** — they are the API contract. See §4.

---

## 2. Screens

Auth column: what the server requires; unauthorised users are redirected, not
shown an error.

### Public / marketing

| URL | Template | Purpose & UI notes |
|---|---|---|
| `/` | `home.html` (302 lines) | Landing page. Top bar (placeholder logo, Join Waitlist + Log In), animated **live match reel** (two capsules, VS, winner gets a green outline — no label), pitch copy, wide white CTA, boosted stat counters, **capsule wall** mosaic of upcoming games, **developer logo wall**. Both CTAs open one modal offering Player / Developer. |
| `/player/join` | `player_join.html` (216) | Player waitlist pitch. Gradient hero: left = auto-advancing **gift-card carousel** (arrows + dots), right = "JOIN THE OTHER N PLAYERS" + role dropdown + email + **Join Waitlist**. Below: 6 testimonial cards with avatars. |
| `/developer/join` | `developer_join.html` (126) | Developer waitlist pitch + logo wall. Same form pattern. |
| `/developer/login` | `developer_login.html` (30) | Email + password. Links to the waitlist (sign-up is invite-only). |
| `/developer/signup` | `developer_signup.html` (31) | **Unlinked back door** for creating test accounts. Not in any nav. |
| `/admin/login` | `admin_login.html` (27) | Single password field. |
| `/r/<code>` | — | Referral link. Records the referrer in the session, redirects to the matching join page (shows an "Invited by X" chip). |
| `/developer/verify-email/<token>` | — | Email-verification landing. Flashes a result and redirects; **no emails are actually sent yet**, so this has no UI of its own. |
| `/player/test-login/<steam_id>` | — | Local-only login bypass for testing (404s when `CHON_ENV=production`). |

### Player portal (requires Steam session)

| URL | Template | Purpose & UI notes |
|---|---|---|
| `/player` | `player.html` (176) | Profile: avatar, **points balance**, votes, login-streak counter, big "Play the capsule game" CTA, **invite link** with copy button, verify-email and cash-in buttons. Deliberately *no* playtime tables, *no* genre breakdown, *no* multiplier — those moved to admin or the game. |
| `/player/feed` | `feed.html` (134) | **The core game.** Two capsules side by side + VS; clicking one submits the vote. HUD shows points and the **multiplier badge**; crossing a tier plays a one-shot animation (value rises, scales, fades over 2.8s) plus a badge bump. |
| `/player/rewards` | `rewards.html` (67) | Points balance, how-you-earn list, gift-card catalogue. **Display only — redemption is not live.** |
| *(partial)* | `_daily_reward.html` (93) | **Daily login popup**, included by both `/player` and `/player/feed`. Grid of day 1→10; earned days green, today's box pulsing, claim button. Fires once per login session; `/player` also shows a persistent claim row as a fallback. |

### Developer portal (requires developer session)

| URL | Template | Purpose & UI notes |
|---|---|---|
| `/profile` | `profile.html` (167) | Studio **logo as a profile picture** top-left (click to upload), game card grid, **dotted "+" add-game tile** inside the grid (not a toolbar button), referral block. |
| `/profile/add` | `add_profile.html` (31) | Single Steam App ID field; the server scrapes name/tags/capsule. |
| `/game/<appid>` | `game_detail.html` (208) | Per-game page: capsule, tag chips with lion's-share weight badges, upload form (category dropdown is temporarily limited to `main`), uploaded-media grid with delete, and the **per-game dashboard** (summary tiles + KPI table: matchups, wins, losses, win rate, percentile). |

### Admin (internal, password-gated)

| URL | Template | Purpose & UI notes |
|---|---|---|
| `/admin`, `/admin/players` | `admin_players.html` (76) | Every player: avatar, points, votes, multiplier, streak, referred-by, own code, last sync. Plus the waitlist table. |
| `/admin/player/<steam_id>` | `admin_player.html` (130) | One player: stat tiles, **best-fit genre clusters** with similarity bars, playtime tabs (all-time / 2-week / multiplier), force re-sync. |
| `/dev/vectors` | `vectors.html` (76) | Internal diagnostic: Plotly t-SNE scatter of ~3,500 games coloured by genre cluster, staleness banner. Low design priority. |

---

## 3. Data each screen receives

Use these names in Jinja; nothing else is in scope.

| Template | Context variables |
|---|---|
| `home.html` | `stats` (games/players/matches/developers), `recent` (reel: `p1,n1,m1,p2,n2,m2,winner_side`), `wall` (`appid,path,name`), `dev_logos` |
| `player_join.html` | `players`, `cards` (`src,label`), `testimonials` (`name,avatar,meta,text`), `roles`, `referrer` |
| `developer_join.html` | `developers`, `logos`, `roles`, `referrer` |
| `player.html` | `player`, `vote_mult`, `reward`, `popup_reward`, `reward_days`, `reward_step`, `referral_code`, `referral_stats`, `referral_bonus` |
| `feed.html` | `player`, `matchup` (`category`, `media1`, `media2` each with `mediaID,path,game_name`), `vote_mult`, `votes_to_next`, `mult_up`, `reward`, `reward_days` |
| `rewards.html` | `player`, `balance`, `multiplier`, `rewards` (`name,cost,icon`) |
| `profile.html` | `developer`, `games`, `referral_code`, `referral_stats`, `referral_bonus` |
| `game_detail.html` | `game`, `tags` (with `weight`), `categories`, `media`, `kpis`, `summary`, `developer` |
| `admin_players.html` | `players`, `waitlist`, `waitlist_total` |
| `admin_player.html` | `player`, `ranking`, `tables`, `multiplier`, `vote_mult`, `reward` |
| `add_profile.html`, `admin_login.html`, `developer_login.html`, `developer_signup.html`, `vectors.html` | none (forms / `**data`) |

---

## 4. Action endpoints — field names are a contract

Redesign the markup freely, but keep these method/path/field combinations.

| Method | Path | Fields | Result |
|---|---|---|---|
| POST | `/waitlist` | `email`, `role` (`player`\|`developer`\|`press`), `next` | Adds to waitlist, credits referrer, redirects to `next` |
| POST | `/player/vote` | `media1`, `media2`, `winner` (must equal one of them) | Scores both, awards points, back to feed |
| POST | `/player/claim-daily` | — | Claims the streak bonus |
| POST | `/player/verify-email` | — | Grants the one-time email bonus (stub) |
| POST | `/game/<appid>/upload` | `category_id`, `image` (multipart) | Uploads a capsule |
| POST | `/developer/logo` | `logo` (multipart) | Sets the studio profile picture |
| POST | `/media/<id>/delete` | — | Deletes one image |
| POST | `/game/<appid>/delete` | — | Unlinks a game from the developer |
| POST | `/profile/add` | `appid` | Scrapes and adds a game |
| POST | `/developer/login` | `email`, `password` | Session login |
| POST | `/developer/signup` | `name`, `email`, `password` | Creates an account |
| POST | `/admin/login` | `password` | Admin session |
| POST | `/admin/player/<steam_id>/sync` | — | Forces a playtime re-sync |
| GET | `/player/login` → `/player/callback` | — | Steam OpenID round trip |
| GET | `/player/logout`, `/developer/logout`, `/admin/logout` | — | Clears the session |

---

## 5. Known rough edges worth fixing

1. **CSS duplication** — the same card/button/table styles are re-declared in a
   dozen templates. Extract to `static/css/` and delete the inline blocks.
2. **Responsive** is minimal: a couple of `@media (max-width: 860px)` rules on
   the join pages. The landing reel, capsule wall, KPI tables and admin tables
   are not mobile-tuned. The player feed is the screen that most needs to work
   on a phone.
3. **Emoji as icons** everywhere (🔥 🎮 📊 🎁). An icon set would tidy this up.
4. **Placeholder assets** — real logo, real gift-card art, real testimonial
   avatars and real testimonial copy all still to come.
5. **Accessibility** — the modal and popup have partial ARIA, focus is not
   trapped, contrast on `--muted` text over `--card` is borderline.
6. The landing stat counters are **deliberately inflated** for marketing
   (`STAT_BOOST` in `app.py`), so don't expect them to match the admin numbers.

---

## 6. Running it locally

**Windows (simplest):** MySQL 8 running, then `python app.py` → http://127.0.0.1:5000

**Ubuntu / WSL (production-like):** see [../deploy/README.md](../deploy/README.md) →
`bash deploy/run-local.sh` → http://localhost:8080

Test accounts (local only):

- **Player** — `/player/test-login/76561198036019909` logs in as MrJamieMcC with
  points, a vote history and a live login streak. (This route 404s in production.)
- **Developer** — `dev@example.com` / `secret123`, owns 4 games with uploaded
  capsules and real KPI data.
- **Admin** — password is `ADMIN_PASSWORD` in `.env` (or `admin` by default on a
  bare Windows dev setup).

The database ships with ~3,868 games and ~3,600 real capsule images, so every
screen has realistic content — no need to mock anything.
