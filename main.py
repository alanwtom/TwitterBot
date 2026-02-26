"""
Nitter RSS → Discord Webhook Notifier
--------------------------------------
Polls a Nitter RSS feed for new tweets and sends the Twitter link to Discord.

Requirements (requirements.txt):
    feedparser
    requests
"""

import os
import time
import feedparser
import requests
from datetime import datetime, timezone

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
NITTER_RSS_URL      = os.environ.get("NITTER_RSS_URL", "https://nitter.net/aleabitoreddit/rss")
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
POLL_INTERVAL       = 120   # seconds between checks
LAST_TWEET_FILE     = "/tmp/last_tweet_id.txt"
# ──────────────────────────────────────────────────────────────


def load_last_id() -> str | None:
    try:
        with open(LAST_TWEET_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_last_id(tweet_id: str) -> None:
    with open(LAST_TWEET_FILE, "w") as f:
        f.write(tweet_id)


def nitter_to_twitter(url: str) -> str:
    """Convert a nitter.net link to a twitter.com link."""
    return url.replace("nitter.net", "twitter.com") \
              .replace("nitter.privacyredirect.com", "twitter.com")


def send_to_discord(entry) -> None:
    twitter_url = nitter_to_twitter(entry.link)
    # Send only the raw Twitter URL as the message content so that
    # Discord can generate its own native embed for the tweet.
    payload = {"content": twitter_url}

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload)
    if r.status_code in (200, 204):
        print(f"[✓] Sent: {twitter_url}")
    else:
        print(f"[✗] Discord error {r.status_code}: {r.text}")


def main():
    print(f"Watching: {NITTER_RSS_URL}")
    print(f"Polling every {POLL_INTERVAL}s — Ctrl-C to stop\n")

    last_id = load_last_id()

    while True:
        try:
            feed = feedparser.parse(NITTER_RSS_URL)
            entries = feed.entries

            if not entries:
                print(f"[–] Feed empty or unreachable ({datetime.now().strftime('%H:%M:%S')})")
            else:
                # Newest entry is first; send oldest-first
                new_entries = []
                for entry in entries:
                    if entry.id == last_id:
                        break
                    new_entries.append(entry)

                for entry in reversed(new_entries):
                    send_to_discord(entry)

                if new_entries:
                    save_last_id(entries[0].id)
                    last_id = entries[0].id
                else:
                    print(f"[–] No new posts ({datetime.now().strftime('%H:%M:%S')})")

        except Exception as e:
            print(f"[!] Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()