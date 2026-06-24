#!/usr/bin/env python3
"""
P-Bandai One Piece Card Game monitor.

Loads the brand listing page with a headless browser (the page renders its
product grid via JavaScript, so a plain HTTP fetch returns nothing), extracts
the current items, diffs them against the last-seen set, and pushes a notification
via ntfy for anything new.

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
LISTING_URL = os.environ.get(
    "PBANDAI_URL",
    "https://p-bandai.com/us/brand/onepiececardgame/",
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")          # e.g. "pbandai-onepiece-x7k2"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
# First run with an empty state file would otherwise alert on EVERYTHING.
# When SEED_ONLY=1 we just record the current items and send nothing.
SEED_ONLY = os.environ.get("SEED_ONLY", "0") == "1"
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


def scrape_items() -> dict:
    """Return {item_url: item_title} for every product currently on the page."""
    items: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        ).new_page()

        page.goto(LISTING_URL, wait_until="domcontentloaded", timeout=60_000)

        # Wait for product links to appear. P-Bandai item pages live at /us/item/N...
        # We poll for those anchors rather than a brittle CSS class that may change.
        try:
            page.wait_for_selector("a[href*='/us/item/']", timeout=30_000)
        except Exception:
            # No items found (page layout changed, blocked, or genuinely empty).
            # Dump some HTML to logs so a failed run is debuggable.
            print("WARNING: no item links found.", file=sys.stderr)
            print(page.content()[:2000], file=sys.stderr)
            browser.close()
            return items

        # Let the grid finish populating.
        page.wait_for_timeout(2500)

        anchors = page.query_selector_all("a[href*='/us/item/']")
        for a in anchors:
            href = a.get_attribute("href") or ""
            if "/us/item/" not in href:
                continue
            # Normalize to an absolute, query-stripped URL so the same item
            # doesn't appear under multiple keys.
            if href.startswith("/"):
                href = "https://p-bandai.com" + href
            href = href.split("?")[0].rstrip("/")
            title = (a.get_attribute("aria-label") or a.inner_text() or "").strip()
            title = " ".join(title.split())  # collapse whitespace
            if not title:
                title = href.rsplit("/", 1)[-1]
            # Keep the longest title we've seen for a URL (image links are often blank).
            if href not in items or len(title) > len(items[href]):
                items[href] = title

        browser.close()
    return items


def notify(title: str, message: str, url: str) -> None:
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC set] would notify: {title} -> {url}")
        return
    endpoint = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    req = urllib.request.Request(
        endpoint,
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Tags": "package",
            "Click": url,            # tapping the notification opens the item
            "Priority": "high",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001 - never let a notify failure kill the run
        print(f"ntfy POST failed: {e}", file=sys.stderr)


def main() -> int:
    seen = load_seen()
    current = scrape_items()

    if not current:
        # Scrape produced nothing; don't wipe state, just exit non-zero so the
        # run shows up as failed in the Actions log without losing seen.json.
        print("No items scraped; leaving state untouched.")
        return 1

    new_urls = [u for u in current if u not in seen]

    if SEED_ONLY or not seen:
        # First-ever run: record everything, alert on nothing.
        save_seen(current)
        print(f"Seeded {len(current)} items (no notifications sent).")
        return 0

    if not new_urls:
        print(f"No new items. ({len(current)} on page)")
        # Still refresh state so titles stay current.
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
        time.sleep(1)  # space out pushes

    # Merge so we keep history of everything seen.
    save_seen({**seen, **current})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
