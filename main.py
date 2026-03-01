"""
Nitter RSS → Discord Bot with AI Sentiment Analysis
----------------------------------------------------
Polls a Nitter RSS feed for new tweets, posts them to Discord,
and creates threaded replies with AI-powered financial sentiment analysis.

Requirements (requirements.txt):
    feedparser
    requests
    groq>=0.11.0
    discord.py>=2.3.0
    python-dotenv>=1.0.0
"""

import asyncio
import json
import os
import sqlite3
import feedparser
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import tasks
from groq import Groq
from dotenv import load_dotenv

# Set user agent for Nitter (some instances block default user agent)
feedparser.USER_AGENT = "Mozilla/5.0 (compatible; TwitterBot/1.0; +https://github.com/alanwtom/TwitterBot)"

# Load environment variables from .env file
load_dotenv()

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

# Nitter instances for fallback (tried in order)
NITTER_INSTANCES = os.environ.get(
    "NITTER_INSTANCES",
    "nitter.net,nitter.privacydev.net,nitter.mint.lgbt,nitter.poast.org"
).split(",")
DEFAULT_USERNAME = os.environ.get("NITTER_USERNAME", "aleabitoreddit")

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

POLL_INTERVAL = 120  # seconds between checks
# Use /data on Railway for persistent storage across restarts
LAST_TWEET_FILE = os.environ.get("LAST_TWEET_FILE", "/data/last_tweet_id.txt")
SENTIMENT_ENABLED = os.environ.get("SENTIMENT_ENABLED", "true").lower() == "true"

# Database configuration
DB_PATH = os.environ.get("DB_PATH", "/data/sentiment.db")
FLIP_ALERTS_ENABLED = os.environ.get("FLIP_ALERTS_ENABLED", "true").lower() == "true"

# Ticker filtering: comma-separated list of tickers to analyze (e.g., "BTC,ETH,SOL")
# If empty, all tweets are analyzed
TICKER_FILTERS = os.environ.get("TICKER_FILTERS", "").split(",") if os.environ.get("TICKER_FILTERS") else []
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
    # Ensure directory exists
    Path(LAST_TWEET_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(LAST_TWEET_FILE, "w") as f:
        f.write(tweet_id)


def build_rss_url(instance: str, username: str) -> str:
    """Build RSS URL for a given Nitter instance and username."""
    return f"https://{instance}/{username}/rss"


def nitter_to_twitter(url: str) -> str:
    """Convert any Nitter link to a twitter.com link."""
    for instance in NITTER_INSTANCES:
        url = url.replace(f"https://{instance}", "https://twitter.com")
        url = url.replace(f"http://{instance}", "https://twitter.com")
    return url.replace("http://", "https://")


def should_analyze(tickers: list) -> bool:
    """
    Check if analysis should proceed based on ticker filters.
    Returns True if:
    - No filters are configured (analyze all), OR
    - At least one detected ticker matches the filter list
    """
    if not TICKER_FILTERS:
        return True
    detected_upper = [t.upper().lstrip("$") for t in tickers]
    filters_upper = [f.upper().lstrip("$") for f in TICKER_FILTERS]
    return bool(set(detected_upper) & set(filters_upper))


# ──────────────────────────────────────────────────────────────
# DATABASE & SENTIMENT HISTORY
# ──────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize the SQLite database and create tables if they don't exist."""
    # Ensure directory exists
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

    # Create indexes for efficient queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickers ON sentiment_history(tickers)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON sentiment_history(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticker_sentiment ON sentiment_history(tickers, sentiment)")

    conn.commit()
    conn.close()
    print(f"[✓] Database initialized: {DB_PATH}")


def save_sentiment(entry, analysis: dict) -> None:
    """
    Save sentiment analysis to the database.

    Args:
        entry: The RSS feed entry
        analysis: The sentiment analysis dict from Groq
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Extract content from entry
        content = entry.get('summary', entry.get('title', ''))
        author = entry.get('author', 'Unknown')
        tweet_url = nitter_to_twitter(entry.link)

        # Convert tickers list to JSON string
        tickers_json = json.dumps(analysis.get("tickers", []))

        cursor.execute("""
            INSERT OR REPLACE INTO sentiment_history
            (tweet_id, tweet_url, author, content, tickers, sentiment, bull_case, bear_case, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.id,
            tweet_url,
            author,
            content,
            tickers_json,
            analysis.get("sentiment", "NEUTRAL"),
            analysis.get("bull_case", ""),
            analysis.get("bear_case", ""),
            analysis.get("summary", "")
        ))

        conn.commit()
        conn.close()
        print(f"[✓] Saved to database: {analysis.get('tickers', [])}")
    except Exception as e:
        print(f"[!] Database save error: {e}")


def get_last_sentiment(ticker: str) -> str | None:
    """
    Get the most recent sentiment for a specific ticker.

    Args:
        ticker: The ticker symbol to query (e.g., "BTC")

    Returns:
        The sentiment string ("BUY", "SELL", "NEUTRAL") or None if not found
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Query for most recent sentiment containing this ticker
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
    """
    Check if any tickers in the analysis have flipped sentiment.

    A flip occurs when:
    - Previous sentiment was BUY and new is SELL
    - Previous sentiment was SELL and new is BUY

    Args:
        analysis: The sentiment analysis dict from Groq

    Returns:
        List of flip dicts: [{"ticker": "BTC", "old": "BUY", "new": "SELL"}]
    """
    if not FLIP_ALERTS_ENABLED:
        return []

    flips = []
    new_sentiment = analysis.get("sentiment", "NEUTRAL").upper()

    # Only check for flips on BUY/SELL changes
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


async def send_flip_alert(channel, ticker: str, old_sentiment: str, new_sentiment: str) -> None:
    """
    Send a Discord alert when sentiment flips for a ticker.

    Args:
        channel: Discord channel to send alert to
        ticker: The ticker symbol
        old_sentiment: Previous sentiment (BUY/SELL)
        new_sentiment: New sentiment (BUY/SELL)
    """
    # Color based on new sentiment
    colors = {
        "BUY": 0x57F287,      # Discord green
        "SELL": 0xED4245,     # Discord red
    }
    color = colors.get(new_sentiment, 0x5865F2)

    # Emoji for signals
    emojis = {"BUY": "🟢", "SELL": "🔴"}
    old_emoji = emojis.get(old_sentiment, "⚪")
    new_emoji = emojis.get(new_sentiment, "⚪")

    # Build description
    direction = "→"
    embed = discord.Embed(
        title=f"🚨 Sentiment Flip Alert: ${ticker}",
        description=f"{old_emoji} **{old_sentiment}** {direction} {new_emoji} **{new_sentiment}**",
        color=color
    )

    # Add context
    old_ticker = get_last_sentiment(ticker)
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
# GROQ SENTIMENT ANALYSIS
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


def analyze_sentiment(entry) -> dict | None:
    """
    Analyze a tweet's financial sentiment using Groq.

    Returns a dict with tickers, sentiment, bull_case, bear_case, and summary,
    or None if analysis fails.
    """
    client = Groq(api_key=GROQ_API_KEY)

    content = entry.get('summary', entry.get('title', ''))
    author = entry.get('author', 'Unknown')

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


def create_analysis_embed(analysis: dict) -> discord.Embed:
    """Create a Discord Embed for sentiment analysis."""
    sentiment = analysis.get("sentiment", "NEUTRAL").upper()

    # Color based on sentiment
    colors = {
        "BUY": 0x57F287,      # Discord green
        "SELL": 0xED4245,     # Discord red
        "NEUTRAL": 0x5865F2,  # Discord blurple
    }
    color = colors.get(sentiment, 0x5865F2)

    # Emoji for signal
    emojis = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}
    signal_emoji = emojis.get(sentiment, "⚪")

    # Format tickers
    tickers = analysis.get("tickers", [])
    if tickers:
        ticker_display = " ".join(f"`${t}`" if not t.startswith("$") else f"`{t}`" for t in tickers[:5])
    else:
        ticker_display = "None detected"

    # Truncate long fields
    bull_case = analysis.get('bull_case', '')[:350] + "..." if len(analysis.get('bull_case', '')) > 350 else analysis.get('bull_case', '')
    bear_case = analysis.get('bear_case', '')[:350] + "..." if len(analysis.get('bear_case', '')) > 350 else analysis.get('bear_case', '')
    summary = analysis.get('summary', '')[:400] + "..." if len(analysis.get('summary', '')) > 400 else analysis.get('summary', '')

    embed = discord.Embed(
        title=f"{signal_emoji} {sentiment} Signal",
        description=f"**Tickers:** {ticker_display}",
        color=color
    )

    # Add bullish and bearish cases as inline fields (side by side)
    if bull_case:
        embed.add_field(name="🐂 Bull Case", value=bull_case or "N/A", inline=False)
    if bear_case:
        embed.add_field(name="🐻 Bear Case", value=bear_case or "N/A", inline=False)

    # Add summary
    if summary:
        embed.add_field(name="📝 Summary", value=summary, inline=False)

    # Add footer and timestamp
    embed.set_footer(text="AI-powered sentiment analysis • Llama 3.3 70B on Groq")
    embed.timestamp = datetime.now()

    return embed


# ──────────────────────────────────────────────────────────────
# DISCORD BOT
# ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    """Called when the bot successfully connects to Discord."""
    print(f"Logged in as {client.user}")
    print(f"Username: @{DEFAULT_USERNAME}")
    print(f"Nitter instances: {', '.join(NITTER_INSTANCES)}")
    if TICKER_FILTERS:
        print(f"Ticker filters: {', '.join(TICKER_FILTERS)}")
    else:
        print(f"Ticker filters: None (analyzing all tweets)")
    print(f"Polling every {POLL_INTERVAL}s")

    # Initialize database
    init_db()
    print(f"Flip alerts: {'enabled' if FLIP_ALERTS_ENABLED else 'disabled'}\n")

    # Wait a moment before starting polling loop
    await asyncio.sleep(2)
    # Start the polling loop
    poll_feed.start()


@client.event
async def on_resumed():
    """Called when the bot resumes a connection."""
    print(f"[✓] Connection resumed")
    if not poll_feed.is_running():
        poll_feed.start()


@client.event
async def on_disconnect():
    """Called when the bot disconnects."""
    print(f"[!] Disconnected from Discord")


@tasks.loop(seconds=POLL_INTERVAL)
async def poll_feed():
    """
    Main polling loop. Fetches RSS feed, posts new tweets to Discord,
    and creates threaded sentiment analysis.
    """
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print("[!] Channel not found")
        return

    # Try each Nitter instance until one works
    feed = None
    working_instance = None

    for instance in NITTER_INSTANCES:
        rss_url = build_rss_url(instance, DEFAULT_USERNAME)
        try:
            print(f"[*] Trying {instance}...", end=" ")
            feed = feedparser.parse(rss_url)
            if feed.entries:
                working_instance = instance
                print(f"OK ({len(feed.entries)} entries)")
                break
            else:
                print("empty")
        except Exception as e:
            print(f"failed: {e}")

    if not feed or not feed.entries:
        print(f"[–] All instances failed or no entries ({datetime.now().strftime('%H:%M:%S')})")
        return

    entries = feed.entries

    try:
        # Load last processed ID
        last_id = load_last_id()

        # Find new entries (newest first)
        new_entries = []
        for entry in entries:
            if entry.id == last_id:
                break
            new_entries.append(entry)

        if not new_entries:
            print(f"[–] No new posts ({datetime.now().strftime('%H:%M:%S')})")
            return

        # Process oldest first for chronological order
        for entry in reversed(new_entries):
            twitter_url = nitter_to_twitter(entry.link)

            # Send tweet URL to channel
            message = await channel.send(twitter_url)
            print(f"[✓] Sent: {twitter_url}")

            # Optional: Sentiment analysis with threaded reply
            if SENTIMENT_ENABLED:
                analysis = analyze_sentiment(entry)
                if analysis and analysis.get("tickers"):
                    # Check if we should analyze based on ticker filters
                    if should_analyze(analysis["tickers"]):
                        # Save to database for historical tracking
                        save_sentiment(entry, analysis)

                        # Check for sentiment flips and send alerts
                        flips = check_sentiment_flip(analysis)
                        for flip in flips:
                            await send_flip_alert(
                                channel,
                                flip["ticker"],
                                flip["old"],
                                flip["new"]
                            )

                        # Create thread with analysis
                        tickers_str = "-".join(analysis["tickers"][:3])
                        thread = await message.create_thread(
                            name=f"{tickers_str} Analysis",
                            auto_archive_duration=1440  # 24 hours
                        )
                        embed = create_analysis_embed(analysis)
                        await thread.send(embed=embed)
                        print(f"[✓] Added analysis for {analysis['tickers']}")
                    else:
                        print(f"[–] Skipped analysis (filtered tickers: {analysis['tickers']})")

        # Save the newest entry ID
        save_last_id(entries[0].id)

    except Exception as e:
        print(f"[!] Error: {e}")


def main():
    """Start the Discord bot."""
    if not DISCORD_BOT_TOKEN:
        print("[!] DISCORD_BOT_TOKEN not set in environment")
        return
    if not DISCORD_CHANNEL_ID:
        print("[!] DISCORD_CHANNEL_ID not set in environment")
        return
    if not GROQ_API_KEY:
        print("[!] GROQ_API_KEY not set in environment")
        return

    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
