#!/usr/bin/env python3
"""
P-Bandai One Piece Card Game monitor.

Loads the One Piece Card Game search listing with a headless browser (the page
renders its product grid via JavaScript, so a plain HTTP fetch returns nothing),
extracts every item INCLUDING upcoming / not-yet-open preorders, diffs them
against the last-seen set, and pushes a notification via ntfy for anything new.

State is stored in seen.json so consecutive runs only alert on genuinely new items.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---- Config (override via environment / GitHub secrets) ---------------------
# We scrape the SEARCH listing rather than the curated brand landing page,
# because the brand page only shows a featured subset and omits upcoming items.
# This URL is the One Piece Card Game results. If P-Bandai changes their
# category codes, update PBANDAI_URL (or override it via the env var) — any
# search/brand URL that lists items will work with the scraper below.
# PRIMARY: the brand landing page is confirmed working and shows ONLY One Piece
# Card Game items (its "latest pre-order" / "closing soon" rails surface upcoming
# items too). This is the reliable backbone of the monitor.
LISTING_URL = os.environ.get(
    "PBANDAI_URL",
    "https://p-bandai.com/us/brand/onepiececardgame/",
)
# SECONDARY (optional): the BANDAI CARD SHOP search page, which lists card-game
# products and is filtered by our title check to One Piece only. Scraped in the
# same run and merged. The previously-used `?keyword=...` URL was NOT valid —
# P-Bandai redirected it to a "PAGE NOT AVAILABLE" error page with no items — so
# it has been removed. If you find a better coded URL in your browser (e.g. a
# `?character=` or `?shop=` filter that shows exactly the items you want), set it
# as the PBANDAI_URL_2 repo secret/variable. Leave blank to scrape only the brand page.
LISTING_URL_2 = os.environ.get(
    "PBANDAI_URL_2",
    "https://p-bandai.com/us/search?shop=05-005",
)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")          # e.g. "pbandai-onepiece-x7k2"
# NOTE: an unset GitHub secret is passed through as an EMPTY string, not absent,
# so os.environ.get(..., default) won't apply its default. Coerce empty -> default,
# and tolerate a server given without a scheme (e.g. "ntfy.sh").
NTFY_SERVER = os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh"
if not NTFY_SERVER.startswith(("http://", "https://")):
    NTFY_SERVER = "https://" + NTFY_SERVER
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
# First run with an empty state file would otherwise alert on EVERYTHING.
# When SEED_ONLY=1 we just record the current items and send nothing.
SEED_ONLY = os.environ.get("SEED_ONLY", "0") == "1"
# TEST_PING=1 sends one test notification and exits, so you can confirm ntfy
# delivery without hand-editing seen.json.
TEST_PING = os.environ.get("TEST_PING", "0") == "1"

# The search/brand listings pull in loosely-related items: Digimon "CARD GAME"
# sets, One Piece figures (S.H.Figuarts / Portrait.Of.Pirates), even Gundam kits
# from "recommended" rails. Every genuine One Piece Card Game product has this
# exact phrase in its title; nothing else does. So we require it (case-insensitive).
# Override via env if you ever want a different franchise/line.
TITLE_MUST_CONTAIN = os.environ.get("TITLE_MUST_CONTAIN", "ONE PIECE CARD GAME").upper()
# Sanity floor: if a scrape returns fewer than this many matching items, we treat
# the run as a failed/partial scrape rather than trusting it — this prevents a
# search-page hiccup (which leaves only the ~3 brand-page items) from corrupting
# the baseline on seed, or causing a flood of false "new item" alerts later when
# the full catalog reappears. Tune via env if the real catalog is ever smaller.
MIN_EXPECTED_ITEMS = int(os.environ.get("MIN_EXPECTED_ITEMS", "5"))
# -----------------------------------------------------------------------------


def matches_filter(title: str) -> bool:
    """True if the item title contains the required phrase (case-insensitive)."""
    return TITLE_MUST_CONTAIN in title.upper()


# Known status phrases as they appear in titles, longest/most-specific first so
# "OUT OF STOCK" is matched before a bare "STOCK" could ever interfere.
STATUS_PHRASES = [
    "OUT OF STOCK",
    "SOLD OUT",
    "PRE-ORDER CLOSED",
    "PRE-ORDER",
    "PREORDER",
    "IN STOCK",
    "COMING SOON",
    "AVAILABLE",
]


def parse_status(title: str) -> str:
    """Extract a normalized sales status from an item title.

    Returns the matched phrase in upper case, or 'UNKNOWN' if none is present.
    """
    t = title.upper()
    for phrase in STATUS_PHRASES:
        if phrase in t:
            # Normalize the two preorder spellings to one label.
            if phrase == "PREORDER":
                return "PRE-ORDER"
            return phrase
    return "UNKNOWN"


def load_seen() -> dict:
    """Load state as {url: {"title": str, "status": str}}.

    Backward compatible with the old {url: title} format so an existing
    seen.json keeps working without a manual reset.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    normalized: dict = {}
    for url, val in raw.items():
        if isinstance(val, dict):
            normalized[url] = {
                "title": val.get("title", ""),
                "status": val.get("status") or parse_status(val.get("title", "")),
            }
        else:  # old format: val is the title string
            normalized[url] = {"title": val, "status": parse_status(val)}
    return normalized


def save_seen(seen: dict) -> None:
    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))


def to_records(items: dict) -> dict:
    """Turn {url: title} from a scrape into {url: {title, status}}."""
    return {u: {"title": t, "status": parse_status(t)} for u, t in items.items()}


def _scrape_one(page, url: str, attempts: int = 2) -> dict:
    """Scrape a single listing URL, returning {item_url: title}.

    Retries once on failure, since the search page intermittently renders slowly
    and a single 30s miss would otherwise drop the whole catalog for that run.
    """
    found: dict[str, str] = {}
    for attempt in range(1, attempts + 1):
        found = {}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            # Dismiss the cookie banner if present — it can overlay the grid.
            for label in (
                "Accept All Cookies (Only available to users aged 16 and over)",
                "Accept All Cookies", "Accept", "ACCEPT",
            ):
                try:
                    btn = page.get_by_text(label, exact=False)
                    if btn and btn.count() > 0:
                        btn.first.click(timeout=2000)
                        break
                except Exception:
                    pass

            # Wait for product links to render. Item pages live at /us/item/N...
            try:
                page.wait_for_selector("a[href*='/us/item/']", timeout=25_000)
            except Exception:
                # As a fallback, let the network settle in case items render late.
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

            # Scroll to trigger lazy-loaded items, then settle.
            try:
                for _ in range(6):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(500)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            for a in page.query_selector_all("a[href*='/us/item/']"):
                href = a.get_attribute("href") or ""
                if "/us/item/" not in href:
                    continue
                if href.startswith("/"):
                    href = "https://p-bandai.com" + href
                href = href.split("?")[0].rstrip("/")
                title = (a.get_attribute("aria-label") or a.inner_text() or "").strip()
                title = " ".join(title.split())
                if not title:
                    title = href.rsplit("/", 1)[-1]
                if href not in found or len(title) > len(found[href]):
                    found[href] = title

            if found:
                return found
            # No items this attempt — log and (maybe) retry.
            print(f"WARNING: no item links found at {url} "
                  f"(attempt {attempt}/{attempts})", file=sys.stderr)
            if attempt == attempts:
                print(page.content()[:1200], file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR on {url} (attempt {attempt}/{attempts}): {e}",
                  file=sys.stderr)
        if attempt < attempts:
            page.wait_for_timeout(3000)  # brief backoff before retry
    return found


def scrape_items() -> dict:
    """Scrape all configured listing URLs and merge results."""
    items: dict[str, str] = {}
    urls = [u for u in (LISTING_URL, LISTING_URL_2) if u]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = ctx.new_page()
        for url in urls:
            try:
                part = _scrape_one(page, url)
                kept_here = sum(1 for t in part.values() if matches_filter(t))
                print(f"  scraped {len(part)} item(s) from {url} "
                      f"({kept_here} match the filter)")
                for k, v in part.items():
                    if k not in items or len(v) > len(items[k]):
                        items[k] = v
            except Exception as e:  # noqa: BLE001
                print(f"ERROR scraping {url}: {e}", file=sys.stderr)
        browser.close()

    # Filter to genuine One Piece Card Game items. We do this AFTER merging so an
    # item with a blank title on one page but a full title on another is judged
    # on its best-available title rather than wrongly dropped.
    filtered = {u: t for u, t in items.items() if matches_filter(t)}
    dropped = len(items) - len(filtered)
    if dropped:
        print(f"Filtered out {dropped} non-matching item(s); kept {len(filtered)}.")
    return filtered


def notify(title: str, message: str, url: str) -> None:
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC set] would notify: {title} -> {url}")
        return
    try:
        endpoint = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
        req = urllib.request.Request(
            endpoint,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Tags": "package",
                "Click": url,
                "Priority": "high",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001 - never let a notify failure kill the run
        print(f"ntfy POST failed: {e}", file=sys.stderr)


def main() -> int:
    if TEST_PING:
        notify(
            title="P-Bandai monitor test",
            message="If you see this on your phone, notifications work.",
            url="https://p-bandai.com/us/brand/onepiececardgame/",
        )
        print("Sent test ping.")
        return 0

    seen = load_seen()                      # {url: {title, status}}
    current = to_records(scrape_items())    # {url: {title, status}}

    if not current:
        print("No items scraped; leaving state untouched.")
        return 1

    # Guard against a partial scrape (e.g. search page failed, only the brand
    # page's few featured items came back). Trusting it would corrupt the seed
    # baseline or trigger false alerts later. We make an exception only when the
    # prior state was itself this small, so a genuinely tiny catalog isn't stuck.
    if len(current) < MIN_EXPECTED_ITEMS and len(current) < len(seen):
        print(
            f"Only {len(current)} item(s) scraped (< {MIN_EXPECTED_ITEMS} and "
            f"fewer than the {len(seen)} already known) — treating as a partial "
            f"scrape. State preserved, no notifications.",
            file=sys.stderr,
        )
        return 1

    if SEED_ONLY or not seen:
        if len(current) < MIN_EXPECTED_ITEMS:
            # Warn but proceed: a genuinely small result on seed day shouldn't
            # block setup. The shrinkage guard above is what protects ongoing
            # runs from a partial-scrape flood; seeding a small set is recoverable
            # (just re-seed later). Glance at the count to sanity-check it.
            print(
                f"NOTE: seeding only {len(current)} item(s) (below the "
                f"{MIN_EXPECTED_ITEMS} soft floor). If you expected more, the "
                f"secondary search URL may have failed this run — check the "
                f"per-URL counts above and re-seed if needed.",
                file=sys.stderr,
            )
        save_seen(current)
        print(f"Seeded {len(current)} items (no notifications sent).")
        return 0

    new_urls = [u for u in current if u not in seen]
    # Status changes on items we've already seen.
    changed = [
        u for u in current
        if u in seen and current[u]["status"] != seen[u]["status"]
    ]

    if not new_urls and not changed:
        print(f"No changes. ({len(current)} items on page)")
        save_seen({**seen, **current})
        return 0

    if new_urls:
        print(f"Found {len(new_urls)} new item(s):")
        for url in new_urls:
            rec = current[url]
            print(f"  + [{rec['status']}] {rec['title']} -> {url}")
            notify(
                title="New One Piece card item!",
                message=rec["title"],
                url=url,
            )
            time.sleep(1)

    if changed:
        print(f"Found {len(changed)} status change(s):")
        for url in changed:
            old = seen[url]["status"]
            new = current[url]["status"]
            rec = current[url]
            print(f"  ~ {old} -> {new}: {rec['title']} -> {url}")
            # Strip the trailing status off the title for a cleaner message,
            # then state the transition explicitly.
            base = rec["title"]
            notify(
                title=f"Status change: {old} -> {new}",
                message=f"{base}",
                url=url,
            )
            time.sleep(1)

    save_seen({**seen, **current})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
