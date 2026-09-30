"""Company data: fundamentals, financial performance, about, shareholding pattern and insights.

Sources (all free, no API key):
- Yahoo Finance via the `yfinance` library: price, ratios, quarterly/yearly results, company profile.
- NSE shareholding-pattern filings (official exchange disclosures) for Indian stocks: promoters,
  FII, DII, mutual funds, retail holdings by quarter.
"""

import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
import yfinance as yf

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
NSE_SHP_LIST = "https://www.nseindia.com/api/corporate-share-holdings-master?index=equities&symbol={symbol}"
SHP_QUARTERS = 5

# XBRL shareholding-pattern elements -> our field names (values are fractions of total shares).
SHP_FIELDS = {
    "ShareholdingOfPromoterAndPromoterGroup": "promoters",
    "InstitutionsForeign": "fii",
    "InstitutionsDomestic": "dii",
    "NonInstitutions": "public",
    "MutualFundsOrUTI": "mutual_funds",
    "InsuranceCompanies": "insurance",
    "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakh": "retail",
    "ResidentIndividualShareholdersHoldingNominalShareCapitalInExcessOfRsTwoLakh": "hni",
}


def _num(value):
    """Convert pandas/numpy/None values to a plain float, or None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _pct(new, old):
    if new is None or old in (None, 0):
        return None
    return (new - old) / abs(old) * 100


def _quarter_label(ts):
    return ts.strftime("%b '%y")


# ---------------------------------------------------------------- Yahoo Finance

def _results(stmt, label_fn):
    """Revenue/profit series (oldest first) from a yfinance income statement."""
    if stmt is None or stmt.empty:
        return []
    rows = []
    for col in stmt.columns:
        revenue = _num(stmt.at["Total Revenue", col]) if "Total Revenue" in stmt.index else None
        profit = _num(stmt.at["Net Income", col]) if "Net Income" in stmt.index else None
        if revenue is None and profit is None:
            continue
        rows.append({"period": label_fn(col), "date": col.to_pydatetime(), "revenue": revenue, "profit": profit})
    rows.sort(key=lambda r: r["date"])
    return rows


def _yoy(rows, field):
    """Year-on-year growth of the latest quarter vs. the same quarter last year (if present)."""
    if not rows:
        return None
    latest = rows[-1]
    target = latest["date"] - timedelta(days=365)
    for r in rows[:-1]:
        if abs((r["date"] - target).days) <= 20:
            return _pct(latest[field], r[field])
    return None


def _latest(df, row):
    """Most recent non-empty value of a row in a yfinance statement."""
    if df is None or df.empty or row not in df.index:
        return None
    for col in sorted(df.columns, reverse=True):
        v = _num(df.at[row, col])
        if v is not None:
            return v
    return None


def _ttm_eps(t):
    """Trailing-12-month EPS: last 4 consecutive quarters, else the latest annual figure."""
    q = t.quarterly_income_stmt
    if q is not None and not q.empty and "Diluted EPS" in q.index:
        cols = sorted(q.columns, reverse=True)[:4]
        vals = [_num(q.at["Diluted EPS", c]) for c in cols]
        if len(cols) == 4 and None not in vals and (cols[0] - cols[3]).days <= 290:
            return sum(vals)
    return _latest(t.income_stmt, "Diluted EPS")


def _computed_fundamentals(t, price):
    """Ratios calculated from published statements and price history.

    Used when Yahoo's company-info feed is unavailable (it is often blocked for cloud servers,
    while the statements and chart feeds keep working).
    """
    bs = t.balance_sheet
    equity = _latest(bs, "Stockholders Equity")
    shares = _latest(bs, "Ordinary Shares Number") or _latest(bs, "Share Issued")
    debt = _latest(bs, "Total Debt")
    net_income = _latest(t.income_stmt, "Net Income")
    eps = _ttm_eps(t)
    book = equity / shares if equity and shares else None

    div_yield = None
    try:
        divs = t.dividends.copy()
        if price and divs is not None and len(divs):
            # Per-share dividends paid before a bonus issue/split must be scaled to today's share count.
            for date, ratio in t.splits.items():
                if ratio and ratio > 0:
                    divs[divs.index < date] /= ratio
            cutoff = divs.index[-1] - timedelta(days=365)
            div_yield = float(divs[divs.index > cutoff].sum()) / price * 100
    except Exception:
        pass

    return {
        "market_cap": price * shares if price and shares else None,
        "pe": price / eps if price and eps and eps > 0 else None,
        "pb": price / book if price and book and book > 0 else None,
        "roe": net_income / equity * 100 if net_income is not None and equity else None,
        "eps": eps,
        "dividend_yield": div_yield,
        "book_value": book,
        "debt_to_equity": debt / equity if debt is not None and equity else None,
    }


def _safe_info(t):
    try:
        return t.info or {}
    except Exception:
        return {}


def _profile(ticker):
    t = yf.Ticker(ticker)
    info = _safe_info(t)
    hist = t.history(period="1y", auto_adjust=False)
    meta = t.get_history_metadata() if not hist.empty else {}
    name = info.get("longName") or info.get("shortName") or meta.get("longName") or meta.get("shortName")
    if not name or hist.empty:
        raise LookupError(f"No data found for {ticker}. Check the symbol (e.g. RELIANCE.NS, AAPL).")

    closes = hist["Close"].dropna()
    price = _num(info.get("currentPrice") or info.get("regularMarketPrice") or meta.get("regularMarketPrice"))         or _num(closes.iloc[-1])
    prev = _num(info.get("previousClose") or info.get("regularMarketPreviousClose"))
    if prev is None and len(closes) >= 2:
        prev = _num(closes.iloc[-2])

    computed = _computed_fundamentals(t, price)
    roe = _num(info.get("returnOnEquity"))
    dte = _num(info.get("debtToEquity"))  # Yahoo reports this as a percentage
    from_info = {
        "market_cap": _num(info.get("marketCap")),
        "pe": _num(info.get("trailingPE")),
        "pb": _num(info.get("priceToBook")),
        "roe": roe * 100 if roe is not None else None,
        "eps": _num(info.get("trailingEps")),
        "dividend_yield": _num(info.get("dividendYield")),
        "book_value": _num(info.get("bookValue")),
        "debt_to_equity": dte / 100 if dte is not None else None,
    }
    fundamentals = {k: from_info[k] if from_info[k] is not None else computed[k] for k in from_info}

    quarterly = _results(t.quarterly_income_stmt, _quarter_label)
    yearly = _results(t.income_stmt, lambda c: f"FY{c.strftime('%y')}" if c.month <= 3 else c.strftime("%Y"))

    ceo = next((o.get("name") for o in info.get("companyOfficers", [])
                if "CEO" in (o.get("title") or "").upper() or "MANAGING DIRECTOR" in (o.get("title") or "").upper()),
               None)
    about = {
        "summary": info.get("longBusinessSummary"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "ceo": ceo,
        "employees": info.get("fullTimeEmployees"),
        "website": info.get("website"),
        "headquarters": ", ".join(x for x in (info.get("city"), info.get("country")) if x) or None,
        "source": "Yahoo Finance" if info.get("longBusinessSummary") else None,
    }
    if not about["summary"]:
        about.update({k: v for k, v in _wikipedia(name).items() if v})

    return {
        "ticker": ticker,
        "name": name,
        "currency": info.get("currency") or meta.get("currency") or "USD",
        "exchange": info.get("fullExchangeName") or meta.get("fullExchangeName") or meta.get("exchangeName"),
        "price": price,
        "change_pct": _pct(price, prev),
        "day_high": _num(info.get("dayHigh") or meta.get("regularMarketDayHigh")),
        "day_low": _num(info.get("dayLow") or meta.get("regularMarketDayLow")),
        "week52_high": _num(info.get("fiftyTwoWeekHigh") or meta.get("fiftyTwoWeekHigh")) or _num(hist["High"].max()),
        "week52_low": _num(info.get("fiftyTwoWeekLow") or meta.get("fiftyTwoWeekLow")) or _num(hist["Low"].min()),
        "fundamentals": fundamentals,
        "about": about,
        "quarterly": quarterly,
        "yearly": yearly,
        "_holders": {
            "insiders": _num(info.get("heldPercentInsiders")),
            "institutions": _num(info.get("heldPercentInstitutions")),
        },
    }


# ---------------------------------------------------------------- Wikipedia (company description fallback)

COMPANY_WORDS = re.compile(
    r"\b(company|companies|conglomerate|corporation|multinational|bank|banking|firm|manufacturer|maker|"
    r"producer|retailer|insurer|depository|exchange|business|enterprise|provider|holding|group)\b", re.I)
WIKI_HEADERS = {"User-Agent": "StockNewsDaily/1.0 (https://github.com/kdheerajk9/Stocknews)"}


def _wikipedia(name):
    """Short company description from Wikipedia, used when Yahoo's profile is unavailable."""
    # "HDFC Bank Limited" -> "HDFC Bank"; "Central Depository Services (India) Limited" -> "Central Depository Services"
    core = re.sub(r"\((India|I)\)|\b(Limited|Ltd|Inc|Incorporated|Corporation|Corp|plc|Co)\b\.?|,", " ", name, flags=re.I)
    core = re.sub(r"\s+", " ", core).strip()
    first_two = " ".join(core.split()[:2]).lower()
    try:
        search = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": f"{core} company", "srlimit": 8, "format": "json"},
            headers=WIKI_HEADERS, timeout=10,
        ).json()
        hits = [h["title"] for h in search.get("query", {}).get("search", [])]
        # The title must contain the company's name ...
        candidates = [h for h in hits if core.lower() in h.lower() or h.lower().startswith(first_two)]
        for title in candidates[:3]:
            page = requests.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(title.replace(" ", "_")),
                headers=WIKI_HEADERS, timeout=10,
            ).json()
            desc = page.get("description") or ""
            # ... and the page must describe a company (not e.g. the fruit "Apple").
            if not COMPANY_WORDS.search(desc + " " + (page.get("extract") or "")[:200]):
                continue
            return {
                "summary": page.get("extract"),
                "industry": desc[:1].upper() + desc[1:] if desc else None,
                "source": "Wikipedia",
                "wiki_url": page.get("content_urls", {}).get("desktop", {}).get("page"),
            }
    except (requests.RequestException, ValueError, KeyError):
        pass
    return {}


# ---------------------------------------------------------------- NSE shareholding

def _parse_shp_xbrl(xml):
    out = {}
    for element, field in SHP_FIELDS.items():
        # Only the "_ContextI" facts are the category totals; other contexts are sub-rows.
        m = re.search(
            rf'<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares[^>]*contextRef="{element}_ContextI"[^>]*>([^<]+)<',
            xml,
        )
        if m:
            value = float(m.group(1))
            # Newer filings use fractions (0.15), older ones percentages (15.0).
            out[field] = round(value * 100 if value <= 1 else value, 2)
        else:
            out[field] = None
    return out


def _nse_shareholding(ticker):
    symbol = ticker.split(".")[0]
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    resp = session.get(NSE_SHP_LIST.format(symbol=requests.utils.quote(symbol, safe="")), timeout=15)
    resp.raise_for_status()
    filings = [f for f in resp.json() if f.get("xbrl") and f.get("date")]
    # Latest filing per quarter (revisions share the same date), newest first.
    by_quarter = {}
    for f in filings:
        by_quarter.setdefault(f["date"], f)
    quarters = sorted(by_quarter.values(), key=lambda f: datetime.strptime(f["date"], "%d-%b-%Y"),
                      reverse=True)[:SHP_QUARTERS]

    def fetch(f):
        xml = session.get(f["xbrl"], timeout=20).text
        row = _parse_shp_xbrl(xml)
        if row["promoters"] is None:  # e.g. companies with no promoter group
            row["promoters"] = _num(f.get("pr_and_prgrp"))
        row["quarter"] = _quarter_label(datetime.strptime(f["date"], "%d-%b-%Y"))
        return row

    with ThreadPoolExecutor(max_workers=SHP_QUARTERS) as pool:
        rows = [r for r in pool.map(fetch, quarters) if r.get("public") is not None]
    rows.reverse()  # oldest first
    return rows


# ---------------------------------------------------------------- Insights

def _insight(impact, title, detail):
    return {"impact": impact, "title": title, "detail": detail}


def _holding_insight(rows, field, name, rising_is_positive=True):
    if len(rows) < 2 or rows[-1].get(field) is None or rows[-2].get(field) is None:
        return None
    new, old = rows[-1][field], rows[-2][field]
    diff = new - old
    if abs(diff) < 0.05:
        return None
    up = diff > 0
    impact = "positive" if up == rising_is_positive else "negative"
    return _insight(
        impact,
        f"{name} Holding {'Up' if up else 'Down'}",
        f"{name} holding {'increased' if up else 'decreased'} from {old:.2f}% to {new:.2f}% "
        f"in the {rows[-1]['quarter']} quarter.",
    )


def _insights(data):
    out = []
    q = data["quarterly"]
    if q:
        latest = q[-1]
        for field, name in (("revenue", "Revenue"), ("profit", "Profit")):
            growth = _yoy(q, field)
            if growth is not None and abs(growth) >= 1:
                up = growth > 0
                out.append(_insight(
                    "positive" if up else "negative",
                    f"{name} {'Growth' if up else 'Decline'}",
                    f"{name} {'grew' if up else 'fell'} {abs(growth):.1f}% year-on-year in the "
                    f"{latest['period']} quarter.",
                ))

    rows = data.get("shareholding", {}).get("quarters") or []
    for field, name, rising_good in (
        ("promoters", "Promoter", True),
        ("mutual_funds", "Mutual Fund", True),
        ("fii", "FII", True),
        ("retail", "Retail Investor", True),
    ):
        item = _holding_insight(rows, field, name, rising_good)
        if item:
            out.append(item)

    f = data["fundamentals"]
    if f["roe"] is not None:
        if f["roe"] >= 15:
            out.append(_insight("positive", "Strong Return on Equity",
                                f"ROE of {f['roe']:.1f}% is above the commonly used 15% benchmark."))
        elif f["roe"] < 8:
            out.append(_insight("negative", "Weak Return on Equity",
                                f"ROE of {f['roe']:.1f}% is below 8%."))
    if f["debt_to_equity"] is not None:
        if f["debt_to_equity"] <= 0.3:
            out.append(_insight("positive", "Low Debt",
                                f"Debt-to-equity is {f['debt_to_equity']:.2f}, meaning little borrowing."))
        elif f["debt_to_equity"] >= 1.5:
            out.append(_insight("negative", "High Debt",
                                f"Debt-to-equity is {f['debt_to_equity']:.2f}, meaning heavy borrowing."))
    return out


# ---------------------------------------------------------------- Public entry point

def get_company(ticker):
    data = _profile(ticker)
    holders = data.pop("_holders")

    shareholding = {"source": None, "quarters": [], "snapshot": None}
    if ticker.endswith((".NS", ".BO")):
        try:
            shareholding["quarters"] = _nse_shareholding(ticker)
            shareholding["source"] = "NSE shareholding-pattern filings"
        except (requests.RequestException, ValueError):
            shareholding["error"] = "Shareholding data from NSE is unavailable right now."
    elif holders["institutions"] is not None:
        insiders = (holders["insiders"] or 0) * 100
        inst = holders["institutions"] * 100
        shareholding["snapshot"] = {
            "insiders": round(insiders, 2),
            "institutions": round(inst, 2),
            "public": round(max(0.0, 100 - insiders - inst), 2),
        }
        shareholding["source"] = "Yahoo Finance"
    data["shareholding"] = shareholding

    data["insights"] = _insights(data)
    for r in data["quarterly"] + data["yearly"]:
        del r["date"]  # only needed for year-on-year matching
    return data
