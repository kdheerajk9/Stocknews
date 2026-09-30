# StockNews Daily

Pick your stocks and get everything you need in one simple page:

- **Today's news, summarised by Claude AI:** short and neutral, with links to the sources. It never gives buy or sell advice.
- **Insights:** automatic positive and negative highlights, such as profit growth, mutual fund holding up, or FII holding down.
- **Fundamentals:** market cap, P/E, P/B, ROE, EPS, dividend yield, book value and debt-to-equity. Each one has a plain-language explanation.
- **Financial performance:** quarterly and yearly revenue and profit chart, with year-on-year growth.
- **Shareholding pattern:** promoters, FII, DII and public by quarter, plus mutual fund and retail-investor trends. For Indian stocks this comes from official NSE filings.
- **About the company:** description, CEO, sector, employees and website.

The page works on phones, has light and dark themes, and remembers your watchlist.

## How it works

| File | What it does |
|---|---|
| `news.py` | Collects the last ~36 hours of headlines from Yahoo Finance and Google News RSS |
| `summarizer.py` | Sends headlines to Claude (`claude-opus-5`) with strict "summarise only, no advice" rules |
| `fundamentals.py` | Gets price, ratios, results and profile from Yahoo Finance (`yfinance`), and shareholding from NSE filings |
| `app.py` | Flask web app; caches each stock's data and each summary for the day |

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
