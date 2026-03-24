#!/usr/bin/env python3
"""
Continuous Twitter → Discord Bot using Playwright
--------------------------------------------------
Features:
- Reuses browser instance for efficiency
- Auto-detects expired cookies
- Graceful error recovery
- Memory-efficient page management
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext

from dotenv import load_dotenv
import discord
from discord.ext import tasks
from groq import Groq

# Load environment
load_dotenv()

# Config
TWITTER_USERNAME = os.environ.get("TWITTER_USERNAME", "aleabitoreddit")
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 120))

# Data storage
LAST_TWEET_FILE = os.environ.get("LAST_TWEET_FILE", "./data/last_tweet_id.txt")
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "./data/twikit_cookies.json"))
DB_PATH = os.environ.get("DB_PATH", "./data/sentiment.db")

# Sentiment analysis
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
SENTIMENT_ENABLED = os.environ.get("SENTIMENT_ENABLED", "true").lower() == "true"
FLIP_ALERTS_ENABLED = os.environ.get("FLIP_ALERTS_ENABLED", "true").lower() == "true"

# Ticker filtering
TICKER_FILTERS = os.environ.get("TICKER_FILTERS", "").split(",") if os.environ.get("TICKER_FILTERS") else []

# Global browser instance (reused across polls)
_browser: Browser | None = None
_context: BrowserContext | None = None


# ──────────────────────────────────────────────────────────────
# DATA PERSISTENCE
# ──────────────────────────────────────────────────────────────

def load_last_id() -> str | None:
    """Load the last processed tweet ID from disk."""
    try:
        with open(LAST_TWEET_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_id(tweet_id: str) -> None:
    """Save the last processed tweet ID to disk."""
    Path(LAST_TWEET_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LAST_TWEET_FILE, "w") as f:
        f.write(tweet_id)


def to_str(value, default=""):
    """Safely convert any value to string for SQLite. Lists become JSON strings."""
    if value is None:
        return default
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


def init_db() -> None:
    """Initialize the SQLite database for sentiment tracking."""
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
    """Save sentiment analysis to the database."""
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
    """Get the most recent sentiment for a specific ticker."""
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
    """Check if any tickers in the analysis have flipped sentiment."""
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
    """Check if analysis should proceed based on ticker filters."""
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
    """Analyze a tweet's financial sentiment using Groq."""
    if not GROQ_API_KEY:
        print("[!] GROQ_API_KEY not set - skipping sentiment analysis")
        return None

    client = Groq(api_key=GROQ_API_KEY)

    content = tweet.get('text', '')
    author = tweet.get('author', 'Unknown')

    prompt = SENTIMENT_PROMPT.format(author=author, content=content)

    try:
        response = client.chat.completions.create(
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
    """Create a Discord Embed for sentiment analysis."""
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
    """Send a Discord alert when sentiment flips for a ticker."""
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
# PLAYWRIGHT BROWSER MANAGEMENT
# ──────────────────────────────────────────────────────────────

async def init_browser() -> BrowserContext:
    """Initialize and return a persistent browser context with cookies."""
    global _browser, _context

    if _context is not None:
        return _context

    if not COOKIES_FILE.exists():
        raise Exception(f"Cookies file not found: {COOKIES_FILE}")

    # Load cookies
    with open(COOKIES_FILE) as f:
        cookies_data = json.load(f)

    # Convert to Playwright format
    playwright_cookies = []
    for cookie in cookies_data:
        pc = {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie.get("domain", ".twitter.com"),
            "path": cookie.get("path", "/"),
            "secure": cookie.get("secure", False),
            "httpOnly": cookie.get("httpOnly", False),
            "sameSite": cookie.get("sameSite", "Lax").capitalize()
        }
        playwright_cookies.append(pc)

    print("[*] Launching browser...")
    playwright = await async_playwright().start()

    _browser = await playwright.chromium.launch(
        headless=True,
        args=['--disable-dev-shm-usage', '--no-sandbox']  # Reduce memory usage
    )

    _context = await _browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080}
    )

    # Add cookies
    await _context.add_cookies(playwright_cookies)
    print("[✓] Browser initialized with cookies")

    return _context


async def check_auth_valid(context: BrowserContext) -> bool:
    """Check if current cookies are valid by visiting Twitter."""
    try:
        page = await context.new_page()
        await page.goto("https://x.com", wait_until="domcontentloaded", timeout=15000)

        # Check if redirected to login (cookies expired)
        current_url = page.url
        await page.close()

        # If we're on login page or being redirected, cookies are bad
        if "i/flow/login" in current_url or "login" in current_url:
            return False

        return True
    except Exception as e:
        print(f"[!] Auth check error: {e}")
        return False


async def fetch_tweets_page(context: BrowserContext, username: str) -> list | None:
    """Fetch tweets from a user's page. Returns None if auth failed."""
    page = None
    try:
        page = await context.new_page()
        url = f"https://x.com/{username}"
        print(f"[*] Fetching {url}...")

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Check for auth failure
        if "i/flow/login" in page.url or "login" in page.url:
            print("[!] Auth failed - redirected to login")
            await page.close()
            return None

        # Wait for tweets to load
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)

        # Extract tweets
        tweets = await page.evaluate('''
            () => {
                const articles = document.querySelectorAll('article[data-testid="tweet"]');
                return Array.from(articles).slice(0, 10).map(article => {
                    // Try to get tweet ID from permalink
                    const timeLink = article.querySelector('a[href*="/status/"]');
                    const tweetId = timeLink ? timeLink.href.split('/status/')[1]?.split('?')[0] || timeLink.href.split('/status/')[1] : null;

                    // Get text
                    const textEl = article.querySelector('[data-testid="tweetText"]');
                    const text = textEl ? textEl.innerText : '';

                    // Get timestamp
                    const timeEl = article.querySelector('time');
                    const timestamp = timeEl ? timeEl.getAttribute('datetime') : '';

                    // Get metrics
                    const getMetric = (testId) => {
                        const el = article.querySelector(`[data-testid="${testId}"]`);
                        return el ? el.innerText : '0';
                    };

                    return {
                        id: tweetId,
                        text: text,
                        timestamp: timestamp,
                        likes: getMetric('like'),
                        retweets: getMetric('retweet'),
                        replies: getMetric('reply')
                    };
                }).filter(t => t.id !== null);
            }
        ''')

        await page.close()

        # Sort by timestamp (newest first)
        tweets.sort(key=lambda x: x['timestamp'] or '', reverse=True)

        return tweets

    except Exception as e:
        print(f"[!] Error fetching tweets: {e}")
        if page:
            await page.close()
        return None


async def close_browser():
    """Close the browser instance."""
    global _browser, _context
    if _browser:
        await _browser.close()
        _browser = None
        _context = None
        print("[✓] Browser closed")


# ──────────────────────────────────────────────────────────────
# DISCORD BOT
# ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    """Called when the bot successfully connects to Discord."""
    print(f"\nLogged in as {client.user}")
    print(f"Monitoring: @{TWITTER_USERNAME}")
    print(f"Polling every {POLL_INTERVAL}s")
    if TICKER_FILTERS:
        print(f"Ticker filters: {', '.join(TICKER_FILTERS)}")
    print(f"Sentiment analysis: {'enabled' if SENTIMENT_ENABLED else 'disabled'}")
    print(f"Flip alerts: {'enabled' if FLIP_ALERTS_ENABLED else 'disabled'}")

    # Initialize database
    init_db()

    # Initialize browser
    try:
        context = await init_browser()
        valid = await check_auth_valid(context)

        if not valid:
            print("\n[!] Cookies are EXPIRED!")
            print("[!] Please refresh cookies:")
            print("    1. Log into https://x.com in your browser")
            print("    2. Export cookies using 'Get cookies.txt LOCALLY' extension")
            print("    3. Save to data/twikit_cookies.json")
            print("    4. Restart the bot\n")
            await close_browser()
        else:
            print("[✓] Cookies are valid\n")

    except Exception as e:
        print(f"\n[!] Browser init failed: {e}\n")

    # Start polling
    poll_tweets.start()


@tasks.loop(seconds=POLL_INTERVAL)
async def poll_tweets():
    """Main polling loop."""
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print("[!] Channel not found")
        return

    # Get or create browser context
    try:
        context = await init_browser()

        # Check auth validity
        valid = await check_auth_valid(context)
        if not valid:
            print("[!] Cookies expired during polling")
            embed = discord.Embed(
                title="🚨 Bot Error: Cookies Expired",
                description="Twitter cookies have expired. Please refresh them and restart the bot.",
                color=0xED4245
            )
            embed.add_field(
                name="Instructions",
                value="1. Log into https://x.com\n2. Export cookies with browser extension\n3. Save to data/twikit_cookies.json\n4. Restart bot",
                inline=False
            )
            await channel.send(embed=embed)
            poll_tweets.stop()
            await close_browser()
            return

        # Fetch tweets
        tweets = await fetch_tweets_page(context, TWITTER_USERNAME)

        if tweets is None:
            print(f"[–] Failed to fetch tweets ({datetime.now().strftime('%H:%M:%S')})")
            return

        if not tweets:
            print(f"[–] No tweets found ({datetime.now().strftime('%H:%M:%S')})")
            return

        # Load last processed ID
        last_id = load_last_id()

        # Find new tweets
        new_tweets = []
        for tweet in tweets:
            if tweet['id'] == last_id:
                break
            new_tweets.append(tweet)

        if not new_tweets:
            print(f"[–] No new tweets ({datetime.now().strftime('%H:%M:%S')})")
            return

        # Process oldest first
        for tweet in reversed(new_tweets):
            tweet_url = f"https://twitter.com/{TWITTER_USERNAME}/status/{tweet['id']}"
            print(f"[🐦] New tweet: {tweet['text'][:50]}...")

            # Add URL to tweet dict for sentiment analysis
            tweet['url'] = tweet_url
            tweet['author'] = TWITTER_USERNAME

            # Sentiment analysis
            if SENTIMENT_ENABLED and GROQ_API_KEY:
                analysis = analyze_sentiment(tweet)

                if analysis and analysis.get("tickers"):
                    if should_analyze(analysis["tickers"]):
                        save_sentiment(tweet, analysis)

                        # Check for flips
                        flips = check_sentiment_flip(analysis)
                        for flip in flips:
                            await send_flip_alert(
                                channel,
                                flip["ticker"],
                                flip["old"],
                                flip["new"]
                            )

                        # Send tweet and analysis
                        embed = create_analysis_embed(analysis, tweet_url)
                        await channel.send(content=tweet_url, embed=embed)
                        print(f"[✓] Sent analysis for {analysis['tickers']}")
                        continue
                    else:
                        print(f"[–] Skipped analysis (filtered tickers: {analysis['tickers']})")

            # Fallback: send bare tweet URL
            await channel.send(tweet_url)
            print(f"[✓] Sent: {tweet_url}")

        # Save newest tweet ID
        save_last_id(new_tweets[0]['id'])
        print(f"[✓] Updated last_id to {new_tweets[0]['id']}\n")

    except Exception as e:
        print(f"[!] Error in poll loop: {e}")
        import traceback
        traceback.print_exc()


@poll_tweets.before_loop
async def before_poll_tweets():
    """Wait before starting first poll."""
    await client.wait_until_ready()
    await asyncio.sleep(5)


def main():
    """Start the Discord bot."""
    try:
        client.run(DISCORD_BOT_TOKEN)
    finally:
        # Cleanup on exit
        if _browser:
            asyncio.run(close_browser())


if __name__ == "__main__":
    main()
