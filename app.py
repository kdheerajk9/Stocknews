"""Daily Stock News Summarizer - Flask web app."""

import hmac
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

from fundamentals import get_company  # noqa: E402
from news import fetch_news  # noqa: E402
from summarizer import summarize  # noqa: E402

MAX_TICKERS = 8
TICKER_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")

app = Flask(__name__)
# One summary per (day, ticker set) so repeat visits don't cost extra API calls.
_cache = {}
# Company data changes at most daily; cache it to keep pages fast and avoid hammering the sources.
_company_cache = {}


def _parse_tickers(raw):
    tickers = []
    for t in re.split(r"[,\s]+", (raw or "").upper()):
        if t and TICKER_RE.match(t) and t not in tickers:
            tickers.append(t)
    return tickers[:MAX_TICKERS]


@app.get("/")
def index():
    return render_template("index.html", needs_code=bool(ACCESS_CODE), max_tickers=MAX_TICKERS)


@app.get("/api/company/<ticker>")
def api_company(ticker):
    tickers = _parse_tickers(ticker)
    if not tickers:
        return jsonify(error="Invalid ticker."), 400
    ticker = tickers[0]
    key = (datetime.now(timezone.utc).strftime("%Y-%m-%d"), ticker)
    if key not in _company_cache:
        try:
            data = get_company(ticker)
        except LookupError as e:
            return jsonify(error=str(e)), 404
        except Exception:
            app.logger.exception("Company data failed for %s", ticker)
            return jsonify(error=f"Could not load data for {ticker} right now. Try again shortly."), 502
        if len(_company_cache) > 300:
            _company_cache.clear()
        _company_cache[key] = data
    return jsonify(_company_cache[key])


@app.get("/api/diag")
def api_diag():
    """TEMPORARY: checks which data sources are reachable from this server."""
    import time
    import traceback

    import requests
    import yfinance as yf

    from fundamentals import NSE_HEADERS

    out = {}
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"}
    now = int(time.time())
    urls = {
        "yahoo_chart": "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d",
        "yahoo_quotesummary": "https://query2.finance.yahoo.com/v10/finance/quoteSummary/AAPL?modules=price",
        "yahoo_timeseries": "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/AAPL"
                            f"?type=quarterlyTotalRevenue&period1={now - 86400 * 800}&period2={now}",
    }
    for name, url in urls.items():
        try:
            r = requests.get(url, headers=ua, timeout=15)
            out[name] = f"{r.status_code} {r.text[:120]}"
        except Exception as e:
            out[name] = f"ERR {e}"
    for name, url in {
        "nse_shp": "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol=CDSL",
        "nse_results": "https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=CDSL&period=Quarterly",
    }.items():
        try:
            r = requests.get(url, headers=NSE_HEADERS, timeout=15)
            out[name] = f"{r.status_code} {r.text[:80]}"
        except Exception as e:
            out[name] = f"ERR {e}"
    try:
        t = yf.Ticker("RELIANCE.NS")
        out["yf_info_keys"] = len(t.info or {})
    except Exception:
        out["yf_info"] = traceback.format_exc()[-500:]
    try:
        q = yf.Ticker("RELIANCE.NS").quarterly_income_stmt
        out["yf_quarterly_shape"] = list(q.shape)
    except Exception:
        out["yf_quarterly"] = traceback.format_exc()[-500:]
    out["yfinance_version"] = yf.__version__
    return jsonify(out)


@app.post("/api/summary")
def api_summary():
    body = request.get_json(silent=True) or {}
    if ACCESS_CODE and not hmac.compare_digest(body.get("access_code", ""), ACCESS_CODE):
        return jsonify(error="Wrong access code."), 401

    tickers = _parse_tickers(body.get("tickers"))
    if not tickers:
        return jsonify(error="Enter at least one valid ticker, e.g. AAPL, MSFT, RELIANCE.NS"), 400

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = (today, tuple(tickers))
    if key in _cache and not body.get("refresh"):
        return jsonify(_cache[key])

    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        news_by_ticker = dict(zip(tickers, pool.map(fetch_news, tickers)))

    try:
        briefing = summarize(news_by_ticker)
    except anthropic.AuthenticationError:
        return jsonify(error="Server is missing a valid ANTHROPIC_API_KEY."), 500
    except anthropic.RateLimitError:
        return jsonify(error="Rate limited by the Claude API. Try again in a minute."), 429
    except anthropic.APIStatusError as e:
        return jsonify(error=f"Claude API error ({e.status_code})."), 502
    except anthropic.APIConnectionError:
        return jsonify(error="Could not reach the Claude API."), 502
    except RuntimeError as e:
        return jsonify(error=str(e)), 502

    sources = {
        t: [{"title": a["title"], "link": a["link"], "source": a["source"]} for a in arts]
        for t, arts in news_by_ticker.items()
    }
    result = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "overview": briefing["overview"],
        "stocks": briefing["stocks"],
        "sources": sources,
    }
    if len(_cache) > 200:
        _cache.clear()
    _cache[key] = result
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
