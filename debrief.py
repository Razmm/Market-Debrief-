#!/usr/bin/env python3
"""Build and email a morning/evening market debrief.

This script is designed for GitHub Actions, but it can also run locally when the
same environment variables are present.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import smtplib
import sys
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.message import EmailMessage
from zoneinfo import ZoneInfo


HOLDINGS = ["MSFT", "UNH", "ODFL", "MU", "ADBE", "PYPL", "FSELX", "BE", "MRVL", "NVDA", "GOOG"]
MARKET_SYMBOLS = {
    "S&P 500 ETF": "SPY",
    "Nasdaq 100 ETF": "QQQ",
    "Dow ETF": "DIA",
    "Russell 2000 ETF": "IWM",
    "10Y Treasury Yield": "^TNX",
    "WTI Oil": "CL=F",
    "Gold": "GC=F",
    "Bitcoin": "BTC-USD",
}
DISPLAY_NAMES = {
    **MARKET_SYMBOLS,
    "MSFT": "Microsoft",
    "UNH": "UnitedHealth Group",
    "ODFL": "Old Dominion Freight Line",
    "MU": "Micron Technology",
    "ADBE": "Adobe",
    "PYPL": "PayPal",
    "FSELX": "Fidelity Select Semiconductors Portfolio",
    "BE": "Bloom Energy",
    "MRVL": "Marvell Technology",
    "NVDA": "Nvidia",
    "GOOG": "Alphabet",
}
USER_AGENT = "Mozilla/5.0 (compatible; market-debrief/1.0)"


@dataclass
class Quote:
    symbol: str
    name: str
    price: float | None
    change_pct: float | None
    market_state: str | None


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    symbols: list[str]
    score: int


@dataclass
class CalendarEvent:
    date: dt.date
    time_et: str
    country: str
    event: str
    consensus: str
    previous: str
    score: int
    sort_time: int


@dataclass
class EarningsEvent:
    symbol: str
    name: str
    date: dt.date
    time: str
    eps_forecast: str
    source: str


def fetch_json(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{decimals}f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text in {"&nbsp;", "nbsp", "N/A", "-"} else text


def week_bounds(now: dt.datetime) -> tuple[dt.date, dt.date]:
    start = now.date() - dt.timedelta(days=now.weekday())
    end = start + dt.timedelta(days=4)
    return start, end


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    days = (end - start).days
    return [start + dt.timedelta(days=offset) for offset in range(days + 1)]


def format_event_day(day: dt.date) -> str:
    return day.strftime("%a %-m/%-d")


def nasdaq_calendar_json(calendar: str, day: dt.date) -> dict:
    date_text = day.isoformat()
    url = f"https://api.nasdaq.com/api/calendar/{calendar}?date={date_text}"
    return fetch_json(url)


def gmt_to_et_label(day: dt.date, gmt_text: str) -> str:
    text = clean_text(gmt_text)
    if not text or not re.match(r"^\d{1,2}:\d{2}$", text):
        return "time n/a"
    hour, minute = [int(part) for part in text.split(":", 1)]
    utc_time = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.timezone.utc)
    et_time = utc_time.astimezone(ZoneInfo("America/New_York"))
    return et_time.strftime("%-I:%M %p ET")


def gmt_to_et_parts(day: dt.date, gmt_text: object) -> tuple[dt.date, str, int]:
    text = clean_text(gmt_text)
    if not text or not re.match(r"^\d{1,2}:\d{2}$", text):
        return day, "time n/a", 24 * 60
    hour, minute = [int(part) for part in text.split(":", 1)]
    utc_time = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.timezone.utc)
    et_time = utc_time.astimezone(ZoneInfo("America/New_York"))
    return et_time.date(), et_time.strftime("%-I:%M %p ET"), et_time.hour * 60 + et_time.minute


def market_calendar_time_parts(day: dt.date, time_text: object) -> tuple[dt.date, str, int]:
    text = clean_text(time_text)
    if not text or not re.match(r"^\d{1,2}:\d{2}$", text):
        return day, "time n/a", 24 * 60
    hour, minute = [int(part) for part in text.split(":", 1)]
    meridiem = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return day, f"{display_hour}:{minute:02d} {meridiem} ET", hour * 60 + minute


def economic_event_score(country: str, event: str) -> int:
    text = f"{country} {event}".lower()
    score = 0
    if country == "United States":
        score += 5
    elif country in {"China", "Euro Zone", "Germany", "United Kingdom", "Japan"}:
        score += 2

    keyword_scores = {
        "fomc": 7,
        "federal reserve": 7,
        "fed ": 6,
        "powell": 6,
        "cpi": 7,
        "pce": 7,
        "payroll": 7,
        "nonfarm": 7,
        "unemployment": 6,
        "jobless claims": 5,
        "jolts": 5,
        "gdp": 6,
        "ism": 6,
        "pmi": 5,
        "retail sales": 5,
        "consumer confidence": 5,
        "durable goods": 4,
        "housing": 4,
        "treasury": 4,
        "auction": 2,
        "beige book": 5,
        "minutes": 5,
        "ecb": 5,
        "boj": 4,
        "opec": 4,
    }
    for keyword, points in keyword_scores.items():
        if keyword in text:
            score += points
    if "bill auction" in text and not any(term in text for term in ["10-year", "20-year", "30-year"]):
        score -= 3
    if "dallas fed" in text or "richmond fed" in text or "chicago fed" in text:
        score -= 3
    return score


def weekly_economic_calendar(now: dt.datetime, limit: int = 8) -> list[CalendarEvent]:
    start, end = week_bounds(now)
    events: list[CalendarEvent] = []
    seen: set[tuple[dt.date, str, str]] = set()

    for day in date_range(start, end):
        try:
            data = nasdaq_calendar_json("economicevents", day)
        except Exception as exc:
            print(f"economic calendar fetch failed for {day}: {exc}", file=sys.stderr)
            continue
        for row in data.get("data", {}).get("rows", []) or []:
            country = clean_text(row.get("country"))
            event = clean_text(row.get("eventName"))
            if not country or not event:
                continue
            event_lower = event.lower()
            if country == "Germany" and not event_lower.startswith("german"):
                continue
            ignored_event_parts = [
                "n.s.a",
                "private nonfarm",
                "government payroll",
                "manufacturing payroll",
                "u6 unemployment",
                "ism manufacturing employment",
                "ism manufacturing new orders",
                "ism manufacturing prices",
                "continuing jobless",
                "4-week",
                "reserve balances",
            ]
            if any(part in event_lower for part in ignored_event_parts):
                continue
            score = economic_event_score(country, event)
            major_global_countries = {"China", "Euro Zone", "Germany", "United Kingdom", "Japan"}
            threshold = 9 if country == "United States" or country in major_global_countries else 12
            if score < threshold:
                continue
            event_day, time_et, sort_time = market_calendar_time_parts(day, row.get("gmt", ""))
            key = (event_day, country, event)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                CalendarEvent(
                    date=event_day,
                    time_et=time_et,
                    country=country,
                    event=event,
                    consensus=clean_text(row.get("consensus")) or "n/a",
                    previous=clean_text(row.get("previous")) or "n/a",
                    score=score,
                    sort_time=sort_time,
                )
            )

    events.sort(key=lambda item: (-item.score, item.date, item.sort_time, item.event))
    selected = events[:limit]
    selected.sort(key=lambda item: (item.date, item.sort_time, -item.score, item.event))
    return selected


def normalize_earnings_time(value: str) -> str:
    text = clean_text(value).lower().replace("time-", "").replace("-", " ")
    if text == "pre market":
        return "before open"
    if text == "after hours":
        return "after close"
    if text == "not supplied":
        return "time n/a"
    return text or "time n/a"


def weekly_holding_earnings(now: dt.datetime) -> list[EarningsEvent]:
    start, end = week_bounds(now)
    holding_set = {symbol for symbol in HOLDINGS if symbol != "FSELX"}
    events: list[EarningsEvent] = []

    for day in date_range(start, end):
        try:
            data = nasdaq_calendar_json("earnings", day)
        except Exception as exc:
            print(f"earnings calendar fetch failed for {day}: {exc}", file=sys.stderr)
            continue
        for row in data.get("data", {}).get("rows", []) or []:
            symbol = clean_text(row.get("symbol")).upper()
            if symbol not in holding_set:
                continue
            events.append(
                EarningsEvent(
                    symbol=symbol,
                    name=clean_text(row.get("name")) or DISPLAY_NAMES.get(symbol, symbol),
                    date=day,
                    time=normalize_earnings_time(row.get("time", "")),
                    eps_forecast=clean_text(row.get("epsForecast")) or "n/a",
                    source="Nasdaq",
                )
            )

    events.sort(key=lambda item: (item.date, item.symbol))
    return events


def parse_yfinance_earnings_date(value: object) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, list) and value:
        return parse_yfinance_earnings_date(value[0])
    if isinstance(value, tuple) and value:
        return parse_yfinance_earnings_date(value[0])
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except Exception:
            return None
    return None


def upcoming_holding_earnings(now: dt.datetime, limit: int = 8) -> list[EarningsEvent]:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return []

    today = now.date()
    max_day = today + dt.timedelta(days=120)
    events: list[EarningsEvent] = []
    for symbol in HOLDINGS:
        if symbol == "FSELX":
            continue
        try:
            calendar = yf.Ticker(symbol).calendar
        except Exception as exc:
            print(f"earnings date fetch failed for {symbol}: {exc}", file=sys.stderr)
            continue
        if not isinstance(calendar, dict):
            continue
        earnings_date = parse_yfinance_earnings_date(calendar.get("Earnings Date"))
        if not earnings_date or earnings_date < today or earnings_date > max_day:
            continue
        eps_average = calendar.get("Earnings Average")
        eps_forecast = fmt_num(float(eps_average), 2) if isinstance(eps_average, (int, float)) else "n/a"
        events.append(
            EarningsEvent(
                symbol=symbol,
                name=DISPLAY_NAMES.get(symbol, symbol),
                date=earnings_date,
                time="time n/a",
                eps_forecast=eps_forecast,
                source="Yahoo Finance",
            )
        )

    events.sort(key=lambda item: (item.date, item.symbol))
    return events[:limit]


def get_quotes(symbols: list[str]) -> dict[str, Quote]:
    yfinance_quotes = get_quotes_yfinance(symbols)
    if yfinance_quotes:
        return yfinance_quotes

    if not symbols:
        return {}
    encoded = urllib.parse.quote(",".join(symbols))
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={encoded}"
    try:
        data = fetch_json(url)
    except Exception as exc:
        print(f"quote fetch failed: {exc}", file=sys.stderr)
        return {}

    quotes: dict[str, Quote] = {}
    for item in data.get("quoteResponse", {}).get("result", []):
        symbol = item.get("symbol")
        if not symbol:
            continue
        quotes[symbol] = Quote(
            symbol=symbol,
            name=item.get("shortName") or item.get("longName") or symbol,
            price=item.get("regularMarketPrice"),
            change_pct=item.get("regularMarketChangePercent"),
            market_state=item.get("marketState"),
        )
    return quotes


def get_quotes_yfinance(symbols: list[str]) -> dict[str, Quote]:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}

    quotes: dict[str, Quote] = {}
    try:
        data = yf.download(
            tickers=" ".join(symbols),
            period="7d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        print(f"yfinance batch quote failed: {exc}", file=sys.stderr)
        return {}

    if data.empty:
        return {}

    for symbol in symbols:
        try:
            if len(symbols) == 1:
                series = data["Close"]
            else:
                series = data[(symbol, "Close")]
        except Exception:
            continue

        closes = [float(value) for value in series.dropna().tolist()]
        if not closes:
            continue
        price = closes[-1]
        previous = closes[-2] if len(closes) > 1 else None
        change_pct = ((price - previous) / previous * 100) if previous else None
        quotes[symbol] = Quote(
            symbol=symbol,
            name=DISPLAY_NAMES.get(symbol, symbol),
            price=price,
            change_pct=change_pct,
            market_state=None,
        )
    return quotes


def rss_items(url: str, limit: int = 10) -> list[tuple[str, str, str]]:
    try:
        text = fetch_text(url)
        root = ET.fromstring(text)
    except Exception as exc:
        print(f"rss fetch failed for {url}: {exc}", file=sys.stderr)
        return []

    rows: list[tuple[str, str, str]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip() or source_from_url(link)
        if title and link:
            rows.append((html.unescape(title), link, html.unescape(source)))
        if len(rows) >= limit:
            break
    return rows


def rss_headlines(symbol: str | None = None, limit: int = 5) -> list[tuple[str, str]]:
    if symbol:
        params = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{params}"
    else:
        url = "https://finance.yahoo.com/news/rssindex"
    return [(title, link) for title, link, _source in rss_items(url, limit=limit)]


def source_from_url(link: str) -> str:
    try:
        host = urllib.parse.urlparse(link).netloc.lower()
    except Exception:
        return "source"
    host = host.removeprefix("www.")
    if "reuters" in host:
        return "Reuters"
    if "cnbc" in host:
        return "CNBC"
    if "marketwatch" in host:
        return "MarketWatch"
    if "bloomberg" in host:
        return "Bloomberg"
    if "apnews" in host:
        return "AP"
    if "finance.yahoo" in host:
        return "Yahoo Finance"
    if "investors.com" in host:
        return "IBD"
    if "barrons" in host:
        return "Barron's"
    return host or "source"


def simplify_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def clean_news_title(title: str, source: str) -> str:
    cleaned = html.unescape(title).strip()
    if source:
        cleaned = re.sub(rf"\s+-\s+{re.escape(source)}\s*$", "", cleaned, flags=re.I)
    return cleaned


def google_news_url(query: str) -> str:
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return f"https://news.google.com/rss/search?{params}"


def impacted_symbols(title: str) -> list[str]:
    text = title.lower()
    impacts: list[str] = []
    rules = {
        "MSFT": ["microsoft", "azure", "cloud", "openai", "software"],
        "UNH": ["unitedhealth", "healthcare", "medicare", "managed care", "insurance"],
        "ODFL": ["fedex", "freight", "trucking", "logistics", "industrial", "transport"],
        "MU": ["micron", "memory", "dram", "hbm", "chip", "semiconductor"],
        "ADBE": ["adobe", "creative", "software", "ai agent", "agentic"],
        "PYPL": ["paypal", "fintech", "payments", "consumer credit"],
        "FSELX": ["chip", "semiconductor", "nvidia", "micron", "marvell", "ai"],
        "BE": ["bloom energy", "fuel cell", "clean energy", "hydrogen", "power"],
        "MRVL": ["marvell", "networking", "custom silicon", "asic", "chip"],
        "NVDA": ["nvidia", "gpu", "ai chip", "semiconductor", "data center"],
        "GOOG": ["alphabet", "google", "search", "cloud", "gemini", "youtube"],
    }
    for symbol, terms in rules.items():
        if any(term in text for term in terms):
            impacts.append(symbol)
    return impacts[:5]


def news_score(title: str, source: str) -> int:
    text = f"{title} {source}".lower()
    score = 0
    source_weights = {
        "reuters": 35,
        "cnbc": 30,
        "bloomberg": 30,
        "ap": 25,
        "marketwatch": 22,
        "barron": 20,
        "yahoo finance": 12,
        "investor": 10,
    }
    for source_key, weight in source_weights.items():
        if source_key in text:
            score += weight
    if not any(source_key in text for source_key in source_weights):
        score -= 18

    high_value_terms = [
        "stock market",
        "s&p 500",
        "nasdaq",
        "dow",
        "futures",
        "fed",
        "federal reserve",
        "treasury",
        "yields",
        "inflation",
        "jobs",
        "payrolls",
        "earnings",
        "oil",
        "geopolitical",
        "ai",
        "semiconductor",
        "chip",
        "nvidia",
        "microsoft",
        "alphabet",
    ]
    for term in high_value_terms:
        if term in text:
            score += 8

    low_value_terms = [
        "millionaire",
        "retirement",
        "social security",
        "credit card",
        "mortgage rates",
        "store",
        "fashion",
        "pump",
        "buy now",
        "zacks",
        "morning squawk",
        "march jobs",
        "target earnings",
        "opening bell",
        "trump accounts",
        "what happens to the stock market",
    ]
    for term in low_value_terms:
        if term in text:
            score -= 18
    return score


def news_category(title: str) -> str:
    text = title.lower()
    if re.search(r"\bfed\b|federal reserve|treasury|yield|inflation|jobs|payroll", text):
        return "rates/macro"
    if any(term in text for term in ["oil", "crude", "energy", "iran", "geopolitical"]):
        return "oil/geopolitics"
    if any(term in text for term in ["ai", "chip", "semiconductor", "nvidia", "micron", "marvell"]):
        return "ai/semis"
    if any(term in text for term in ["earnings", "revenue", "profit", "guidance"]):
        return "earnings"
    if any(term in text for term in ["s&p", "nasdaq", "dow", "stock market", "stocks"]):
        return "market"
    return "other"


def market_news_articles(limit: int = 5) -> list[NewsItem]:
    urls = [
        "https://finance.yahoo.com/news/rssindex",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.cnbc.com/id/15839135/device/rss/rss.html",
        "https://www.marketwatch.com/rss/topstories",
        google_news_url("stock market today S&P 500 Nasdaq Dow Reuters CNBC when:1d"),
        google_news_url("Federal Reserve Treasury yields stocks market Reuters CNBC when:1d"),
        google_news_url("oil prices stocks market Reuters CNBC when:1d"),
        google_news_url("AI semiconductor stocks Nvidia Micron Marvell market when:1d"),
        google_news_url("market close stocks earnings economy jobs inflation when:1d"),
    ]
    items: list[NewsItem] = []
    seen: set[str] = set()
    for url in urls:
        for title, link, source in rss_items(url, limit=8):
            clean_title = clean_news_title(title, source)
            key = simplify_title(clean_title)
            if not key or key in seen:
                continue
            seen.add(key)
            symbols = impacted_symbols(clean_title)
            items.append(
                NewsItem(
                    title=clean_title,
                    link=link,
                    source=source,
                    symbols=symbols,
                    score=news_score(clean_title, source),
                )
            )

    items.sort(key=lambda item: item.score, reverse=True)
    selected: list[NewsItem] = []
    category_counts: dict[str, int] = {}
    for item in items:
        category = news_category(item.title)
        max_per_category = 2 if category in {"market", "rates/macro"} else 1
        if category_counts.get(category, 0) >= max_per_category:
            continue
        selected.append(item)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= limit:
            return selected

    for item in items:
        if item not in selected:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def finviz_low_peg(limit: int = 5) -> list[dict[str, str]]:
    """Best-effort PEG screen from Finviz sorted by PEG ascending.

    Finviz is used as a public screener source. If its page shape changes or it
    blocks automated access, the report shows a clear unavailable note.
    """
    filters = "geo_usa,cap_midover,fa_peg_pos"
    metadata = finviz_metadata(filters)
    url = f"https://finviz.com/screener.ashx?v=121&f={filters}&ft=4&o=peg"
    try:
        page = fetch_text(url)
    except Exception as exc:
        print(f"PEG screen fetch failed: {exc}", file=sys.stderr)
        return []

    rows: list[dict[str, str]] = []
    for match in re.finditer(r"<tr[^>]*class=\"(?:styled-row[^\">]*|table-dark-row-cp|table-light-row-cp)[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", match.group(1), re.S)
        clean = [re.sub(r"\s+", " ", re.sub(r"<.*?>", "", cell)).strip() for cell in cells]
        if len(clean) < 18 or clean[1] == "Ticker":
            continue
        peg = clean[5] if len(clean) > 5 else "-"
        if peg in {"-", ""}:
            continue
        try:
            peg_value = float(peg)
        except ValueError:
            continue
        if peg_value <= 0:
            continue
        ticker_cell = cells[1] if len(cells) > 1 else ""
        company_match = re.search(r'data-boxover-company="([^"]+)"', ticker_cell)
        industry_match = re.search(r'data-boxover-industry="([^"]+)"', ticker_cell)
        meta = metadata.get(clean[1], {})
        rows.append(
            {
                "ticker": clean[1],
                "company": meta.get("company") or (html.unescape(company_match.group(1)) if company_match else clean[1]),
                "sector": meta.get("sector") or "n/a",
                "industry": meta.get("industry") or (html.unescape(industry_match.group(1)) if industry_match else "n/a"),
                "peg": f"{peg_value:.2f}",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def finviz_metadata(filters: str) -> dict[str, dict[str, str]]:
    url = f"https://finviz.com/screener.ashx?v=152&f={filters}&ft=4&o=peg"
    try:
        page = fetch_text(url)
    except Exception as exc:
        print(f"PEG metadata fetch failed: {exc}", file=sys.stderr)
        return {}

    metadata: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"<tr[^>]*class=\"(?:styled-row[^\">]*)[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", match.group(1), re.S)
        clean = [re.sub(r"\s+", " ", re.sub(r"<.*?>", "", cell)).strip() for cell in cells]
        if len(clean) < 6 or clean[1] == "Ticker":
            continue
        metadata[clean[1]] = {
            "company": html.unescape(clean[2]),
            "sector": html.unescape(clean[3]),
            "industry": html.unescape(clean[4]),
        }
    return metadata


def resolve_kind(now: dt.datetime, requested: str) -> str | None:
    if requested != "auto":
        return requested
    if now.weekday() >= 5:
        return None
    if now.hour == 8:
        return "morning"
    if now.hour == 17:
        return "evening"
    return None


def sorted_quotes(quotes: dict[str, Quote], symbols: list[str]) -> list[Quote]:
    rows = [quotes[symbol] for symbol in symbols if symbol in quotes and quotes[symbol].change_pct is not None]
    return sorted(rows, key=lambda quote: quote.change_pct or 0, reverse=True)


def move_label(quote: Quote) -> str:
    return f"{quote.symbol}: {quote.name} - {fmt_num(quote.price)} ({fmt_pct(quote.change_pct)})"


def why_news_matters(item: NewsItem) -> str:
    if item.symbols:
        return f"Read-through: relevant to {', '.join(item.symbols)}."
    text = item.title.lower()
    if re.search(r"\bfed\b|federal reserve|yield|treasury|inflation|jobs", text):
        return "Read-through: affects rates, valuation multiples, and growth-stock appetite."
    if any(term in text for term in ["oil", "energy", "geopolitical"]):
        return "Read-through: watch inflation risk, consumer pressure, and risk appetite."
    if any(term in text for term in ["nasdaq", "s&p", "dow", "market"]):
        return "Read-through: broad tape signal for your watchlist."
    return "Read-through: market sentiment item to keep on the radar."


def theme_read(market_quotes: dict[str, Quote], holding_quotes: dict[str, Quote]) -> list[str]:
    lines: list[str] = []
    spy = market_quotes.get("SPY")
    qqq = market_quotes.get("QQQ")
    iwm = market_quotes.get("IWM")
    oil = market_quotes.get("CL=F")

    if qqq and spy and qqq.change_pct is not None and spy.change_pct is not None:
        if qqq.change_pct > spy.change_pct + 0.5:
            lines.append("Growth/AI led the tape: Nasdaq outperformed the S&P 500.")
        elif spy.change_pct > qqq.change_pct + 0.5:
            lines.append("The tape favored broader large caps over Nasdaq-heavy growth.")
    if iwm and spy and iwm.change_pct is not None and spy.change_pct is not None:
        if iwm.change_pct < spy.change_pct - 0.75:
            lines.append("Small caps lagged, so breadth was weaker than the headline indexes suggest.")
        elif iwm.change_pct > spy.change_pct + 0.75:
            lines.append("Small caps outperformed, which points to broader risk appetite.")
    if oil and oil.change_pct is not None and abs(oil.change_pct) >= 1.5:
        direction = "rose" if oil.change_pct > 0 else "fell"
        lines.append(f"Oil {direction} {fmt_pct(oil.change_pct)}, an important macro input for inflation and rates.")

    movers = sorted_quotes(holding_quotes, HOLDINGS)
    if movers:
        winners = [quote.symbol for quote in movers[:3]]
        losers = [quote.symbol for quote in sorted(movers, key=lambda quote: quote.change_pct or 0)[:3]]
        lines.append(f"Your strongest names: {', '.join(winners)}.")
        lines.append(f"Your weakest names: {', '.join(losers)}.")

    semi_symbols = ["MU", "MRVL", "NVDA", "FSELX"]
    semi_moves = [holding_quotes[s].change_pct for s in semi_symbols if s in holding_quotes and holding_quotes[s].change_pct is not None]
    if len(semi_moves) >= 2:
        avg = sum(semi_moves) / len(semi_moves)
        lines.append(f"Semiconductor/AI basket average move: {fmt_pct(avg)} across {', '.join(semi_symbols)}.")

    return lines or ["No single dominant theme stood out from the available quote data."]


def alert_lines(holding_quotes: dict[str, Quote], peg_rows: list[dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for quote in sorted_quotes(holding_quotes, HOLDINGS):
        if quote.change_pct is None:
            continue
        if quote.change_pct >= 5:
            lines.append(f"{quote.symbol}: up {fmt_pct(quote.change_pct)}; check if the move is news-driven or short-term momentum.")
        elif quote.change_pct <= -3:
            lines.append(f"{quote.symbol}: down {fmt_pct(quote.change_pct)}; check for company-specific news or sector pressure.")

    peg_symbols = {row["ticker"] for row in peg_rows}
    overlap = [symbol for symbol in HOLDINGS if symbol in peg_symbols]
    if overlap:
        lines.append(f"Low-PEG overlap in your holdings: {', '.join(overlap)}. Treat as a valuation flag, not a buy signal.")
    if any(symbol in holding_quotes for symbol in ["MU", "MRVL", "NVDA", "FSELX"]):
        lines.append("Concentration watch: several holdings are tied to the same AI/semiconductor factor.")
    return lines[:6] or ["No major price-move alerts triggered from the available data."]


def build_report(kind: str, now: dt.datetime) -> str:
    market_quotes = get_quotes(list(MARKET_SYMBOLS.values()))
    holding_quotes = get_quotes(HOLDINGS)
    market_news = market_news_articles(limit=5)
    weekly_events = weekly_economic_calendar(now, limit=12)
    weekly_earnings = weekly_holding_earnings(now)
    upcoming_earnings = upcoming_holding_earnings(now, limit=8)
    holding_news = {symbol: rss_headlines(symbol=symbol, limit=1) for symbol in HOLDINGS}
    peg_rows = finviz_low_peg(limit=5)

    title = "Morning Market Debrief" if kind == "morning" else "Evening Market Debrief"
    setup_label = "Pre-market setup" if kind == "morning" else "Market close recap"
    week_start, week_end = week_bounds(now)

    lines = [
        title.upper(),
        now.strftime("Generated %A, %B %-d, %Y at %-I:%M %p ET"),
        "",
        "TOP 5 MARKET NEWS ARTICLES",
    ]

    if market_news:
        for index, item in enumerate(market_news, start=1):
            lines.append(f"{index}. {item.title} - {item.source}")
            lines.append(f"   {item.link}")
            lines.append(f"   {why_news_matters(item)}")
    else:
        lines.append("- Market news feed unavailable in this run.")

    lines.extend(["", f"THIS WEEK'S MARKET CALENDAR ({format_event_day(week_start)}-{format_event_day(week_end)})"])
    if weekly_events:
        for event in weekly_events:
            details = []
            if event.consensus != "n/a":
                details.append(f"consensus {event.consensus}")
            if event.previous != "n/a":
                details.append(f"prior {event.previous}")
            suffix = f" ({'; '.join(details)})" if details else ""
            lines.append(f"- {format_event_day(event.date)} {event.time_et}: {event.country} - {event.event}{suffix}")
    else:
        lines.append("- Major economic calendar feed unavailable or no high-priority events found.")

    lines.extend(["", "YOUR HOLDING EARNINGS CALENDAR"])
    if weekly_earnings:
        lines.append("This week:")
        for item in weekly_earnings:
            lines.append(
                f"- {format_event_day(item.date)} {item.time}: {item.symbol} - {item.name} "
                f"(EPS estimate {item.eps_forecast}, source {item.source})"
            )
    else:
        lines.append("- No earnings dates found for your individual stock holdings this week.")

    if upcoming_earnings:
        weekly_keys = {(item.symbol, item.date) for item in weekly_earnings}
        next_items = [item for item in upcoming_earnings if (item.symbol, item.date) not in weekly_keys]
        if next_items:
            lines.append("Next known holding dates:")
            for item in next_items[:6]:
                lines.append(
                    f"- {format_event_day(item.date)}: {item.symbol} - {item.name} "
                    f"(EPS estimate {item.eps_forecast}, source {item.source})"
                )
    else:
        lines.append("- Next holding earnings dates unavailable in this run.")

    lines.extend(["", setup_label.upper()])

    for label, symbol in MARKET_SYMBOLS.items():
        quote = market_quotes.get(symbol)
        if quote:
            lines.append(f"- {label}: {fmt_num(quote.price)} ({fmt_pct(quote.change_pct)})")
        else:
            lines.append(f"- {label}: n/a")

    lines.extend(["", "PORTFOLIO PULSE"])
    for line in theme_read(market_quotes, holding_quotes):
        lines.append(f"- {line}")

    movers = sorted_quotes(holding_quotes, HOLDINGS)
    lines.extend(["", "YOUR BIGGEST MOVERS"])
    if movers:
        for quote in movers[:5]:
            lines.append(f"- {move_label(quote)}")
        losers = sorted(movers, key=lambda quote: quote.change_pct or 0)[:3]
        lines.append("Weakest names:")
        for quote in losers:
            lines.append(f"- {move_label(quote)}")
    else:
        lines.append("- Holding quote data unavailable in this run.")

    lines.extend(["", "YOUR HOLDINGS"])
    for symbol in HOLDINGS:
        quote = holding_quotes.get(symbol)
        if quote:
            lines.append(f"- {symbol}: {quote.name} - {fmt_num(quote.price)} ({fmt_pct(quote.change_pct)})")
        else:
            lines.append(f"- {symbol}: quote unavailable")

    lines.extend(["", "HOLDINGS NEWS"])
    for symbol in HOLDINGS:
        items = holding_news.get(symbol, [])
        if items:
            title_text, link = items[0]
            lines.append(f"- {symbol}: {title_text}")
            lines.append(f"  {link}")
        else:
            lines.append(f"- {symbol}: no fresh linked headline found in this run.")

    lines.extend(["", "ALERTS AND RISK FLAGS"])
    for line in alert_lines(holding_quotes, peg_rows):
        lines.append(f"- {line}")

    lines.extend(["", "TOP 5 LOWEST POSITIVE PEG SCREEN"])
    if peg_rows:
        for index, row in enumerate(peg_rows, start=1):
            sector = row["sector"]
            industry = row["industry"]
            lines.append(
                f"{index}. {row['ticker']} - {row['company']} - PEG {row['peg']} - {sector} / {industry}"
            )
        lines.append("Source/date: Finviz screener, pulled at report time.")
        lines.append("Caution: PEG can be distorted by one-time earnings, cyclical earnings, stale estimates, or tiny growth assumptions.")
    else:
        lines.append("- PEG screen unavailable in this run. Check Finviz/Yahoo manually if needed.")

    if kind == "morning":
        lines.extend(
            [
                "",
                "WHAT TO WATCH TODAY",
                "- Which of the top news items actually moves futures and rates after the open.",
                "- Nasdaq versus Russell 2000: leadership quality and breadth.",
                "- Treasury yields and oil, since both can drive valuation and inflation risk.",
                "- AI/chip leadership across NVDA, MU, MRVL, and FSELX.",
                "- Any company-specific headlines listed above that develop during the session.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "WHAT TO WATCH TOMORROW",
                "- Whether today's biggest market headlines keep driving futures overnight.",
                "- Whether Nasdaq leadership broadens or stays concentrated in mega-cap/AI.",
                "- Overnight macro/geopolitical headlines, especially oil and rates.",
                "- Any earnings, analyst actions, or guidance changes tied to your holdings.",
                "- Reversals in today's biggest holding movers.",
            ]
        )

    lines.extend(["", "Not financial advice."])
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    sender = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipient = os.environ.get("EMAIL_TO", sender)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["auto", "morning", "evening"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = dt.datetime.now(ZoneInfo("America/New_York"))
    kind = resolve_kind(now, args.kind)
    if kind is None:
        print(f"No debrief due at {now.isoformat()}")
        return

    body = build_report(kind, now)
    subject_prefix = "Morning" if kind == "morning" else "Evening"
    subject = f"{subject_prefix} Market Debrief - {now.strftime('%b %-d, %Y')}"

    if args.dry_run:
        print(body)
        return

    send_email(subject, body)
    print(f"Sent {kind} market debrief email.")


if __name__ == "__main__":
    main()
