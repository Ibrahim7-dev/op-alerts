# P-Bandai One Piece Card Game — restock/new-item monitor

Watches the [P-Bandai US One Piece Card Game page](https://p-bandai.com/us/brand/onepiececardgame/)
and sends a **push notification to your phone** whenever a new item is listed.
Runs itself for free on GitHub Actions — no laptop, no open browser tab.

The page loads its products with JavaScript, so this uses a real headless browser
(Playwright) rather than a simple HTTP fetch.

---

## What you need
- A free GitHub account
- The free **ntfy** app on your phone ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))

Total setup time: ~10 minutes.

---

## Step 1 — Pick a secret ntfy topic
A topic is just a name that doubles as your "address." Anyone who knows it can
read your notifications, so make it long and random — treat it like a password.

Example: `pbandai-onepiece-7fk29ax3`

Open the **ntfy app → Subscribe to topic →** type that exact name. Leave the
server as the default `ntfy.sh`.

> Want it private? You can self-host ntfy or use ntfy.sh with auth, but a long
> random topic is enough for most people.

## Step 2 — Create the repo
1. Create a new GitHub repo (private is fine).
2. Upload all the files from this folder (`monitor.py`, `requirements.txt`,
   `seen.json`, and the `.github/` folder). Keep the structure intact.

## Step 3 — Add your topic as a secret
In the repo: **Settings → Secrets and variables → Actions → New repository secret**
- Name: `NTFY_TOPIC`
- Value: your topic from Step 1 (e.g. `pbandai-onepiece-7fk29ax3`)

(Optional second secret `NTFY_SERVER` only if you self-host ntfy.)

## Step 4 — Seed the state (so you don't get 50 alerts at once)
The first run should record everything currently on the page **without**
notifying. Do this once:

**Actions tab → "P-Bandai One Piece monitor" → Run workflow →**
set **seed_only = `true`** → Run.

When it finishes, `seen.json` in the repo will contain the current items.

## Step 5 — Test that notifications work
Run the workflow again manually, this time with **seed_only = `false`**.
If nothing new is on the page you won't get a push (that's correct). To force a
test, you can temporarily delete a line from `seen.json`, commit, and run — that
"new" item will trigger a push to your phone. Then re-seed.

## Done
From now on it checks roughly every 5 minutes on its own. When a new One Piece
item appears, your phone buzzes; tapping the notification opens the product page.

---

## Good to know / honest limits
- **Timing:** GitHub's scheduled runs are *best-effort* and frequently run late
  when their infrastructure is busy — expect "every 5–15 min," not exact. Fine
  for preorders that stay up a while; **not** reliable for instant flash-drop
  sniping. For sub-minute checks, run `monitor.py` on an always-on host instead
  (a cheap VPS, a Raspberry Pi, or a service like Render/Railway with a cron).
- **Site changes:** if P-Bandai restructures their page, the scraper may stop
  finding items. The script logs a warning and the run shows as failed in the
  Actions tab (it won't silently lie that there's nothing new). If that happens,
  the selector in `monitor.py` (`a[href*='/us/item/']`) is what to adjust.
- **It detects *listings*, not stock changes.** It alerts when a new item URL
  appears on the page. A previously-listed item going from sold-out to in-stock
  isn't caught unless its URL was absent before.
- **Be polite:** every-5-min is light traffic. Don't crank the cron to every
  minute across many parallel jobs — that's just hammering their site.

## Switching to Pushover instead of ntfy
Replace the `notify()` body in `monitor.py` with a POST to
`https://api.pushover.net/1/messages.json` using `token`, `user`, `message`,
`title`, and `url` fields, and set those as secrets. Everything else is identical.

## Files
| File | Purpose |
|------|---------|
| `monitor.py` | Scrapes the page, diffs against `seen.json`, sends pushes |
| `seen.json` | Persisted list of items already seen (auto-updated each run) |
| `.github/workflows/monitor.yml` | The schedule + run + commit-state workflow |
| `requirements.txt` | Python deps (Playwright) |
