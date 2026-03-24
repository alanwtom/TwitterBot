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

            # Send to Discord
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
