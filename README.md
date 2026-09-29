# Daily Stock News Summarizer

Pick a few stocks, and the app collects today's news about them from free feeds (Yahoo Finance and Google News). Claude then writes a short, neutral daily summary.
It summarizes the news only. It never gives buy, sell or hold advice.

## How it works

1. `news.py` fetches the RSS headlines for each ticker, removes duplicates and keeps articles from the last 36 hours.
2. `summarizer.py` sends those headlines to Claude (`claude-opus-5`) with strict rules: stick to the articles, give no advice and stay neutral. Claude returns structured JSON.
3. `app.py` is a Flask web app. It saves each day's summary for a given set of tickers, so repeat visits don't cost you another API call.

## Run locally (Windows)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

In `.env`, set `ANTHROPIC_API_KEY` to your key from https://console.anthropic.com, then run:

```bash
.venv\Scripts\python app.py
```

Open http://localhost:5000.

## Host it on the web (Render, free tier)

1. Push this folder to a GitHub repository. `.env` is git-ignored, so your key stays private.
2. On https://render.com, choose **New → Blueprint** and select the repo. Render reads `render.yaml`.
3. When prompted, set these values:
   - `ANTHROPIC_API_KEY`: your key.
   - `ACCESS_CODE`: optional, but recommended. Visitors must enter this code to generate a summary, so strangers can't run up your API bill.
4. Deploy. You'll get a public URL like `https://stock-news-summarizer.onrender.com`.

On other hosts, such as Railway or Heroku, the `Procfile` starts the app with gunicorn. Set the same two environment variables there.

## Notes

- **Ticker format:** use Yahoo Finance symbols, for example `AAPL`, `TSLA`, `RELIANCE.NS` (NSE) or `TCS.BO` (BSE). You can enter up to 8 tickers.
- **Cost:** each summary is one Claude call. The cache means repeat visits on the same day are free.
- **Accuracy:** summaries are AI-generated from headlines and short blurbs, not full articles. Every summary card links its sources.
