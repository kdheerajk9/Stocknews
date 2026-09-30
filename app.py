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

from fundamentals import direct_candidates, get_company, name_matches, search  # noqa: E402
from news import fetch_news  # noqa: E402
from summarizer import summarize  # noqa: E402

MAX_TICKERS = 8
TICKER_RE = re.compile(r"^[A-Z0-9.&\-^=]{1,20}$")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")

app = Flask(__name__)
# One summary per (day, ticker set) so repeat visits don't cost extra API calls.
_cache = {}
# Company data changes at most daily; cache it to keep pages fast and avoid hammering the sources.
_company_cache = {}
_search_cache = {}


def _parse_tickers(raw):
    tickers = []
    for t in re.split(r"[,\s]+", (raw or "").upper()):
        if t and TICKER_RE.match(t) and t not in tickers:
            tickers.append(t)
    return tickers[:MAX_TICKERS]


@app.get("/")
def index():
    return render_template("index.html", needs_code=bool(ACCESS_CODE), max_tickers=MAX_TICKERS)


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()[:40]
    if len(q) < 2:
        return jsonify(results=[])
    key = q.lower()
    if key not in _search_cache:
        try:
            _search_cache[key] = search(q)
        except Exception:
            app.logger.exception("Search failed for %s", q)
            return jsonify(results=[])
        if len(_search_cache) > 1000:
            _search_cache.clear()
    return jsonify(results=_search_cache[key])


def _load_company(ticker):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = (today, ticker)
    if key not in _company_cache:
        data = get_company(ticker)  # raises LookupError for unknown symbols
        if len(_company_cache) > 300:
            _company_cache.clear()
        _company_cache[key] = data
    return _company_cache[key]


@app.get("/api/company/<path:query>")
def api_company(query):
    query = query.strip().upper()
    if not query or not TICKER_RE.match(query):
        return jsonify(error="Invalid symbol."), 400
    # Try the symbol as typed (adding .NS / .BO for Indian stocks), then match by company name.
    tried = set()
    try:
        for candidates in (lambda: direct_candidates(query), lambda: name_matches(query)):
            for ticker in candidates():
                if ticker in tried:
                    continue
                tried.add(ticker)
                try:
                    return jsonify(_load_company(ticker))
                except LookupError:
                    continue
    except Exception:
        app.logger.exception("Company data failed for %s", query)
        return jsonify(error=f"Could not load data for {query} right now. Try again shortly."), 502
    return jsonify(error=f"Couldn't find a stock called {query}. Try searching by company name."), 404


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
