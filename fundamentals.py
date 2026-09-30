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


def _profile(ticker):
    t = yf.Ticker(ticker)
    info = t.info or {}
    if not info.get("longName") and not info.get("shortName"):
        raise LookupError(f"No data found for {ticker}. Check the symbol (e.g. RELIANCE.NS, AAPL).")

    price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
    prev = _num(info.get("previousClose") or info.get("regularMarketPreviousClose"))
    eps = _num(info.get("trailingEps"))
    book = _num(info.get("bookValue"))
    roe = _num(info.get("returnOnEquity"))
    if roe is None and eps and book:
        roe = eps / book  # ROE ~ EPS / book value per share
    div = _num(info.get("dividendYield"))
    dte = _num(info.get("debtToEquity"))  # Yahoo reports this as a percentage

    quarterly = _results(t.quarterly_income_stmt, _quarter_label)
    yearly = _results(t.income_stmt, lambda c: f"FY{c.strftime('%y')}" if c.month <= 3 else c.strftime("%Y"))

    ceo = next((o.get("name") for o in info.get("companyOfficers", [])
                if "CEO" in (o.get("title") or "").upper() or "MANAGING DIRECTOR" in (o.get("title") or "").upper()),
               None)
    return {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName"),
        "currency": info.get("currency") or "USD",
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "price": price,
        "change_pct": _pct(price, prev),
        "day_high": _num(info.get("dayHigh")),
        "day_low": _num(info.get("dayLow")),
        "week52_high": _num(info.get("fiftyTwoWeekHigh")),
        "week52_low": _num(info.get("fiftyTwoWeekLow")),
        "fundamentals": {
            "market_cap": _num(info.get("marketCap")),
            "pe": _num(info.get("trailingPE")),
            "pb": _num(info.get("priceToBook")),
            "roe": roe * 100 if roe is not None else None,
            "eps": eps,
            "dividend_yield": div,
            "book_value": book,
            "debt_to_equity": dte / 100 if dte is not None else None,
            "face_value": None,
        },
        "about": {
            "summary": info.get("longBusinessSummary"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "ceo": ceo,
            "employees": info.get("fullTimeEmployees"),
            "website": info.get("website"),
            "headquarters": ", ".join(x for x in (info.get("city"), info.get("country")) if x) or None,
        },
        "quarterly": quarterly,
        "yearly": yearly,
        "_holders": {
            "insiders": _num(info.get("heldPercentInsiders")),
            "institutions": _num(info.get("heldPercentInstitutions")),
        },
    }


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
