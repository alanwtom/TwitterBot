# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A Discord bot that monitors a Nitter RSS feed and forwards new tweets with AI-powered financial sentiment analysis. Uses Groq API for LLM-based sentiment extraction and SQLite for historical tracking.

## Architecture

The bot (`main.py`) runs as an async Discord bot with a background polling task:

### Main Loop (`poll_feed`)
1. Tries multiple Nitter instances in order, sorted by health (fallback mechanism)
2. Falls back to RSS-Bridge if all Nitter instances fail (if configured)
3. Falls back to Twitter API as last resort (if configured, costs money)
4. Compares entries against last seen tweet ID (persisted in `LAST_TWEET_FILE`)
5. For each new tweet:
   - Sends to Groq for sentiment analysis (if enabled)
   - Saves analysis to SQLite database
   - Checks for sentiment flips and sends alerts
   - Posts tweet URL + analysis embed to Discord
6. Sleeps for `POLL_INTERVAL` seconds

### Key Systems

**Multi-Source Feed Fetching** (NEW):
- Primary: Enhanced Nitter pool with health tracking
- Secondary: RSS-Bridge (self-hosted, free)
- Emergency: Twitter API pay-per-use (optional)
- Instances tracked by success/failure rate
- Failing instances automatically deprioritized

**Instance Health Tracking** (NEW):
- Tracks success/failure rates per Nitter instance
- Sorts instances by reliability for each fetch
- Health stats shown in outage alerts
- Configurable via `HEALTH_TRACKING_ENABLED`

**Sentiment Analysis**: Uses Groq API with Llama 3.3 70B to extract:
- Ticker symbols mentioned
- Sentiment (BUY/SELL/NEUTRAL)
- Bull/bear cases
- One-sentence summary

**Database Schema** (`sentiment_history` table):
- Stores all analyzed tweets with sentiment, tickers, bull_case, bear_case
- Indexes on `tickers`, `created_at`, and composite `(tickers, sentiment)`
- Used for sentiment flip detection: queries last sentiment per ticker

**Sentiment Flip Alerts**:
- Tracks previous sentiment per ticker via database queries
- Alerts on BUY→SELL or SELL→BUY transitions
- Configurable via `FLIP_ALERTS_ENABLED`

**Outage Detection**:
- Tracks consecutive RSS fetch failures
- Alerts after `OUTAGE_ALERT_THRESHOLD` failures
- Sends recovery alert when feed comes back
- Includes instance health summary in alerts

**URL Conversion**: Nitter links converted to `twitter.com` for native Discord embeds

## Environment Variables

Required in `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `DISCORD_BOT_TOKEN` | Discord bot token | *(required)* |
| `DISCORD_CHANNEL_ID` | Target channel ID | *(required)* |
| `GROQ_API_KEY` | Groq API key for sentiment analysis | *(required)* |
| `GROQ_MODEL` | Groq model name | `llama-3.3-70b-versatile` |
| `NITTER_INSTANCES` | Comma-separated Nitter instances | `nitter.net,nitter.privacydev.com,nitter.fdn.fr` |
| `NITTER_USERNAME` | Twitter username to monitor | `aleabitoreddit` |
| `TICKER_FILTERS` | Only analyze these tickers (empty = all) | *(empty)* |
| `SENTIMENT_ENABLED` | Enable/disable sentiment analysis | `true` |
| `FLIP_ALERTS_ENABLED` | Enable/disable flip alerts | `true` |
| `LAST_TWEET_FILE` | Path to persist last tweet ID | `/data/last_tweet_id.txt` |
| `DB_PATH` | Path to SQLite database | `/data/sentiment.db` |
| `RSS_BRIDGE_URL` | RSS-Bridge instance URL (fallback) | *(empty)* |
| `TWITTER_BEARER_TOKEN` | Twitter API token (emergency) | *(empty)* |

### Optional Fallback Sources

**RSS-Bridge**:
- Self-hosted RSS bridge for Twitter
- Free option when Nitter instances fail
- Set `RSS_BRIDGE_URL` to your instance
- See: https://github.com/RSS-Bridge/rss-bridge

**Twitter API**:
- Emergency fallback when free sources fail
- Requires Twitter Basic tier ($5/mo)
- Set `TWITTER_BEARER_TOKEN` to enable
- Costs ~$0.50 per 1,000 tweets

## Railway Deployment

The bot is Railway-ready with deployment configuration files:

- `Procfile`: Specifies worker process
- `runtime.txt`: Python version (3.11)
- `/data` paths: Railway volume for persistence

See `RAILWAY_DEPLOYMENT.md` for detailed deployment instructions.

Key Railway requirements:
1. Create volume mounted at `/data` for persistence
2. Set required environment variables
3. Bot runs as background worker (not web service)

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
- `discord.py>=2.3.0`: Discord bot API
- `groq>=0.11.0`: Groq LLM API client
- `python-dotenv>=1.0.0`: Environment variable loading
- `requests`: HTTP requests (for feedparser)
