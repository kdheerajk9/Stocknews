"""Turns collected news into a short daily summary using Claude."""

import json

import anthropic

MODEL = "claude-opus-5"

SYSTEM_PROMPT = """You write a short daily news briefing for busy traders.

Rules:
- Feeds sometimes include articles that only mention a stock in passing or are about other companies; ignore those.
- Summarize ONLY what the provided articles say. Do not add outside facts or guess at prices.
- Never give investment advice: no buy/sell/hold calls, no price targets, no "bullish/bearish" \
recommendations, no suggestions about what the reader should do.
- Neutral, factual tone. Mention the source when a claim is notable or disputed.
- If there is little or no real news for a stock, say so plainly in one sentence.
- Keep each stock summary to 2-4 sentences and at most 4 key points."""

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {
            "type": "string",
            "description": "1-2 sentence overview of the day's news across all stocks.",
        },
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "headline": {"type": "string", "description": "One-line gist of today's news."},
                    "summary": {"type": "string"},
                    "key_points": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["ticker", "headline", "summary", "key_points"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "stocks"],
    "additionalProperties": False,
}

client = anthropic.Anthropic()


def _format_articles(news_by_ticker):
    parts = []
    for ticker, articles in news_by_ticker.items():
        lines = [f"## {ticker}"]
        if not articles:
            lines.append("(no articles found)")
        for i, a in enumerate(articles, 1):
            when = a["published"].strftime("%Y-%m-%d %H:%M UTC") if a["published"] else "unknown time"
            lines.append(f"{i}. [{a['source']}, {when}] {a['title']}")
            if a["summary"] and a["summary"] != a["title"]:
                lines.append(f"   {a['summary']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def summarize(news_by_ticker):
    """Return {"overview": str, "stocks": [...]} for the given {ticker: [articles]}."""
    prompt = (
        "Here are today's news articles, grouped by stock ticker. "
        "Write the daily briefing, with one entry per ticker in the same order.\n\n"
        + _format_articles(news_by_ticker)
    )
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": SCHEMA},
        },
        # If a safety classifier declines, retry server-side on Anthropic's recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize this request.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The summary was cut off. Try fewer tickers.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)
