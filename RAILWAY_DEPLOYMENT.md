# Railway Deployment Guide

This guide covers deploying the TwitterBot to Railway for 24/7 operation.

## Prerequisites

- GitHub repository with the bot code
- Railway account (sign up at [railway.app](https://railway.app))
- Required API keys and tokens:
  - Discord Bot Token
  - Discord Channel ID
  - Groq API Key

## Deployment Steps

### 1. Prepare Your Code

Ensure your repository has these files (already included):
- `Procfile` - Tells Railway how to start the bot
- `runtime.txt` - Specifies Python version
- `.env.example` - Example environment variables
- `requirements.txt` - Python dependencies

### 2. Deploy to Railway

1. **Create Railway Project**
   - Go to [railway.app](https://railway.app)
   - Click "New Project" → "Deploy from GitHub repo"
   - Select your TwitterBot repository
   - Railway will automatically detect it's a Python project

2. **Configure Volume (CRITICAL for persistence)**
   - Go to your project's "Variables" tab
   - Scroll down to "Volumes" section
   - Click "New Volume"
   - Name: `bot-data`
   - Mount path: `/data`
   - Size: 1 GB

   This ensures your `last_tweet_id.txt` and `sentiment.db` persist across deployments!

3. **Set Environment Variables**

   In the "Variables" tab, add these:

   **Required:**
   ```
   DISCORD_BOT_TOKEN=your_actual_bot_token
   DISCORD_CHANNEL_ID=123456789012345678
   GROQ_API_KEY=your_actual_groq_key
   ```

   **Optional (defaults provided):**
   ```
   NITTER_USERNAME=aleabitoreddit
   NITTER_INSTANCES=nitter.net,nitter.privacydev.com,nitter.fdn.fr
   TICKER_FILTERS=
   SENTIMENT_ENABLED=true
   FLIP_ALERTS_ENABLED=true
   ```

   **Optional fallbacks:**
   ```
   RSS_BRIDGE_URL=https://your-rssbridge-instance.com
   TWITTER_BEARER_TOKEN=your_twitter_bearer_token
   ```

### 3. Deploy

- Railway automatically deploys when you push to GitHub
- Monitor the deployment in the "Deployments" tab
- Check logs for successful startup:
  - `Logged in as YourBot#1234`
  - `Database initialized: /data/sentiment.db`
  - `Polling every 120s`

### 4. Verify Operation

1. **Check logs** in the "Deployments" tab for:
   - Successful Discord connection
   - Database initialization message
   - Successful feed fetching

2. **Test with a real tweet:**
   - Have the monitored account post a tweet
   - Wait 1-2 minutes (poll interval)
   - Check if it appears in your Discord channel

3. **Verify persistence:**
   - Make a new commit and push
   - After redeployment, verify no duplicate tweets are posted
   - This confirms `/data` volume is working

## Free Tier Limitations

### What Works
- ✅ 512 MB RAM (plenty for this bot)
- ✅ 0.5 GB storage
- ✅ Continuous operation

### Limitations
- ⚠️ Service pauses after ~$1 usage (~500 hours/month)
- ⚠️ No automatic restarts after pauses

### Upgrade Path
If you experience issues:
- **Hobby plan**: $5/month
- Includes $5 credit (effectively free for light usage)
- True 24/7 operation, no pauses
- 8 GB RAM, 5 GB storage

## Monitoring

### Health Tracking
The bot now tracks Nitter instance health:
- Failing instances are automatically deprioritized
- Health stats shown in outage alerts
- No manual intervention needed

### Outage Alerts
If all feed sources fail:
- Alert sent after 3 consecutive failures
- Recovery alert when feed comes back online
- Health summary included in alerts

## Troubleshooting

### Bot not starting
- Check logs in "Deployments" tab
- Verify all required environment variables are set
- Ensure `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are correct

### Duplicate tweets after redeployment
- Volume may not be mounted correctly
- Check "Volumes" section in Variables tab
- Mount path must be exactly `/data`

### All feed sources failing
- Check Nitter instances are responding
- Consider adding RSS-Bridge fallback
- As last resort, configure Twitter API (costs money)

### High memory usage
- Free tier has 512 MB limit
- Bot typically uses <100 MB
- If exceeding, check for memory leaks or reduce poll interval

## Cost Summary

| Item | Monthly Cost |
|------|--------------|
| Railway hosting | $0 (free tier) or $5 (Hobby) |
| Nitter instances | $0 |
| RSS-Bridge | $0 (if self-hosted) |
| Twitter API (optional) | $0-7.50 (only if needed) |
| **Total** | **$0-12.50/month** |

For most users: **$0/month** (Railway free tier + free Nitter instances)

## Additional Resources

- [Railway Documentation](https://docs.railway.app/)
- [RSS-Bridge GitHub](https://github.com/RSS-Bridge/rss-bridge)
- [Twitter API Pricing](https://developer.twitter.com/en/products/twitter-api)
