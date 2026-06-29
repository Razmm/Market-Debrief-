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


def rss_headlines(symbol: str | None = None, limit: int = 5) -> list[tuple[str, str]]:
    if symbol:
        params = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{params}"
    else:
        url = "https://finance.yahoo.com/news/rssindex"

    try:
        text = fetch_text(url)
        root = ET.fromstring(text)
    except Exception as exc:
        print(f"rss fetch failed for {symbol or 'market'}: {exc}", file=sys.stderr)
        return []

    rows: list[tuple[str, str]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            rows.append((html.unescape(title), link))
        if len(rows) >= limit:
            break
    return rows


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


def build_report(kind: str, now: dt.datetime) -> str:
    market_quotes = get_quotes(list(MARKET_SYMBOLS.values()))
    holding_quotes = get_quotes(HOLDINGS)
    headlines = rss_headlines(limit=5)
    peg_rows = finviz_low_peg(limit=5)

    title = "Morning Market Debrief" if kind == "morning" else "Evening Market Debrief"
    setup_label = "Pre-market setup" if kind == "morning" else "Market close recap"

    lines = [
        title.upper(),
        now.strftime("Generated %A, %B %-d, %Y at %-I:%M %p ET"),
        "",
        setup_label.upper(),
    ]

    for label, symbol in MARKET_SYMBOLS.items():
        quote = market_quotes.get(symbol)
        if quote:
            lines.append(f"- {label}: {fmt_num(quote.price)} ({fmt_pct(quote.change_pct)})")
        else:
            lines.append(f"- {label}: n/a")

    lines.extend(["", "TOP MARKET NEWS"])
    if headlines:
        for title_text, link in headlines:
            lines.append(f"- {title_text} ({link})")
    else:
        lines.append("- News feed unavailable in this run.")

    lines.extend(["", "YOUR HOLDINGS"])
    for symbol in HOLDINGS:
        quote = holding_quotes.get(symbol)
        if quote:
            lines.append(f"- {symbol}: {quote.name} - {fmt_num(quote.price)} ({fmt_pct(quote.change_pct)})")
        else:
            lines.append(f"- {symbol}: quote unavailable")

    lines.extend(["", "HOLDINGS HEADLINES"])
    for symbol in HOLDINGS:
        items = rss_headlines(symbol=symbol, limit=1)
        if items:
            title_text, link = items[0]
            lines.append(f"- {symbol}: {title_text} ({link})")

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
                "- Index direction after the open, especially Nasdaq versus Dow.",
                "- Treasury yields and oil, since both can drive risk appetite.",
                "- AI/chip leadership across NVDA, MU, MRVL, and FSELX.",
                "- Company-specific headlines for your holdings.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "WHAT TO WATCH TOMORROW",
                "- Whether today's leaders hold after-hours and pre-market.",
                "- Overnight macro/geopolitical headlines.",
                "- Any earnings, analyst actions, or guidance changes tied to your holdings.",
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
