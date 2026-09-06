"""
NSE + BSE Corporate Announcements -> Telegram Channel bot.

Polls the public announcement feeds of NSE and BSE on a schedule,
formats any new announcements, and posts them to a Telegram channel
using the Telegram Bot API.

Run:
    python main.py

Environment variables (see .env.example):
    TELEGRAM_BOT_TOKEN   - token from @BotFather
    TELEGRAM_CHAT_ID     - your channel id, e.g. -1001234567890
    POLL_INTERVAL_MIN    - how often to check for new announcements (default 5)
"""

import os
import io
import re
import json
import time
import logging
import requests
import pdfplumber
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

import news_feeds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("nse-bse-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "5"))
NEWS_INTERVAL_MIN = int(os.environ.get("NEWS_INTERVAL_MIN", "20"))
AUTHORIZED_USER_IDS = {
    int(x) for x in os.environ.get("AUTHORIZED_USER_IDS", "").split(",") if x.strip().isdigit()
}

SEEN_FILE = "seen_ids.json"
SEEN_NEWS_FILE = "seen_news_ids.json"
UPDATE_OFFSET_FILE = "update_offset.json"
MAX_SEEN_IDS = 5000  # cap file size, oldest ids drop off

NSE_BASE = "https://www.nseindia.com"
NSE_ANNOUNCEMENTS_URL = f"{NSE_BASE}/api/corporate-announcements?index=equities"
BSE_ANNOUNCEMENTS_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- persistence ----------

def load_seen(path=SEEN_FILE):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen, path=SEEN_FILE):
    trimmed = list(seen)[-MAX_SEEN_IDS:]
    with open(path, "w") as f:
        json.dump(trimmed, f)


def load_offset():
    if os.path.exists(UPDATE_OFFSET_FILE):
        try:
            with open(UPDATE_OFFSET_FILE, "r") as f:
                return json.load(f).get("offset", 0)
        except Exception:
            return 0
    return 0


def save_offset(offset):
    with open(UPDATE_OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


# ---------- Telegram ----------

def send_message_to_chat(chat_id, text, _retry=True):
    if not TELEGRAM_BOT_TOKEN:
        log.error("Missing TELEGRAM_BOT_TOKEN env var.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 429 and _retry:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            log.warning("Rate limited by Telegram, waiting %ss before retrying.", retry_after)
            time.sleep(retry_after + 1)
            send_message_to_chat(chat_id, text, _retry=False)
        elif resp.status_code != 200:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram send error: %s", e)


def send_telegram_message(text):
    if not TELEGRAM_CHAT_ID:
        log.error("Missing TELEGRAM_CHAT_ID env var.")
        return
    send_message_to_chat(TELEGRAM_CHAT_ID, text)


# ---------- NSE ----------

def get_nse_session():
    """NSE requires a warmed-up session (cookies) before its API endpoints
    will respond. We GET the homepage first, then reuse the session."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    try:
        session.get(NSE_BASE, timeout=15)
    except Exception as e:
        log.error("NSE session warmup failed: %s", e)
    return session


def fetch_nse_announcements():
    session = get_nse_session()
    try:
        resp = session.get(NSE_ANNOUNCEMENTS_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("NSE fetch failed: %s", e)
        return []

    items = []
    for row in data:
        ann_id = f"nse-{row.get('symbol','')}-{row.get('an_dt','')}-{row.get('desc','')[:40]}"
        items.append({
            "id": ann_id,
            "source": "NSE",
            "company": row.get("sm_name") or row.get("symbol", ""),
            "subject": row.get("desc", "") or row.get("attchmntText", ""),
            "time": row.get("an_dt", ""),
            "link": row.get("attchmntFile", ""),
        })
    return items


# ---------- BSE ----------

def fetch_bse_announcements():
    today = datetime.now().strftime("%Y%m%d")
    params = {
        "pageno": 1,
        "strCat": -1,
        "strPrevDate": today,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": today,
        "strType": "C",
    }
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = "https://www.bseindia.com/corporates/ann.html"
    headers["Origin"] = "https://www.bseindia.com"
    try:
        resp = requests.get(BSE_ANNOUNCEMENTS_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        snippet = ""
        try:
            snippet = resp.text[:200]
        except Exception:
            pass
        log.error("BSE fetch failed: %s (response started with: %r)", e, snippet)
        return []

    items = []
    table = data.get("Table", []) if isinstance(data, dict) else []
    for row in table:
        ann_id = f"bse-{row.get('SCRIP_CD','')}-{row.get('NEWS_DT','')}-{row.get('HEADLINE','')[:40]}"
        items.append({
            "id": ann_id,
            "source": "BSE",
            "company": row.get("SLONGNAME") or row.get("SCRIP_CD", ""),
            "subject": row.get("HEADLINE", "") or row.get("NEWSSUB", ""),
            "time": row.get("NEWS_DT", ""),
            "link": row.get("ATTACHMENTNAME", ""),
        })
    return items


MAX_RAW_SCAN_CHARS = 4000    # how much raw PDF text we read internally, to search for a subject line
MAX_SUMMARY_CHARS = 320      # length of the final human-readable summary posted to the channel

# Many NSE/BSE circulars follow a standard letter format with a "Sub:" or
# "Subject:" line stating exactly what the filing is about — e.g.
# "Sub: Submission of newspaper advertisement ... 29th AGM". Pulling that
# one line out gives a far more readable result than dumping the whole
# letter, and it's the same technique a person skimming the PDF would use.
SUBJECT_RE = re.compile(
    r"\bsub(?:ject)?\s*[:\-]\s*(.+?)\s*"
    r"(?:dear\s+sir|dear\s+madam|dear\s+sirs?\s*/\s*madam|yours\s+faithfully|thanking\s+you|$)",
    re.IGNORECASE,
)

# Strips characters outside printable ASCII — this cleans up the garbled
# box/replacement characters that show up when a PDF embeds a non-Latin
# script (e.g. Punjabi, Hindi) in a font pdfplumber can't decode properly.
# Downside: any legitimate non-English portions of a circular get dropped
# from the summary rather than shown garbled.
NON_ASCII_RE = re.compile(r"[^\x20-\x7E]+")


def summarize_circular_text(raw_text):
    """Turns raw extracted PDF text into a short, readable summary —
    prefers the letter's own "Sub:" line, falls back to a clean truncation."""
    if not raw_text:
        return None
    cleaned = NON_ASCII_RE.sub(" ", raw_text)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None  # nothing readable survived cleaning (e.g. all non-Latin script)

    match = SUBJECT_RE.search(cleaned)
    summary = match.group(1).strip(" .") if match else cleaned

    if len(summary) > MAX_SUMMARY_CHARS:
        cut = summary[:MAX_SUMMARY_CHARS]
        last_space = cut.rfind(" ")
        summary = (cut[:last_space] if last_space > 100 else cut) + "…"
    return summary


def extract_pdf_text(url, session=None):
    """Downloads a circular PDF and returns a short readable summary of it.
    Returns None if the PDF is a scanned image (no extractable text) or the
    download fails — the caller should fall back to just linking the PDF
    in that case. Pass a warmed-up session for NSE links — NSE blocks plain
    requests without the cookies obtained by first visiting nseindia.com."""
    if not url or not url.startswith("http"):
        return None
    try:
        requester = session if session is not None else requests
        resp = requester.get(url, headers=BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log.error("PDF download failed for %s: %s", url, e)
        return None

    try:
        text_parts = []
        total_len = 0
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text:
                    text_parts.append(page_text)
                    total_len += len(page_text)
                if total_len >= MAX_RAW_SCAN_CHARS:
                    break
        raw_text = " ".join(" ".join(text_parts).split())  # collapse whitespace
        if not raw_text:
            log.info("PDF at %s produced no extractable text (likely a scanned image).", url)
            return None  # likely a scanned/image-only PDF
        return summarize_circular_text(raw_text)
    except Exception as e:
        log.error("PDF text extraction failed for %s: %s", url, e)
        return None


# ---------- formatting ----------

def escape_url_for_html(url):
    """Telegram's HTML parse mode needs & and " escaped inside href
    attributes, or links with query strings can break/get cut off."""
    return url.replace("&", "&amp;").replace('"', "&quot;")


def format_message(item):
    """Clean, no-boilerplate style: just the company, what happened, and
    a link — no repeated header line, no timestamp. The link is shown as
    a short clickable word, not the full raw URL, so it stays one line."""
    body = item.get("extracted_text") or item["subject"]
    lines = [f"📌 <b>{item['company']}</b> — {item['source']}", body]
    if item.get("link"):
        safe_url = escape_url_for_html(item["link"])
        lines.append(f'🔗 <a href="{safe_url}">View Circular</a>')
    return "\n".join(lines)


# ---------- main poll cycle ----------

def poll_and_post():
    log.info("Polling NSE + BSE for new announcements...")
    is_first_run = not os.path.exists(SEEN_FILE)
    seen = load_seen()
    new_items = []

    for fetch_fn in (fetch_nse_announcements, fetch_bse_announcements):
        try:
            items = fetch_fn()
        except Exception as e:
            log.error("Fetcher crashed: %s", e)
            items = []
        for item in items:
            if item["id"] not in seen:
                new_items.append(item)
                seen.add(item["id"])

    if is_first_run:
        # Railway wipes local files on every redeploy, so on a fresh start
        # everything looks "new." Rather than blast the whole day's backlog
        # (and get rate-limited), silently baseline it and only alert on
        # genuinely new announcements from here on.
        log.info("First run detected — baselining %d existing announcement(s) silently.", len(new_items))
        save_seen(seen)
        return

    if not new_items:
        log.info("No new announcements.")
        return

    log.info("Posting %d new announcement(s).", len(new_items))
    nse_session = get_nse_session()  # reused for downloading NSE PDF attachments below
    for item in new_items:
        # normalize BSE's relative attachment path into a full URL
        if item["source"] == "BSE" and item.get("link") and not item["link"].startswith("http"):
            item["link"] = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{item['link']}"

        if item.get("link"):
            session = nse_session if item["source"] == "NSE" else None
            item["extracted_text"] = extract_pdf_text(item["link"], session=session)

        send_telegram_message(format_message(item))
        time.sleep(1.5)  # stay comfortably under Telegram's rate limits

    save_seen(seen)


def poll_and_post_news():
    log.info("Polling company news, geopolitics, brokerage & credit rating feeds...")
    is_first_run = not os.path.exists(SEEN_NEWS_FILE)
    seen = load_seen(SEEN_NEWS_FILE)

    all_news = news_feeds.fetch_all_news()

    if is_first_run:
        for items in all_news.values():
            for it in items:
                seen.add(it["id"])
        log.info("First run detected — baselining existing news items silently.")
        save_seen(seen, SEEN_NEWS_FILE)
        return

    total_posted = 0
    for category, items in all_news.items():
        new_items = [it for it in items if it["id"] not in seen]
        if not new_items:
            continue
        # cap how many go out this cycle so a sudden burst doesn't flood
        # the channel — anything beyond the cap simply rolls into the
        # next cycle instead of being sent (it stays "unseen" until then)
        to_send = new_items[:news_feeds.MAX_ITEMS_PER_CATEGORY]
        for it in to_send:
            seen.add(it["id"])
            send_telegram_message(news_feeds.format_news_item(it))
            time.sleep(1.5)
            total_posted += 1
        if len(new_items) > len(to_send):
            log.info(
                "Category '%s' had %d extra new item(s) beyond this cycle's cap; they'll go out next cycle.",
                category, len(new_items) - len(to_send),
            )

    log.info("Posted %d new news item(s) this cycle.", total_posted)

    save_seen(seen, SEEN_NEWS_FILE)


def listen_for_manual_posts():
    """Long-polls Telegram for messages sent directly (privately) to the
    bot. Any message starting with /post from an id in AUTHORIZED_USER_IDS
    gets relayed into the channel. This runs forever in the main thread
    while the scheduled jobs run in the background."""
    if not AUTHORIZED_USER_IDS:
        log.warning(
            "AUTHORIZED_USER_IDS not set — manual /post command is disabled. "
            "You can still post directly in the channel yourself as admin."
        )
        while True:
            time.sleep(3600)  # keep the process alive for the scheduled jobs

    offset = load_offset()
    log.info("Listening for /post commands from authorized user id(s): %s", AUTHORIZED_USER_IDS)

    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=40,
            )
            data = resp.json()
        except Exception as e:
            log.error("getUpdates failed: %s", e)
            time.sleep(5)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            save_offset(offset)

            msg = update.get("message")
            if not msg:
                continue
            user_id = msg.get("from", {}).get("id")
            text = msg.get("text", "") or ""

            if user_id not in AUTHORIZED_USER_IDS:
                continue  # silently ignore messages from anyone not whitelisted

            if text.startswith("/post"):
                content = text[len("/post"):].strip()
                if content:
                    send_telegram_message(content)
                    send_message_to_chat(user_id, "✅ Posted to the channel.")
                else:
                    send_message_to_chat(user_id, "Usage: /post your message here")
            elif text.startswith("/start") or text.startswith("/help"):
                send_message_to_chat(
                    user_id,
                    "Send /post <message> and I'll relay it into the channel immediately.",
                )


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables "
            "before starting the bot. See .env.example."
        )
        return

    send_telegram_message("✅ NSE/BSE announcement bot is online.")

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(poll_and_post, "interval", minutes=POLL_INTERVAL_MIN, next_run_time=datetime.now())
    scheduler.add_job(poll_and_post_news, "interval", minutes=NEWS_INTERVAL_MIN, next_run_time=datetime.now())
    log.info(
        "Starting scheduler: NSE/BSE every %d min, news digest every %d min.",
        POLL_INTERVAL_MIN, NEWS_INTERVAL_MIN,
    )
    scheduler.start()

    listen_for_manual_posts()  # blocks forever, keeping the process alive


if __name__ == "__main__":
    main()
