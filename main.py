"""
Nitter RSS → Discord Bot with AI Sentiment Analysis
----------------------------------------------------
Polls a Nitter RSS feed for new tweets, posts them to Discord,
and creates threaded replies with AI-powered financial sentiment analysis.

Requirements (requirements.txt):
    feedparser
    requests
    google-genai>=1.0.0
    discord.py>=2.3.0
    python-dotenv>=1.0.0
"""

import json
import os
import feedparser
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import tasks
from google import genai
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
NITTER_RSS_URL = os.environ.get("NITTER_RSS_URL", "https://nitter.net/aleabitoreddit/rss")
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-exp")

POLL_INTERVAL = 120  # seconds between checks
# Use /data on Railway for persistent storage across restarts
LAST_TWEET_FILE = os.environ.get("LAST_TWEET_FILE", "/data/last_tweet_id.txt")
SENTIMENT_ENABLED = os.environ.get("SENTIMENT_ENABLED", "true").lower() == "true"
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


def nitter_to_twitter(url: str) -> str:
    """Convert a nitter.net link to a twitter.com link."""
    return url.replace("nitter.net", "twitter.com") \
              .replace("nitter.privacyredirect.com", "twitter.com")


# ──────────────────────────────────────────────────────────────
# GEMINI SENTIMENT ANALYSIS
# ──────────────────────────────────────────────────────────────

SENTIMENT_PROMPT = """You are a financial sentiment analyst. Analyze this tweet and extract:
1. Tickers/symbols mentioned (crypto: $BTC, $ETH; stocks: NVDA, AAPL)
2. Sentiment: BUY, SELL, or NEUTRAL
3. Bull case (reasons to be long)
4. Bear case (reasons to be short/avoid)
5. Brief summary

Author: {author}
Content: {content}

Return valid JSON only:
{{
    "tickers": ["BTC", "ETH"],
    "sentiment": "BUY",
    "bull_case": "...",
    "bear_case": "...",
    "summary": "..."
}}
"""


def analyze_sentiment(entry) -> dict | None:
    """
    Analyze a tweet's financial sentiment using Gemini AI.

    Returns a dict with tickers, sentiment, bull_case, bear_case, and summary,
    or None if analysis fails.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    content = entry.get('summary', entry.get('title', ''))
    author = entry.get('author', 'Unknown')

    prompt = SENTIMENT_PROMPT.format(author=author, content=content)

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[!] Gemini error: {e}")
        return None


def format_analysis(analysis: dict) -> str:
    """Format the sentiment analysis for Discord display."""
    lines = ["**Sentiment Analysis**\n"]

    if analysis.get("tickers"):
        tickers = " ".join(f"${t}" if not t.startswith("$") else t for t in analysis["tickers"])
        lines.append(f"**Tickers:** {tickers}\n")

    sentiment = analysis.get("sentiment", "NEUTRAL")
    emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}.get(sentiment, "⚪")
    lines.append(f"**Signal:** {emoji} {sentiment}\n")

    if analysis.get("bull_case"):
        lines.append(f"**Bull:** {analysis['bull_case']}\n")
    if analysis.get("bear_case"):
        lines.append(f"**Bear:** {analysis['bear_case']}\n")
    if analysis.get("summary"):
        lines.append(f"**Summary:** {analysis['summary']}")

    return "".join(lines)


# ──────────────────────────────────────────────────────────────
# DISCORD BOT
# ──────────────────────────────────────────────────────────────

intents = discord.Intents.default()
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    """Called when the bot successfully connects to Discord."""
    print(f"Logged in as {client.user}")
    print(f"Watching: {NITTER_RSS_URL}")
    print(f"Polling every {POLL_INTERVAL}s — Ctrl-C to stop\n")
    # Start the polling loop
    poll_feed.start()


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

    try:
        feed = feedparser.parse(NITTER_RSS_URL)
        entries = feed.entries

        if not entries:
            print(f"[–] Feed empty or unreachable ({datetime.now().strftime('%H:%M:%S')})")
            return

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
                    # Create thread with analysis
                    tickers_str = "-".join(analysis["tickers"][:3])
                    thread = await message.create_thread(
                        name=f"{tickers_str} Analysis",
                        auto_archive_duration=1440  # 24 hours
                    )
                    await thread.send(format_analysis(analysis))
                    print(f"[✓] Added analysis for {analysis['tickers']}")

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
    if not GEMINI_API_KEY:
        print("[!] GEMINI_API_KEY not set in environment")
        return

    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
