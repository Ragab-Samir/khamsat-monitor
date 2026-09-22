#!/usr/bin/env python3
"""
Khamsat Keyword Monitor
========================
Polls the Khamsat "requests" / community section, matches new posts against a
keyword list, and sends an instant Telegram notification for each match.

WHY PLAYWRIGHT INSTEAD OF requests+BeautifulSoup:
Khamsat does not expose a public API or RSS feed for this section. Playwright
renders the page like a real browser, which is more resilient if the site
serves content via JS or adds a soft bot-check later. If you confirm the page
is plain server-rendered HTML with no JS dependency, you can swap the fetch
function for `requests` + BeautifulSoup for a much lighter footprint — see
the `fetch_with_requests()` fallback stub at the bottom.

SETUP
-----
1. pip install playwright python-dotenv
   playwright install chromium

2. Create a Telegram bot:
   - Message @BotFather on Telegram -> /newbot -> copy the token
   - Message your new bot once (anything), then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     and copy the "chat":{"id": ...} value -> that's your CHAT_ID

3. Fill in the CONFIG block below (or use a .env file).

4. IMPORTANT: Inspect the live Khamsat requests page yourself and update
   TARGET_URL and the CSS selectors in `parse_listings()` — markup changes
   over time and this script ships with placeholder selectors you MUST verify.

5. Run manually first: python khamsat_monitor.py
   Then schedule it (cron example at the bottom of this file).
"""

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

# ------------------------------------------------------------------------
# CONFIG — edit these
# ------------------------------------------------------------------------
TARGET_URL = "https://khamsat.com/community/requests"  # VERIFY: exact section URL
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

KEYWORDS = [
    "تحليل بيانات",
    "data analysis",
    "إدخال بيانات",
    "dashboard",
    "power bi",
    "محلل بيانات",
    "تحليل احصائي",
    "تحليل إحصائي",
    "تنظيف بيانات",
    "تصور بيانات",
    "جدول محوري",
    "تحليل مبيعات",
    "تحليل استبيان",
    "excel تحليل",
    "google sheets",
    "sql",
    "tableau",
    "looker studio",
    "data analyst",
    "data cleaning",
    "data visualization",
    "junior data analyst",
    "entry level data analyst",
]

DB_PATH = Path(__file__).parent / "seen_requests.db"
POLL_INTERVAL_SECONDS = 600  # 10 minutes — see reasoning in the write-up
REQUEST_TIMEOUT_MS = 30000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "(KeywordMonitorBot; contact: you@example.com)"
)

# ------------------------------------------------------------------------
# Arabic-aware normalization so keyword matching isn't fooled by
# diacritics or alternate letter forms (e.g. أ/إ/آ vs ا, ى vs ي, ة vs ه)
# ------------------------------------------------------------------------
_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u06D6-\u06DC\u06DF-\u06E8\u06EA-\u06ED]")


def normalize_text(text: str) -> str:
    text = text.strip().lower()
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"\s+", " ", text)
    return text


NORMALIZED_KEYWORDS = [normalize_text(k) for k in KEYWORDS]


def matches_keywords(title: str, body: str = "") -> list[str]:
    """Return the list of keywords that matched, in their original casing."""
    haystack = normalize_text(f"{title} {body}")
    return [
        original
        for original, normalized in zip(KEYWORDS, NORMALIZED_KEYWORDS)
        if normalized in haystack
    ]


# ------------------------------------------------------------------------
# Deduplication store (SQLite) — this is what keeps you from getting
# re-notified about the same request on every poll cycle
# ------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_requests (
            request_id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            matched_keywords TEXT,
            first_seen_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def already_seen(conn, request_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM seen_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return row is not None


def mark_seen(conn, request_id: str, title: str, url: str, matched: list[str]):
    conn.execute(
        "INSERT OR IGNORE INTO seen_requests VALUES (?, ?, ?, ?, ?)",
        (request_id, title, url, json.dumps(matched, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ------------------------------------------------------------------------
# Fetch + parse (Playwright)
# ------------------------------------------------------------------------
async def fetch_page_html(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        await page.goto(url, timeout=REQUEST_TIMEOUT_MS, wait_until="networkidle")
        html = await page.content()
        await browser.close()
        return html


# Debug variant — flip to this call in run_once() if you ever need to watch
# the browser again (e.g. Khamsat changes their layout and 0 listings return):
#
# async def fetch_page_html_debug(url: str) -> str:
#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=False, slow_mo=1000)
#         context = await browser.new_context(user_agent=USER_AGENT)
#         page = await context.new_page()
#         await page.goto(url, timeout=REQUEST_TIMEOUT_MS, wait_until="networkidle")
#         await page.wait_for_timeout(15000)
#         html = await page.content()
#         await browser.close()
#         return html


def parse_listings(html: str) -> list[dict]:
    """
    Confirmed against a real capture of khamsat.com/community/requests on
    2026-09-22. Khamsat renders each request as a <tr class="forum_post">
    row inside <table id="forums_table"> — not a div-based card. The row's
    id is "forum_post-<ID>" and the title+link live in
    <h3 class="details-head"><a href="/community/requests/<ID>-slug">...</a></h3>.

    If Khamsat restyles the page later and this starts returning 0 again,
    re-check the live markup — the site does not have a public API, so this
    selector is the only contract we have with it.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    rows = soup.select("tr.forum_post")

    for row in rows:
        title_el = row.select_one("h3.details-head a")
        if not title_el:
            continue

        href = title_el.get("href", "")
        title = title_el.get_text(strip=True)
        full_url = href if href.startswith("http") else f"https://khamsat.com{href}"

        # Request ID: prefer the row's own id ("forum_post-797558" -> "797558"),
        # fall back to parsing it out of the URL slug if that ever changes.
        row_id = row.get("id", "")
        request_id = row_id.replace("forum_post-", "") if row_id else href.rstrip("/").split("/")[-1].split("-")[0]

        listings.append(
            {"id": request_id, "title": title, "url": full_url, "body": ""}
        )

    return listings


# ------------------------------------------------------------------------
# Notification (Telegram)
# ------------------------------------------------------------------------
async def send_telegram_notification(title: str, url: str, matched: list[str]):
    message = (
        f"🔔 <b>New matching Khamsat request</b>\n\n"
        f"<b>Title:</b> {title}\n"
        f"<b>Matched keywords:</b> {', '.join(matched)}\n"
        f"<b>Link:</b> {url}"
    )
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(api_url, json=payload)
        resp.raise_for_status()


# ------------------------------------------------------------------------
# Main poll cycle
# ------------------------------------------------------------------------
async def run_once():
    conn = init_db()
    try:
        html = await fetch_page_html(TARGET_URL)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc)}] Fetch failed: {e}")
        return

    listings = parse_listings(html)
    print(f"[{datetime.now(timezone.utc)}] Fetched {len(listings)} listings.")

    if len(listings) == 0:
        debug_path = Path(__file__).parent / "debug_page.html"
        debug_path.write_text(html, encoding="utf-8")
        print(f"[{datetime.now(timezone.utc)}] 0 listings — wrote raw page to {debug_path} for inspection.")

    new_matches = 0
    for item in listings:
        if already_seen(conn, item["id"]):
            continue

        matched = matches_keywords(item["title"], item["body"])
        # Always record as seen, whether it matched or not, so we never
        # re-process it — this is what makes polling cheap and idempotent.
        mark_seen(conn, item["id"], item["title"], item["url"], matched)

        if matched:
            new_matches += 1
            try:
                await send_telegram_notification(item["title"], item["url"], matched)
                print(f"  -> Notified: {item['title']} ({matched})")
            except Exception as e:
                print(f"  -> Notification failed for {item['url']}: {e}")

    print(f"[{datetime.now(timezone.utc)}] Done. {new_matches} new matches this cycle.")
    conn.close()


async def run_forever():
    while True:
        await run_once()
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        asyncio.run(run_once())  # good for cron / GitHub Actions
    else:
        asyncio.run(run_forever())  # good for a long-running server/container


# ------------------------------------------------------------------------
# CRON EXAMPLE (self-hosted Linux box), run every 10 minutes:
# */10 * * * * /usr/bin/python3 /path/to/khamsat_monitor.py --once >> /path/to/monitor.log 2>&1
#
# GITHUB ACTIONS ALTERNATIVE (free, no server needed):
# Create .github/workflows/monitor.yml with a `schedule: cron: "*/10 * * * *"`
# trigger that checks out the repo, installs deps, restores seen_requests.db
# from a committed artifact or cache, and runs `python khamsat_monitor.py --once`.
# ------------------------------------------------------------------------
