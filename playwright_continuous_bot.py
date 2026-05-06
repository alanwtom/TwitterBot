#!/usr/bin/env python3
"""
Continuous Twitter → Discord Bot using twikit
----------------------------------------------
Features:
- Cookie-based auth via twikit (no browser needed)
- Auto-detects expired cookies and re-logins
- Graceful error recovery
- Financial sentiment analysis via Groq
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from twikit import Client as TwikitClient

from dotenv import load_dotenv
import discord
from discord.ext import tasks
from groq import Groq

load_dotenv()

TWITTER_USERNAME = os.environ.get("TWITTER_USERNAME", "aleabitoreddit")
TWITTER_EMAIL = os.environ.get("TWITTER_EMAIL", "")
TWITTER_PASSWORD = os.environ.get("TWITTER_PASSWORD", "")
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 120))

LAST_TWEET_FILE = os.environ.get("LAST_TWEET_FILE", "./data/last_tweet_id.txt")
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "./data/twikit_cookies.json"))
DB_PATH = os.environ.get("DB_PATH", "./data/sentiment.db")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
SENTIMENT_ENABLED = os.environ.get("SENTIMENT_ENABLED", "true").lower() == "true"
FLIP_ALERTS_ENABLED = os.environ.get("FLIP_ALERTS_ENABLED", "true").lower() == "true"

TICKER_FILTERS = os.environ.get("TICKER_FILTERS", "").split(",") if os.environ.get("TICKER_FILTERS") else []

_client: TwikitClient | None = None
_user_id: str | None = None


# ──────────────────────────────────────────────────────────────
# DATA PERSISTENCE
# ──────────────────────────────────────────────────────────────

def load_last_id() -> str | None:
    try:
        with open(LAST_TWEET_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_id(tweet_id: str) -> None:
    Path(LAST_TWEET_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LAST_TWEET_FILE, "w") as f:
        f.write(tweet_id)


def to_str(value, default=""):
    if value is None:
        return default
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tweet_id TEXT UNIQUE,
            tweet_url TEXT,
            author TEXT,
            content TEXT,
            tickers TEXT,
            sentiment TEXT,
            bull_case TEXT,
            bear_case TEXT,
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickers ON sentiment_history(tickers)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON sentiment_history(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_sentiment ON sentiment_history(tickers, sentiment)")

    conn.commit()
    conn.close()
    print(f"[✓] Database initialized: {DB_PATH}")


def save_sentiment(tweet, analysis: dict) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO sentiment_history
            (tweet_id, tweet_url, author, content, tickers, sentiment, bull_case, bear_case, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(tweet['id']),
            tweet['url'],
            tweet['author'],
            tweet['text'],
            to_str(analysis.get("tickers", [])),
            to_str(analysis.get("sentiment", "NEUTRAL")),
            to_str(analysis.get("bull_case", "")),
            to_str(analysis.get("bear_case", "")),
            to_str(analysis.get("summary", ""))
        ))

        conn.commit()
        conn.close()
        print(f"[✓] Saved to database: {analysis.get('tickers', [])}")
    except Exception as e:
        print(f"[!] Database save error: {e}")


def get_last_sentiment(ticker: str) -> str | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT sentiment FROM sentiment_history
            WHERE tickers LIKE ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (f'%"{ticker}"%',))

        result = cursor.fetchone()
        conn.close()

        return result[0] if result else None
    except Exception as e:
        print(f"[!] Database query error: {e}")
        return None


def check_sentiment_flip(analysis: dict) -> list[dict]:
    if not FLIP_ALERTS_ENABLED:
        return []

    flips = []
    new_sentiment = analysis.get("sentiment", "NEUTRAL").upper()

    if new_sentiment == "NEUTRAL":
        return flips

    for ticker in analysis.get("tickers", []):
        ticker_upper = ticker.upper().lstrip("$")
        old_sentiment = get_last_sentiment(ticker_upper)

        if old_sentiment and old_sentiment != "NEUTRAL" and old_sentiment != new_sentiment:
            flips.append({
                "ticker": ticker_upper,
                "old": old_sentiment,
                "new": new_sentiment
            })

    return flips


def should_analyze(tickers: list) -> bool:
    if not TICKER_FILTERS:
        return True
    detected_upper = [t.upper().lstrip("$") for t in tickers]
    filters_upper = [f.upper().lstrip("$") for f in TICKER_FILTERS]
    return bool(set(detected_upper) & set(filters_upper))


# ──────────────────────────────────────────────────────────────
# SENTIMENT ANALYSIS
# ──────────────────────────────────────────────────────────────

SENTIMENT_PROMPT = """You are a financial sentiment analyst. Analyze this tweet and extract:
1. Tickers/symbols mentioned (crypto: $BTC, $ETH; stocks: NVDA, AAPL)
2. Sentiment: BUY, SELL, or NEUTRAL
3. Bull case (2-3 bullet points max, reasons to be long)
4. Bear case (2-3 bullet points max, reasons to be short/avoid)
5. One-sentence summary

Author: {author}
Content: {content}

Return valid JSON only:
{{
    "tickers": ["BTC", "ETH"],
    "sentiment": "BUY",
    "bull_case": "• Strong momentum\\n• Positive catalysts",
    "bear_case": "• Overbought conditions\\n• Risk of reversal",
    "summary": "One sentence summary here."
}}
"""


def analyze_sentiment(tweet: dict) -> dict | None:
    if not GROQ_API_KEY:
        print("[!] GROQ_API_KEY not set - skipping sentiment analysis")
        return None

    groq_client = Groq(api_key=GROQ_API_KEY)

    content = tweet.get('text', '')
    author = tweet.get('author', 'Unknown')

    prompt = SENTIMENT_PROMPT.format(author=author, content=content)

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "You are a financial sentiment analyst. Always respond with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"[!] Groq error: {e}")
        return None


def create_analysis_embed(analysis: dict, tweet_url: str) -> discord.Embed:
    sentiment = analysis.get("sentiment", "NEUTRAL").upper()

    colors = {
        "BUY": 0x57F287,
        "SELL": 0xED4245,
        "NEUTRAL": 0x5865F2,
    }
    color = colors.get(sentiment, 0x5865F2)

    emojis = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}
    signal_emoji = emojis.get(sentiment, "⚪")

    tickers = analysis.get("tickers", [])
    if tickers:
        ticker_display = " ".join(f"`${t}`" if not t.startswith("$") else f"`{t}`" for t in tickers[:5])
    else:
        ticker_display = "None detected"

    bull_case = analysis.get('bull_case', '')[:350] + "..." if len(analysis.get('bull_case', '')) > 350 else analysis.get('bull_case', '')
    bear_case = analysis.get('bear_case', '')[:350] + "..." if len(analysis.get('bear_case', '')) > 350 else analysis.get('bear_case', '')
    summary = analysis.get('summary', '')[:400] + "..." if len(analysis.get('summary', '')) > 400 else analysis.get('summary', '')

    embed = discord.Embed(
        title=f"{signal_emoji} {sentiment} Signal",
        description=f"**Tickers:** {ticker_display}",
        color=color
    )

    if bull_case:
        embed.add_field(name="🐂 Bull Case", value=bull_case or "N/A", inline=False)
    if bear_case:
        embed.add_field(name="🐻 Bear Case", value=bear_case or "N/A", inline=False)
    if summary:
        embed.add_field(name="📝 Summary", value=summary, inline=False)

    embed.set_footer(text="AI-powered sentiment analysis • Llama 3.3 70B on Groq")
    embed.timestamp = datetime.now()

    return embed


async def send_flip_alert(channel, ticker: str, old_sentiment: str, new_sentiment: str) -> None:
    colors = {
        "BUY": 0x57F287,
        "SELL": 0xED4245,
    }
    color = colors.get(new_sentiment, 0x5865F2)

    emojis = {"BUY": "🟢", "SELL": "🔴"}
    old_emoji = emojis.get(old_sentiment, "⚪")
    new_emoji = emojis.get(new_sentiment, "⚪")

    embed = discord.Embed(
        title=f"🚨 Sentiment Flip Alert: ${ticker}",
        description=f"{old_emoji} **{old_sentiment}** → {new_emoji} **{new_sentiment}**",
        color=color
    )

    embed.add_field(
        name="Details",
        value=f"The sentiment signal for **${ticker}** has changed from **{old_sentiment}** to **{new_sentiment}**.",
        inline=False
    )

    embed.set_footer(text="AI-powered sentiment tracking • Sentiment Flip Alert")
    embed.timestamp = datetime.now()

    await channel.send(embed=embed)
    print(f"[🚨] Flip alert sent: ${ticker} {old_sentiment} → {new_sentiment}")


# ──────────────────────────────────────────────────────────────
# TWIKIT CLIENT MANAGEMENT
# ──────────────────────────────────────────────────────────────

async def init_twikit() -> bool:
    """Initialize the twikit client. Returns True on success."""
    global _client, _user_id

    if _client is not None and _user_id is not None:
        return True

    _client = TwikitClient('en-US')

    # Try loading existing cookies first
    if COOKIES_FILE.exists():
        try:
            await _client.set_cookies(cookies_file=str(COOKIES_FILE))
            user = await _client.user(username=TWITTER_USERNAME)
            _user_id = user.id
            print(f"[✓] Loaded cookies, user_id={_user_id}")
            return True
        except Exception as e:
            print(f"[!] Cookie load failed: {e}")
            print("[*] Attempting fresh login...")

    # Fresh login with credentials
    if not TWITTER_EMAIL or not TWITTER_PASSWORD:
        print("[!] No cookies and no TWITTER_EMAIL/TWITTER_PASSWORD set.")
        print("[!] Set TWITTER_EMAIL and TWITTER_PASSWORD in .env, or provide a cookies file.")
        return False

    try:
        await _client.login(
            auth_info_1=TWITTER_USERNAME,
            auth_info_2=TWITTER_EMAIL,
            password=TWITTER_PASSWORD,
            cookies_file=str(COOKIES_FILE)
        )
        user = await _client.user(username=TWITTER_USERNAME)
        _user_id = user.id
        print(f"[✓] Logged in and saved cookies, user_id={_user_id}")
        return True
    except Exception as e:
        print(f"[!] Login failed: {e}")
        _client = None
        _user_id = None
        return False


async def fetch_tweets_twikit(username: str) -> list[dict] | None:
    """Fetch recent tweets via twikit. Returns list of tweet dicts or None on auth failure."""
    global _client, _user_id

    if _client is None or _user_id is None:
        print("[!] twikit client not initialized")
        return None

    try:
        tweets = await _client.get_user_tweets(_user_id, 'Tweets')

        if not tweets:
            return []

        results = []
        for tweet in tweets[:10]:
            results.append({
                'id': str(tweet.id),
                'text': tweet.text,
                'timestamp': tweet.created_at.isoformat() if tweet.created_at else '',
                'url': f"https://twitter.com/{username}/status/{tweet.id}",
                'author': tweet.user.screen_name if tweet.user else username,
                'likes': str(tweet.favorite_count) if tweet.favorite_count else '0',
                'retweets': str(tweet.retweet_count) if tweet.retweet_count else '0',
                'replies': '0',
            })

        results.sort(key=lambda x: x['timestamp'] or '', reverse=True)
        return results

    except Exception as e:
        error_str = str(e).lower()
        if 'couldnot' in error_str or 'unauthorized' in error_str or '401' in error_str or 'forbidden' in error_str:
            print(f"[!] Auth error fetching tweets: {e}")
            _client = None
            _user_id = None
            return None
        print(f"[!] Error fetching tweets: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# DISCORD BOT
# ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    print(f"\nLogged in as {bot.user}")
    print(f"Monitoring: @{TWITTER_USERNAME}")
    print(f"Polling every {POLL_INTERVAL}s")
    if TICKER_FILTERS:
        print(f"Ticker filters: {', '.join(TICKER_FILTERS)}")
    print(f"Sentiment analysis: {'enabled' if SENTIMENT_ENABLED else 'disabled'}")
    print(f"Flip alerts: {'enabled' if FLIP_ALERTS_ENABLED else 'disabled'}")

    init_db()

    ok = await init_twikit()
    if not ok:
        channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🚨 Bot Error: Twitter Auth Failed",
                description="Could not authenticate with Twitter. Check TWITTER_EMAIL/TWITTER_PASSWORD in .env or provide a valid cookies file.",
                color=0xED4245
            )
            embed.add_field(
                name="Required",
                value="1. Set TWITTER_EMAIL and TWITTER_PASSWORD in .env\n2. Or place valid cookies at data/twikit_cookies.json\n3. Restart the bot",
                inline=False
            )
            await channel.send(embed=embed)
        print("[!] Twitter auth failed — bot will retry on next poll cycle")

    poll_tweets.start()


@tasks.loop(seconds=POLL_INTERVAL)
async def poll_tweets():
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print("[!] Channel not found")
        return

    try:
        if _client is None:
            ok = await init_twikit()
            if not ok:
                print("[!] Still cannot authenticate with Twitter")
                return

        tweets = await fetch_tweets_twikit(TWITTER_USERNAME)

        if tweets is None:
            print(f"[–] Auth failure, will retry login ({datetime.now().strftime('%H:%M:%S')})")
            await init_twikit()
            return

        if not tweets:
            print(f"[–] No tweets found ({datetime.now().strftime('%H:%M:%S')})")
            return

        last_id = load_last_id()

        new_tweets = []
        for tweet in tweets:
            if tweet['id'] == last_id:
                break
            new_tweets.append(tweet)

        if not new_tweets:
            print(f"[–] No new tweets ({datetime.now().strftime('%H:%M:%S')})")
            return

        for tweet in reversed(new_tweets):
            tweet_url = tweet['url']
            print(f"[🐦] New tweet: {tweet['text'][:50]}...")

            if SENTIMENT_ENABLED and GROQ_API_KEY:
                analysis = analyze_sentiment(tweet)

                if analysis and analysis.get("tickers"):
                    if should_analyze(analysis["tickers"]):
                        save_sentiment(tweet, analysis)

                        flips = check_sentiment_flip(analysis)
                        for flip in flips:
                            await send_flip_alert(
                                channel,
                                flip["ticker"],
                                flip["old"],
                                flip["new"]
                            )

                        await channel.send(tweet_url)

                        embed = create_analysis_embed(analysis, tweet_url)
                        await channel.send(embed=embed)
                        print(f"[✓] Sent tweet and analysis for {analysis['tickers']}")
                        continue
                    else:
                        print(f"[–] Skipped analysis (filtered tickers: {analysis['tickers']})")

            await channel.send(tweet_url)
            print(f"[✓] Sent: {tweet_url}")

        save_last_id(new_tweets[0]['id'])
        print(f"[✓] Updated last_id to {new_tweets[0]['id']}\n")

    except Exception as e:
        print(f"[!] Error in poll loop: {e}")
        import traceback
        traceback.print_exc()


@poll_tweets.before_loop
async def before_poll_tweets():
    await bot.wait_until_ready()
    await asyncio.sleep(5)


def main():
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
