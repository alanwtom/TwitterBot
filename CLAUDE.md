# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a single-file Python bot that monitors a Nitter RSS feed and forwards new tweets to a Discord webhook. Nitter is an alternative Twitter/X front-end that provides RSS feeds.

## Architecture

The bot (`main.py`) runs a continuous polling loop:
1. Fetches RSS feed from Nitter instance
2. Compares entries against last seen tweet ID (persisted in `/tmp/last_tweet_id.txt`)
3. Sends any new tweets to Discord webhook
4. Sleeps for `POLL_INTERVAL` seconds

Key design details:
- **State persistence**: The last processed tweet ID is stored locally to avoid duplicates on restart
- **URL conversion**: Nitter links are converted to `twitter.com` links before sending to Discord, allowing Discord to generate native tweet embeds
- **Error handling**: Exceptions are caught and logged; the polling loop continues indefinitely

## Environment Variables

Required in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `NITTER_RSS_URL` | RSS feed URL to monitor | `https://nitter.net/aleabitoreddit/rss` |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL | *(required)* |

## Commands

### Install dependencies
```bash
pip install -r requirements.txt
```

### Run the bot
```bash
python main.py
```

## Dependencies

- `feedparser`: RSS/Atom feed parsing
- `requests`: HTTP requests for Discord webhook
