"""
General news module: company news, geopolitics, brokerage calls, and
credit rating actions — pulled from RSS feeds and Google News RSS search
(used as a reliable proxy for outlets like Reuters that no longer run
public RSS feeds of their own).

Unlike the NSE/BSE module (one alert per filing), this module batches
everything found in a poll cycle into a single digest message per
category, so the channel doesn't get flooded — general news feeds
produce far more items per hour than exchange filings do.
"""

import re
import logging
import feedparser

log = logging.getLogger("nse-bse-bot")

GOOGLE_NEWS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

# ---- direct publisher RSS feeds (official, stable) ----
COMPANY_NEWS_FEEDS = [
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/rssfeedsdefault.cms"),
    ("Business Line", "https://www.thehindubusinessline.com/feeder/default.rss"),
    ("Financial Express", "https://www.financialexpress.com/feed/"),
]

# ---- Google News RSS search queries, used where a publisher has no ----
# ---- reliable public RSS feed of its own (e.g. Reuters), or where   ----
# ---- we want to pull together several trusted sources on one topic ----
GEOPOLITICS_QUERIES = [
    ("Reuters World News", 'site:reuters.com'),
    ("Geopolitics", 'geopolitics OR "international relations" OR sanctions OR conflict'),
    ("Trump News", 'Trump'),
]

BROKERAGE_QUERIES = [
    ("Brokerage Calls", '(brokerage OR "target price" OR upgrades OR downgrades) (NSE OR BSE OR "Indian stocks")'),
]

CREDIT_RATING_QUERIES = [
    ("Credit Ratings", '(CRISIL OR ICRA OR "CARE Ratings" OR "India Ratings") rating (upgrade OR downgrade OR outlook)'),
]

MAX_ITEMS_PER_CATEGORY = 10  # cap digest size per category per cycle

TAG_RE = re.compile(r"<[^>]+>")


def _clean(text):
    if not text:
        return ""
    text = TAG_RE.sub("", text)
    return " ".join(text.split())


def _fetch_feed(url):
    try:
        parsed = feedparser.parse(url)
        return parsed.entries or []
    except Exception as e:
        log.error("Feed fetch failed for %s: %s", url, e)
        return []


def _entries_to_items(entries, source_label, category):
    items = []
    for e in entries:
        link = e.get("link", "")
        title = _clean(e.get("title", ""))
        if not title or not link:
            continue
        item_id = f"news-{category}-{link}"
        items.append({
            "id": item_id,
            "category": category,
            "source": source_label,
            "title": title,
            "link": link,
        })
    return items


def fetch_company_news():
    all_items = []
    for label, url in COMPANY_NEWS_FEEDS:
        entries = _fetch_feed(url)
        all_items.extend(_entries_to_items(entries, label, "company_news"))
    return all_items


def fetch_geopolitics_news():
    all_items = []
    for label, query in GEOPOLITICS_QUERIES:
        url = GOOGLE_NEWS.format(query=query.replace(" ", "+"))
        entries = _fetch_feed(url)
        all_items.extend(_entries_to_items(entries, label, "geopolitics"))
    return all_items


def fetch_brokerage_news():
    all_items = []
    for label, query in BROKERAGE_QUERIES:
        url = GOOGLE_NEWS.format(query=query.replace(" ", "+"))
        entries = _fetch_feed(url)
        all_items.extend(_entries_to_items(entries, label, "brokerage"))
    return all_items


def fetch_credit_rating_news():
    all_items = []
    for label, query in CREDIT_RATING_QUERIES:
        url = GOOGLE_NEWS.format(query=query.replace(" ", "+"))
        entries = _fetch_feed(url)
        all_items.extend(_entries_to_items(entries, label, "credit_ratings"))
    return all_items


CATEGORY_LABELS = {
    "company_news": "🏢 Company & Market News",
    "geopolitics": "🌍 Geopolitics",
    "brokerage": "📊 Brokerage Calls",
    "credit_ratings": "💳 Credit Rating Actions",
}


def build_digest_message(category, items):
    """Combine several items from the same category into one message
    so a busy news cycle doesn't spam the channel with dozens of posts."""
    header = CATEGORY_LABELS.get(category, category)
    lines = [f"<b>{header}</b>"]
    for item in items[:MAX_ITEMS_PER_CATEGORY]:
        lines.append(f"• <b>[{item['source']}]</b> {item['title']}\n{item['link']}")
    return "\n\n".join(lines)


def fetch_all_news():
    """Returns dict: category -> list of new items (dedup happens in main.py)."""
    return {
        "company_news": fetch_company_news(),
        "geopolitics": fetch_geopolitics_news(),
        "brokerage": fetch_brokerage_news(),
        "credit_ratings": fetch_credit_rating_news(),
    }
