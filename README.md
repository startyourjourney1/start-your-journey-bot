# NSE/BSE Announcements → Telegram Channel Bot

Polls NSE and BSE's public corporate-announcement feeds every few minutes
and posts new ones (results, board meetings, dividends, allotments, etc.)
into your Telegram channel, 24/7.

## 1. Create the Telegram channel + bot (5 minutes, one-time, manual)

1. In Telegram, tap **New Channel**, name it (e.g. "NSE BSE Live Announcements"), set it Public or Private.
2. Message **@BotFather** → `/newbot` → follow the prompts → it gives you a **token** like `123456789:AAExample...`. Save it.
3. Add your new bot to your channel as an **administrator** (channel → Administrators → Add Admin → search your bot's username).
4. Get your channel's chat ID:
   - Easiest: post any message in the channel, then visit
     `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and look for `"chat":{"id": ...}` — it'll be a negative number like `-1001234567890`.
   - If that's empty (common for channels), forward a channel message to **@userinfobot** or **@JsonDumpBot**, or add `@getidsbot` to the channel briefly.

## 2. Deploy to Railway (free tier)

1. Go to [railway.app](https://railway.app) → sign up (GitHub login is easiest).
2. Push this folder to a new GitHub repo (or use Railway's "Deploy from local folder" via their CLI).
3. In Railway: **New Project → Deploy from GitHub repo** → select the repo.
4. Railway auto-detects Python from `requirements.txt`. In the service's **Settings → Deploy**, set:
   - **Start command**: `python main.py`
   - Under **Settings**, there's no need to expose a public port — this is a background worker, not a web server.
5. Go to **Variables** and add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your channel's chat id (with the `-100` prefix)
   - `POLL_INTERVAL_MIN` = `5` (or whatever cadence you want)
6. Deploy. Check the **Logs** tab — you should see `"Starting scheduler..."` and a "✅ NSE/BSE announcement bot is online." message land in your channel.

Railway's free tier gives ~$5/month of usage credit, which comfortably covers a lightweight polling worker like this running continuously. If you outgrow it, Render's free worker tier or a $5/mo VPS works the same way.

## 3. Posting manually, whenever you want

Two ways:

- **Directly in the channel** — you're the admin, so you can just type and send a message in the channel yourself, any time, no setup needed. This works right now regardless of anything below.
- **Through the bot, from anywhere** — set `AUTHORIZED_USER_IDS` (see below) and then, in a *private* chat with your bot (search its username and hit Start), send:
  ```
  /post Nifty just crossed 26,000 — new all-time high.
  ```
  The bot relays it straight into the channel and replies "✅ Posted to the channel." This is handy if you're on your phone and don't want to switch into the channel itself, or if you want everything the bot posts (auto + manual) to look consistent.

  To enable this, get your own Telegram numeric user id — message **@userinfobot**, it replies with your id — then set the `AUTHORIZED_USER_IDS` env var to that number (comma-separate multiple ids if more than one person should be able to trigger posts). Only messages from ids in that list are honored; everyone else's DMs to the bot are ignored.

## 4. How it works

Two independent jobs run on a schedule (APScheduler):

- **Exchange filings** (`poll_and_post`, every `POLL_INTERVAL_MIN`, default 5 min) — polls NSE's `corporate-announcements` endpoint and BSE's `AnnGetData` endpoint, posts **one message per new filing** since these are low-volume and time-sensitive.
- **General news digest** (`poll_and_post_news`, every `NEWS_INTERVAL_MIN`, default 20 min) — pulls from `news_feeds.py` and posts **one batched digest message per category** (so a busy news hour doesn't flood the channel with 40 separate messages):
  - 🏢 **Company & market news** — direct RSS from Moneycontrol, Economic Times, Business Line, Financial Express.
  - 🌍 **Geopolitics & Trump news** — via Google News RSS search, including a `site:reuters.com` query (Reuters retired its own public RSS feeds years ago, so this is the standard reliable workaround news aggregators use).
  - 📊 **Brokerage calls** — Google News RSS query for "target price / upgrade / downgrade" mentions tied to NSE/BSE stocks.
  - 💳 **Credit rating actions** — Google News RSS query for CRISIL / ICRA / CARE Ratings / India Ratings rating actions.

Both jobs track what's already been posted in `seen_ids.json` / `seen_news_ids.json` so nothing repeats.

### Editing the feed list

Open `news_feeds.py` — `COMPANY_NEWS_FEEDS` is a list of `(label, rss_url)` tuples you can add to directly (any outlet with a public RSS feed works). `GEOPOLITICS_QUERIES`, `BROKERAGE_QUERIES`, and `CREDIT_RATING_QUERIES` are `(label, search query)` tuples run through Google News RSS — edit the query strings to tune what gets pulled in (e.g. add `OR site:bloomberg.com` to broaden sources).

### On "brokerage house" and "credit rating" coverage — please read

Individual brokerages (Motilal Oswal, ICICI Securities, Jefferies, etc.) and rating agencies (CRISIL, ICRA, CARE) don't publish public RSS/API feeds of their research notes or press releases — those are distributed to paying clients or behind logins. The `brokerage` and `credit_ratings` categories here work by searching Google News for public reporting *about* brokerage calls and rating actions (i.e. when Moneycontrol/ET/etc. write an article about a broker's target-price change or a CRISIL rating action), not the original notes themselves. This is the same limitation every retail-facing aggregator has — genuine real-time brokerage research feeds require a paid data vendor (Bloomberg, Refinitiv, Trendlyne, TrendlyneAPI, etc.).

## 5. Important limitations — please read

- **NSE and BSE have no official public API.** This uses the same public JSON endpoints their own website's front-end calls (no login required), which is how the well-known open-source scrapers (`stock-nse-india`, `BseIndiaApi`) work too. That means:
  - These endpoints can change or start blocking without notice — if the bot goes quiet, check the logs first.
  - Keep polling frequency reasonable (5 min is plenty) — hammering their servers can get your IP rate-limited.
- **RSS/Google News content is headlines + links only** — the bot never reproduces full article text, both for copyright reasons and because that's genuinely all an RSS feed gives you. Tap through to read the full piece.
- Not financial advice — this just relays official exchange filings faster than checking the website manually.

## 6. Local testing (optional, before deploying)

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in your real token + chat id
export $(cat .env | xargs)   # loads env vars (mac/linux)
python main.py
```
