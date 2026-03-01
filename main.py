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
NITTER_RSS_URL = os.environ.get("NITTER_RSS_URL", "https://nitter.net/aleabitoreddit/rss")
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

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
    print(f"Watching: {NITTER_RSS_URL}")
    print(f"Polling every {POLL_INTERVAL}s\n")
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
                    embed = create_analysis_embed(analysis)
                    await thread.send(embed=embed)
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
    if not GROQ_API_KEY:
        print("[!] GROQ_API_KEY not set in environment")
        return

    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
