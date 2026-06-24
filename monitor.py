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
LISTING_URL = os.environ.get(
    "PBANDAI_URL",
    "https://p-bandai.com/us/search?keyword=ONE%20PIECE%20CARD%20GAME",
)
# Optional second URL scraped in the same run and merged (e.g. the brand page).
# Leave blank to skip. Lets you cover both a search and the brand landing page.
LISTING_URL_2 = os.environ.get("PBANDAI_URL_2", "https://p-bandai.com/us/brand/onepiececardgame/")

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
# -----------------------------------------------------------------------------


def load_seen() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    STATE_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))


def _scrape_one(page, url: str) -> dict:
    """Scrape a single listing URL, returning {item_url: title}."""
    found: dict[str, str] = {}
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    # Dismiss the cookie banner if present — it can overlay/await interaction.
    for label in ("Accept All Cookies (Only available to users aged 16 and over)",
                  "Accept All Cookies", "Accept", "ACCEPT"):
        try:
            btn = page.get_by_text(label, exact=False)
            if btn and btn.count() > 0:
                btn.first.click(timeout=2000)
                break
        except Exception:
            pass

    # Wait for product links to render. Item pages live at /us/item/N...
    try:
        page.wait_for_selector("a[href*='/us/item/']", timeout=30_000)
    except Exception:
        print(f"WARNING: no item links found at {url}", file=sys.stderr)
        print(page.content()[:1500], file=sys.stderr)
        return found

    # Scroll to bottom to trigger any lazy-loaded items, then settle.
    try:
        for _ in range(6):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(600)
    except Exception:
        pass
    page.wait_for_timeout(2000)

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
                for k, v in part.items():
                    if k not in items or len(v) > len(items[k]):
                        items[k] = v
            except Exception as e:  # noqa: BLE001
                print(f"ERROR scraping {url}: {e}", file=sys.stderr)
        browser.close()
    return items


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

    seen = load_seen()
    current = scrape_items()

    if not current:
        print("No items scraped; leaving state untouched.")
        return 1

    new_urls = [u for u in current if u not in seen]

    if SEED_ONLY or not seen:
        save_seen(current)
        print(f"Seeded {len(current)} items (no notifications sent).")
        return 0

    if not new_urls:
        print(f"No new items. ({len(current)} on page)")
        save_seen({**seen, **current})
        return 0

    print(f"Found {len(new_urls)} new item(s):")
    for url in new_urls:
        title = current[url]
        print(f"  + {title} -> {url}")
        notify(
            title="New One Piece item on P-Bandai!",
            message=title,
            url=url,
        )
        time.sleep(1)

    save_seen({**seen, **current})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
