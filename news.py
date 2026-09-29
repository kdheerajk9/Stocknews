"""Collects recent news headlines for stock tickers from free RSS feeds (no API key needed)."""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import requests

USER_AGENT = "Mozilla/5.0 (compatible; StockNewsSummarizer/1.0)"
LOOKBACK_HOURS = 36
MAX_ARTICLES_PER_TICKER = 12

FEEDS = {
    "Yahoo Finance": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    "Google News": "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
}


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _fetch_feed(url):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
    resp.raise_for_status()
    return feedparser.parse(resp.content).entries


def fetch_news(ticker):
    """Return a list of recent, de-duplicated articles for one ticker, newest first."""
    articles, seen = [], set()
    urls = {
        "Yahoo Finance": FEEDS["Yahoo Finance"].format(ticker=quote_plus(ticker)),
        # Search engines don't know exchange suffixes like ".NS", so search the bare symbol.
        "Google News": FEEDS["Google News"].format(query=quote_plus(f"{ticker.split('.')[0]} stock")),
    }
    for source, url in urls.items():
        try:
            entries = _fetch_feed(url)
        except requests.RequestException:
            continue
        for e in entries:
            title = _clean(e.get("title"))
            key = title.lower()[:80]
            if not title or key in seen:
                continue
            seen.add(key)
            articles.append({
                "title": title,
                "summary": _clean(e.get("summary"))[:500],
                "link": e.get("link", ""),
                "source": source,
                "published": _published(e),
            })

    articles.sort(key=lambda a: a["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    recent = [a for a in articles if a["published"] and a["published"] >= cutoff]
    # Quiet day: fall back to the latest few headlines so the summary isn't empty.
    chosen = recent or articles[:5]
    return chosen[:MAX_ARTICLES_PER_TICKER]
