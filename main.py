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
import re
import sqlite3
import feedparser
from datetime import datetime
from pathlib import Path
from typing import Literal

import discord
from discord.ext import tasks
from groq import Groq
from dotenv import load_dotenv
from ntscraper import Nitter

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

# Outage detection
OUTAGE_ALERT_THRESHOLD = 3  # consecutive failures before alerting
RECOVERY_ALERT_ENABLED = True  # alert when feed recovers after outage

# State tracking
consecutive_failures = 0
was_in_outage = False

# Ticker filtering: comma-separated list of tickers to analyze (e.g., "BTC,ETH,SOL")
# If empty, all tweets are analyzed
TICKER_FILTERS = os.environ.get("TICKER_FILTERS", "").split(",") if os.environ.get("TICKER_FILTERS") else []

# Thread & Reply Analysis
THREAD_ANALYSIS_ENABLED = os.environ.get("THREAD_ANALYSIS_ENABLED", "true").lower() == "true"
REPLY_ANALYSIS_ENABLED = os.environ.get("REPLY_ANALYSIS_ENABLED", "true").lower() == "true"
AUTHOR_USERNAME = os.environ.get("AUTHOR_USERNAME", "aleabitoreddit")  # For author reply filtering
MAX_THREAD_TWEETS = int(os.environ.get("MAX_THREAD_TWEETS", "10"))  # Max tweets to analyze in thread
MAX_REPLIES = int(os.environ.get("MAX_REPLIES", "20"))  # Max replies to analyze
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

def to_str(value, default=""):
    """Safely convert any value to string for SQLite. Lists become JSON strings."""
    if value is None:
        return default
    if isinstance(value, list):
        return json.dumps(value)
    return str(value)


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

        cursor.execute("""
            INSERT OR REPLACE INTO sentiment_history
            (tweet_id, tweet_url, author, content, tickers, sentiment, bull_case, bear_case, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.id,
            tweet_url,
            author,
            content,
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
# THREAD & REPLY ANALYSIS
# ──────────────────────────────────────────────────────────────

def extract_tweet_id(url: str) -> str | None:
    """Extract tweet ID from a Twitter/Nitter URL."""
    # Match patterns like: twitter.com/user/status/123456 or nitter.net/user/status/123456
    match = re.search(r'/status/(\w+)', url)
    return match.group(1) if match else None


def _get_thread_tweets_sync(tweet_url: str) -> list[dict]:
    """
    Synchronous helper to fetch thread tweets.
    """
    if not THREAD_ANALYSIS_ENABLED:
        return []

    tweet_id = extract_tweet_id(tweet_url)
    if not tweet_id:
        print(f"[!] Could not extract tweet ID from {tweet_url}")
        return []

    try:
        # Use ntscraper with multiple Nitter instances
        scraper = Nitter(log_level=0, skip_instance_check=False)

        # Try each Nitter instance
        for instance in NITTER_INSTANCES:
            try:
                print(f"[*] Fetching thread from {instance}...")
                tweets = scraper.get_tweets(
                    tweet_id,
                    mode='thread',
                    instance=f'https://{instance}'
                )

                if tweets and 'tweets' in tweets and tweets['tweets']:
                    thread_tweets = tweets['tweets'][:MAX_THREAD_TWEETS]
                    print(f"[✓] Fetched {len(thread_tweets)} thread tweets from {instance}")
                    return [
                        {
                            'text': t.get('text', ''),
                            'author': t.get('user', {}).get('username', ''),
                            'id': t.get('link', ''),
                            'is_thread': True
                        }
                        for t in thread_tweets
                    ]
            except Exception as e:
                print(f"[!] Instance {instance} failed for thread: {e}")
                continue

        print(f"[!] All instances failed for thread fetch")
        return []
    except Exception as e:
        print(f"[!] Thread fetch error: {e}")
        return []


async def get_thread_tweets(tweet_url: str) -> list[dict]:
    """
    Fetch all tweets in the thread using ntscraper.

    Returns list of tweet dicts with 'text', 'author', 'id' keys.
    Returns empty list if fetching fails.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_thread_tweets_sync, tweet_url)


def _get_replies_sync(tweet_url: str) -> list[dict]:
    """
    Synchronous helper to fetch replies.
    """
    if not REPLY_ANALYSIS_ENABLED:
        return []

    tweet_id = extract_tweet_id(tweet_url)
    if not tweet_id:
        return []

    try:
        scraper = Nitter(log_level=0, skip_instance_check=False)

        for instance in NITTER_INSTANCES:
            try:
                print(f"[*] Fetching replies from {instance}...")
                tweets = scraper.get_tweets(
                    tweet_id,
                    mode='thread',
                    instance=f'https://{instance}'
                )

                if tweets and 'tweets' in tweets and tweets['tweets']:
                    # Filter to only replies (not the original thread tweets)
                    replies = [
                        t for t in tweets['tweets']
                        if t.get('is-reply') or t.get('is_retweet') is False
                    ][:MAX_REPLIES]

                    print(f"[✓] Fetched {len(replies)} replies from {instance}")
                    return [
                        {
                            'text': t.get('text', ''),
                            'author': t.get('user', {}).get('username', ''),
                            'id': t.get('link', ''),
                            'is_reply': True
                        }
                        for t in replies
                    ]
            except Exception as e:
                print(f"[!] Instance {instance} failed for replies: {e}")
                continue

        return []
    except Exception as e:
        print(f"[!] Reply fetch error: {e}")
        return []


async def get_replies(tweet_url: str) -> list[dict]:
    """
    Fetch replies to a tweet using ntscraper.

    Returns list of reply dicts with 'text', 'author', 'id' keys.
    Returns empty list if fetching fails.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_replies_sync, tweet_url)


def is_bot_reply(reply: dict) -> bool:
    """
    Detect if a reply is from a bot using heuristics.

    Returns True if the reply appears to be from a bot.
    """
    username = reply.get('author', '').lower()
    text = reply.get('text', '').lower()

    # Always include the author's own replies
    if username == AUTHOR_USERNAME.lower():
        return False

    # Bot username patterns
    bot_patterns = [
        r'bot$',           # ends in 'bot'
        r'_bot_',          # contains '_bot_'
        r'^.*_.*_.*_.*$',  # multiple underscores
        r'\d{4,}$',        # ends in 4+ numbers
    ]

    for pattern in bot_patterns:
        if re.search(pattern, username):
            return True

    # Generic bot reply phrases
    bot_phrases = [
        'nice project',
        'great project',
        'to the moon',
        'moon soon',
        'diamond hands',
        'holding strong',
        'when launch',
        'when token',
        'when presale',
        'gm gm',
        'gn gn',
        '🚀🚀🚀',
    ]

    for phrase in bot_phrases:
        if phrase in text:
            return True

    # Very short replies (likely low effort/bot)
    if len(text) < 10 and not any(c.isalpha() for c in text):
        return True

    return False


def filter_replies(replies: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Filter replies into author replies and non-bot community replies.

    Returns:
        (author_replies, community_replies)
    """
    author_replies = []
    community_replies = []

    for reply in replies:
        username = reply.get('author', '').lower()

        # Author's own replies
        if username == AUTHOR_USERNAME.lower():
            author_replies.append(reply)
        # Non-bot replies
        elif not is_bot_reply(reply):
            community_replies.append(reply)

    return author_replies, community_replies


async def analyze_tweet_sentiment(tweet: dict) -> dict | None:
    """
    Analyze sentiment for a single tweet using Groq.

    Args:
        tweet: Dict with 'text' and 'author' keys

    Returns:
        Sentiment analysis dict or None if failed
    """
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
        print(f"[!] Groq error for tweet by {author}: {e}")
        return None


def aggregate_sentiments(analyses: list[dict]) -> dict:
    """
    Aggregate multiple sentiment analyses into a single result.

    Uses confidence-weighted averaging based on sentiment strength.
    """
    if not analyses:
        return {
            "sentiment": "NEUTRAL",
            "tickers": [],
            "bull_case": "No data available",
            "bear_case": "No data available",
            "summary": "No tweets analyzed",
            "sample_size": 0
        }

    # Count sentiments
    buy_count = sum(1 for a in analyses if a.get('sentiment') == 'BUY')
    sell_count = sum(1 for a in analyses if a.get('sentiment') == 'SELL')
    neutral_count = sum(1 for a in analyses if a.get('sentiment') == 'NEUTRAL')

    # Determine aggregate sentiment
    total = len(analyses)
    if buy_count > sell_count and buy_count > neutral_count:
        sentiment = "BUY"
    elif sell_count > buy_count and sell_count > neutral_count:
        sentiment = "SELL"
    else:
        # Tie or neutral majority
        if buy_count == sell_count:
            sentiment = "NEUTRAL"
        elif buy_count > sell_count:
            sentiment = "BUY"
        else:
            sentiment = "SELL"

    # Collect all unique tickers
    all_tickers = set()
    for a in analyses:
        all_tickers.update(a.get('tickers', []))
    tickers = list(all_tickers)[:5]

    # Aggregate bull/bear cases
    bull_cases = [a.get('bull_case', '') for a in analyses if a.get('bull_case')]
    bear_cases = [a.get('bear_case', '') for a in analyses if a.get('bear_case')]

    # Join non-empty cases
    bull_case = "\n".join(bull_cases[:3]) if bull_cases else "No bullish signals detected"
    bear_case = "\n".join(bear_cases[:3]) if bear_cases else "No bearish signals detected"

    # Summary based on counts
    summary = (
        f"Analyzed {total} tweet(s): {buy_count} bullish, {sell_count} bearish, "
        f"{neutral_count} neutral. Overall signal: {sentiment}."
    )

    return {
        "sentiment": sentiment,
        "tickers": tickers,
        "bull_case": bull_case[:500],
        "bear_case": bear_case[:500],
        "summary": summary,
        "sample_size": total,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "neutral_count": neutral_count
    }


async def get_extended_sentiment(entry) -> dict:
    """
    Get extended sentiment analysis including thread and replies.

    Returns a dict with:
    - main: Main tweet sentiment
    - thread: Thread sentiment (if available)
    - community: Non-bot reply sentiment (if available)
    - author: Author reply sentiment (if available)
    """
    main_analysis = analyze_sentiment(entry)
    if not main_analysis:
        return {"main": None}

    result = {"main": main_analysis}
    tweet_url = nitter_to_twitter(entry.link)

    # Analyze thread
    if THREAD_ANALYSIS_ENABLED:
        thread_tweets = await get_thread_tweets(tweet_url)
        if thread_tweets:
            print(f"[*] Analyzing {len(thread_tweets)} thread tweets...")
            thread_analyses = []
            for tweet in thread_tweets:
                analysis = await analyze_tweet_sentiment(tweet)
                if analysis:
                    thread_analyses.append(analysis)
            result["thread"] = aggregate_sentiments(thread_analyses)

    # Analyze replies
    if REPLY_ANALYSIS_ENABLED:
        replies = await get_replies(tweet_url)
        if replies:
            author_replies, community_replies = filter_replies(replies)

            # Analyze community replies
            if community_replies:
                print(f"[*] Analyzing {len(community_replies)} non-bot replies...")
                community_analyses = []
                for reply in community_replies[:MAX_REPLIES]:
                    analysis = await analyze_tweet_sentiment(reply)
                    if analysis:
                        community_analyses.append(analysis)
                result["community"] = aggregate_sentiments(community_analyses)

            # Analyze author replies
            if author_replies:
                print(f"[*] Analyzing {len(author_replies)} author replies...")
                author_analyses = []
                for reply in author_replies:
                    analysis = await analyze_tweet_sentiment(reply)
                    if analysis:
                        author_analyses.append(analysis)
                result["author"] = aggregate_sentiments(author_analyses)

    return result


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


def create_analysis_embed(analysis: dict, tweet_url: str = None) -> discord.Embed:
    """
    Create a Discord Embed for sentiment analysis.

    Args:
        analysis: Extended sentiment dict with main, thread, community, author keys
        tweet_url: Original tweet URL to include in the embed
    """
    main = analysis.get("main", {})
    sentiment = main.get("sentiment", "NEUTRAL").upper()

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
    tickers = main.get("tickers", [])
    if tickers:
        ticker_display = " ".join(f"`${t}`" if not t.startswith("$") else f"`{t}`" for t in tickers[:5])
    else:
        ticker_display = "None detected"

    # Build description with tweet link
    description = f"**Tickers:** {ticker_display}"
    if tweet_url:
        description += f"\n\n[**View Original Tweet**]({tweet_url})"

    # Truncate long fields
    bull_case = main.get('bull_case', '')[:350] + "..." if len(main.get('bull_case', '')) > 350 else main.get('bull_case', '')
    bear_case = main.get('bear_case', '')[:350] + "..." if len(main.get('bear_case', '')) > 350 else main.get('bear_case', '')
    summary = main.get('summary', '')[:400] + "..." if len(main.get('summary', '')) > 400 else main.get('summary', '')

    embed = discord.Embed(
        title=f"{signal_emoji} {sentiment} Signal",
        description=description,
        color=color,
        url=tweet_url or ""
    )

    # Add bullish and bearish cases
    if bull_case:
        embed.add_field(name="🐂 Bull Case", value=bull_case or "N/A", inline=False)
    if bear_case:
        embed.add_field(name="🐻 Bear Case", value=bear_case or "N/A", inline=False)

    # Add summary
    if summary:
        embed.add_field(name="📝 Summary", value=summary, inline=False)

    # Add extended sentiment section if available
    extended_fields = []

    if "thread" in analysis:
        thread = analysis["thread"]
        thread_emoji = emojis.get(thread.get("sentiment", "NEUTRAL"), "⚪")
        sample_size = thread.get("sample_size", 0)
        extended_fields.append(f"💬 **Thread:** {thread_emoji} `{thread.get('sentiment', 'N/A')}` ({sample_size} tweets)")

    if "community" in analysis:
        comm = analysis["community"]
        comm_emoji = emojis.get(comm.get("sentiment", "NEUTRAL"), "⚪")
        sample_size = comm.get("sample_size", 0)
        extended_fields.append(f"👥 **Community (non-bot):** {comm_emoji} `{comm.get('sentiment', 'N/A')}` ({sample_size} replies)")

    if "author" in analysis:
        auth = analysis["author"]
        auth_emoji = emojis.get(auth.get("sentiment", "NEUTRAL"), "⚪")
        sample_size = auth.get("sample_size", 0)
        extended_fields.append(f"✍️ **Author replies:** {auth_emoji} `{auth.get('sentiment', 'N/A')}` ({sample_size} replies)")

    if extended_fields:
        embed.add_field(name="📊 Extended Sentiment", value="\n".join(extended_fields), inline=False)

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
    print(f"Thread analysis: {'enabled' if THREAD_ANALYSIS_ENABLED else 'disabled'}")
    print(f"Reply analysis: {'enabled' if REPLY_ANALYSIS_ENABLED else 'disabled'}")

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


async def fetch_feed_with_retry(channel, retry_count=0) -> tuple:
    """
    Try to fetch RSS feed from all Nitter instances.
    Returns (feed, working_instance) or (None, None) if all fail.
    """
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
                return feed, working_instance
            else:
                print("empty")
        except Exception as e:
            print(f"failed: {e}")

    return None, None


async def send_outage_alert(channel, consecutive: int, is_recovery: bool = False) -> None:
    """Send Discord alert for feed outage or recovery."""
    if is_recovery:
        color = 0x57F287  # green
        title = "✅ Feed Recovered"
        desc = f"RSS feed is back online after **{consecutive}** failed attempts."
    else:
        color = 0xED4245  # red
        title = "⚠️ Feed Outage Detected"
        desc = f"**{consecutive} consecutive failures** fetching RSS feed. Tweets may be missed!"

    embed = discord.Embed(
        title=title,
        description=desc,
        color=color
    )
    embed.add_field(name="Instances tried", value=", ".join(NITTER_INSTANCES), inline=False)
    embed.set_footer(text="TwitterBot RSS Monitor")
    embed.timestamp = datetime.now()

    await channel.send(embed=embed)


@tasks.loop(seconds=POLL_INTERVAL)
async def poll_feed():
    """
    Main polling loop. Fetches RSS feed, posts new tweets to Discord,
    and creates threaded sentiment analysis.
    """
    global consecutive_failures, was_in_outage

    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        print("[!] Channel not found")
        return

    # Try to fetch feed
    feed, working_instance = await fetch_feed_with_retry(channel)

    if not feed or not feed.entries:
        consecutive_failures += 1
        print(f"[–] All instances failed or no entries ({datetime.now().strftime('%H:%M:%S')})")

        # Send outage alert after threshold
        if consecutive_failures == OUTAGE_ALERT_THRESHOLD:
            was_in_outage = True
            print(f"[⚠️] Outage alert: {consecutive_failures} consecutive failures")
            await send_outage_alert(channel, consecutive_failures, is_recovery=False)
        return

    entries = feed.entries

    # Reset failure counter on success
    if consecutive_failures >= OUTAGE_ALERT_THRESHOLD and was_in_outage:
        print(f"[✅] Feed recovered after {consecutive_failures} failures")
        if RECOVERY_ALERT_ENABLED:
            await send_outage_alert(channel, consecutive_failures, is_recovery=True)
        was_in_outage = False

    consecutive_failures = 0

    try:
        # Load last processed ID
        last_id = load_last_id()

        # Find new entries (newest first)
        new_entries = []
        found_last_id = False
        for entry in entries:
            if entry.id == last_id:
                found_last_id = True
                break
            new_entries.append(entry)

        # If we didn't find last_id, check if we have a gap
        if not found_last_id and last_id:
            print(f"[⚠️] Gap detected! last_id={last_id} not in feed ({len(entries)} entries)")
            print(f"[⚠️] Tweets may have been missed during outage")

            # Optional: Send alert about potential missed tweets
            embed = discord.Embed(
                title="🚨 Potential Missed Tweets",
                description=f"Last processed tweet ID (`{last_id[:20]}...`) not found in current feed of {len(entries)} entries. "
                           f"Any tweets posted during the outage may have been lost.",
                color=0xFEE75C  # yellow
            )
            embed.add_field(name="Current feed range", value=f"From: `{entries[-1].id[:20]}...`\nTo: `{entries[0].id[:20]}...`", inline=False)
            embed.set_footer(text="Consider checking the account directly for missed content")
            embed.timestamp = datetime.now()
            await channel.send(embed=embed)

            # Process all current entries to get back on track
            new_entries = list(entries)

        if not new_entries:
            print(f"[–] No new posts ({datetime.now().strftime('%H:%M:%S')})")
            return

        # Process oldest first for chronological order
        for entry in reversed(new_entries):
            twitter_url = nitter_to_twitter(entry.link)

            # Log timing info to diagnose delays
            tweet_time = entry.get('published_parsed')
            if tweet_time:
                tweet_dt = datetime(*tweet_time[:6])
                now_dt = datetime.now()
                lag_seconds = int((now_dt - tweet_dt).total_seconds())
                print(f"[🕐] Tweet: {tweet_dt.strftime('%H:%M:%S')} | Sent: {now_dt.strftime('%H:%M:%S')} | Lag: {lag_seconds}s")
            else:
                print(f"[🕐] Tweet time: unknown | Sent: {datetime.now().strftime('%H:%M:%S')}")

            # Try sentiment analysis first
            if SENTIMENT_ENABLED:
                extended_analysis = await get_extended_sentiment(entry)
                if extended_analysis.get("main") and extended_analysis["main"].get("tickers"):
                    main_analysis = extended_analysis["main"]
                    # Check if we should analyze based on ticker filters
                    if should_analyze(main_analysis["tickers"]):
                        # Save to database for historical tracking
                        save_sentiment(entry, main_analysis)

                        # Check for sentiment flips and send alerts
                        flips = check_sentiment_flip(main_analysis)
                        for flip in flips:
                            await send_flip_alert(
                                channel,
                                flip["ticker"],
                                flip["old"],
                                flip["new"]
                            )

                        # Send analysis embed directly to channel (includes tweet URL)
                        embed = create_analysis_embed(extended_analysis, tweet_url)
                        await channel.send(embed=embed)
                        print(f"[✓] Sent analysis for {main_analysis['tickers']}: {twitter_url}")
                        continue  # Skip the bare URL send
                    else:
                        print(f"[–] Skipped analysis (filtered tickers: {main_analysis['tickers']})")

            # Fallback: send bare tweet URL if analysis disabled or failed
            await channel.send(twitter_url)
            print(f"[✓] Sent: {twitter_url}")

        # Save the newest entry ID
        save_last_id(entries[0].id)

    except Exception as e:
        print(f"[!] Error: {e}")
        import traceback
        traceback.print_exc()


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
