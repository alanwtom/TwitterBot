# Deploy with Sentiment Analysis

## Environment Variables Required

Add these to your deployment platform (Render/Railway):

```bash
# Required for sentiment analysis
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile
SENTIMENT_ENABLED=true
FLIP_ALERTS_ENABLED=true

# Optional: Filter to only analyze specific tickers
# TICKER_FILTERS=BTC,ETH,SOL

# Database path (auto-created)
DB_PATH=./data/sentiment.db
```

## Features

1. **AI-Powered Sentiment Analysis**
   - Uses Groq's Llama 3.3 70B model
   - Extracts tickers from tweets
   - Generates BUY/SELL/NEUTRAL signals
   - Creates bull/bear cases

2. **Sentiment History Tracking**
   - SQLite database stores all analyses
   - Track sentiment changes over time

3. **Flip Alerts**
   - Get notified when sentiment flips (BUY→SELL or vice versa)
   - Useful for catching trend reversals

4. **Discord Embeds**
   - Beautiful formatted analysis cards
   - Color-coded signals (green=BUY, red=SELL)
   - Shows tickers, bull/bear cases, and summary

## Cost

Free with Groq's free tier (up to 70 requests/minute).

## Manual Deployment

### Render
1. Go to https://dashboard.render.com
2. Your service → "Environment" tab
3. Add the environment variables above
4. Click "Save Changes"
5. Service will auto-restart

### Railway
```bash
railway variables set GROQ_API_KEY=gsk_...
railway variables set SENTIMENT_ENABLED=true
railway variables set FLIP_ALERTS_ENABLED=true
```
