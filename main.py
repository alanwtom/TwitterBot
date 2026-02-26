"""
Twitter → Discord Webhook Notifier
-----------------------------------
Polls a Twitter/X account for new posts and sends them to a Discord channel
via webhook.

Requirements:
    pip install tweepy requests

"""

import time
import json
import os
import requests
import tweepy
from datetime import datetime, timezone


def load_env_file(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a local .env file into os.environ."""
    if not os.path.exists(path):
        return

    with open(path) as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

# ──────────────────────────────────────────────────────────────
# CONFIG  ← set these in .env
# ──────────────────────────────────────────────────────────────
load_env_file()

TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "")  # account to watch (no @)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # seconds between checks
LAST_TWEET_FILE = os.getenv("LAST_TWEET_FILE", "last_tweet_id.txt")  # persistence
# ──────────────────────────────────────────────────────────────


def load_last_tweet_id() -> str | None:
    """Read the last processed tweet ID from disk."""
    if os.path.exists(LAST_TWEET_FILE):
        with open(LAST_TWEET_FILE) as f:
            return f.read().strip() or None
    return None


def save_last_tweet_id(tweet_id: str) -> None:
    """Persist the latest tweet ID so we don't re-send on restart."""
    with open(LAST_TWEET_FILE, "w") as f:
        f.write(tweet_id)


def send_to_discord(tweet, username: str) -> None:
    """Format and POST a tweet to the Discord webhook."""
    tweet_url = f"https://twitter.com/{username}/status/{tweet.id}"

    embed = {
        "color": 0x1DA1F2,  # Twitter blue
        "author": {
            "name": f"@{username} posted on Twitter/X",
            "icon_url": "https://abs.twimg.com/favicons/twitter.3.ico",
        },
        "description": tweet.text,
        "url": tweet_url,
        "footer": {"text": "Twitter/X"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = {"embeds": [embed]}

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )

    if response.status_code in (200, 204):
        print(f"[✓] Sent tweet {tweet.id} to Discord")
    else:
        print(f"[✗] Discord returned {response.status_code}: {response.text}")


def get_user_id(client: tweepy.Client, username: str) -> str:
    """Resolve a Twitter username to a numeric user ID."""
    user = client.get_user(username=username)
    if not user.data:
        raise ValueError(f"Twitter user '@{username}' not found.")
    return user.data.id


def fetch_new_tweets(client: tweepy.Client, user_id: str, since_id: str | None):
    """Return new tweets newer than since_id (most-recent first)."""
    kwargs = dict(
        id=user_id,
        max_results=5,
        tweet_fields=["id", "text", "created_at"],
        exclude=["retweets", "replies"],  # remove these to include RTs/replies
    )
    if since_id:
        kwargs["since_id"] = since_id

    response = client.get_users_tweets(**kwargs)
    return response.data or []


def main():
    if not TWITTER_BEARER_TOKEN or not DISCORD_WEBHOOK_URL or not TWITTER_USERNAME:
        raise ValueError(
            "Missing required config. Set TWITTER_BEARER_TOKEN, "
            "DISCORD_WEBHOOK_URL, and TWITTER_USERNAME in .env."
        )

    print(f"Starting Twitter → Discord notifier for @{TWITTER_USERNAME}")
    print(f"Polling every {POLL_INTERVAL}s  |  Ctrl-C to stop\n")

    client = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN)
    user_id = get_user_id(client, TWITTER_USERNAME)
    last_id = load_last_tweet_id()

    while True:
        try:
            tweets = fetch_new_tweets(client, user_id, last_id)

            if tweets:
                # Tweepy returns newest-first; send oldest first for natural order
                for tweet in reversed(tweets):
                    send_to_discord(tweet, TWITTER_USERNAME)

                newest_id = tweets[0].id
                save_last_tweet_id(str(newest_id))
                last_id = str(newest_id)
            else:
                print(f"[–] No new tweets  ({datetime.now().strftime('%H:%M:%S')})")

        except tweepy.TweepyException as e:
            print(f"[!] Twitter API error: {e}")
        except requests.RequestException as e:
            print(f"[!] Network error: {e}")
        except KeyboardInterrupt:
            print("\nStopped.")
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()