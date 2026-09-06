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

def send_message_to_chat(chat_id, text):
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
        if resp.status_code != 200:
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
    try:
        resp = requests.get(BSE_ANNOUNCEMENTS_URL, params=params, headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("BSE fetch failed: %s", e)
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


MAX_EXTRACTED_CHARS = 3200  # keep well under Telegram's 4096-char message limit


def extract_pdf_text(url):
    """Downloads a circular PDF and pulls its text out. Returns None if the
    PDF is a scanned image (no extractable text) or the download fails —
    the caller should fall back to just linking the PDF in that case."""
    if not url or not url.startswith("http"):
        return None
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
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
                if total_len >= MAX_EXTRACTED_CHARS:
                    break
        text = " ".join(" ".join(text_parts).split())  # collapse whitespace
        if not text:
            return None  # likely a scanned/image-only PDF
        if len(text) > MAX_EXTRACTED_CHARS:
            text = text[:MAX_EXTRACTED_CHARS].rsplit(" ", 1)[0] + "…"
        return text
    except Exception as e:
        log.error("PDF text extraction failed for %s: %s", url, e)
        return None


# ---------- formatting ----------

def format_message(item):
    lines = [
        f"📢 <b>{item['source']} Announcement</b>",
        f"<b>{item['company']}</b>",
        item["subject"],
    ]
    if item.get("time"):
        lines.append(f"🕒 {item['time']}")

    if item.get("extracted_text"):
        lines.append("")
        lines.append(item["extracted_text"])
        if item.get("link"):
            lines.append("")
            lines.append(f"🔗 Full circular: {item['link']}")
    elif item.get("link"):
        lines.append(item["link"])

    return "\n".join(lines)


# ---------- main poll cycle ----------

def poll_and_post():
    log.info("Polling NSE + BSE for new announcements...")
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

    if not new_items:
        log.info("No new announcements.")
        return

    log.info("Posting %d new announcement(s).", len(new_items))
    for item in new_items:
        # normalize BSE's relative attachment path into a full URL
        if item["source"] == "BSE" and item.get("link") and not item["link"].startswith("http"):
            item["link"] = f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{item['link']}"

        if item.get("link"):
            item["extracted_text"] = extract_pdf_text(item["link"])

        send_telegram_message(format_message(item))
        time.sleep(1.5)  # stay comfortably under Telegram's rate limits

    save_seen(seen)


def poll_and_post_news():
    log.info("Polling company news, geopolitics, brokerage & credit rating feeds...")
    seen = load_seen(SEEN_NEWS_FILE)

    all_news = news_feeds.fetch_all_news()

    for category, items in all_news.items():
        new_items = [it for it in items if it["id"] not in seen]
        if not new_items:
            continue
        for it in new_items:
            seen.add(it["id"])
        message = news_feeds.build_digest_message(category, new_items)
        send_telegram_message(message)
        time.sleep(1.5)
        log.info("Posted %d new item(s) in category '%s'.", len(new_items), category)

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
