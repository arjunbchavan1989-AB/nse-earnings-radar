#!/usr/bin/env python3
"""
NSE Earnings Dashboard — data pipeline.

Collects, for each ticker in tickers.txt:
  - Announcement dates + EPS actual/estimate/surprise  (Yahoo Finance, via yfinance)
  - Quarterly sales & EPS history in Rs crore          (screener.in consolidated pages)
  - Daily price history                                (Yahoo Finance)
  - Sector / company name                              (Yahoo Finance)

Computes per earnings event:
  - Sales YoY, EPS YoY (same fiscal quarter, previous year)
  - Beat/miss (surprise % vs analyst consensus, when available)
  - Day-1 market reaction (first close after the announcement)
  - Post-earnings drift: +5 / +10 / +20 trading sessions
  - Delayed positive reaction flag (flat-or-down day 1, then close above
    the reaction-session high within the next 5 sessions)

Writes data/earnings.json for the static dashboard (index.html).

Usage:  python3 pipeline.py [--tickers tickers.txt] [--out data/earnings.json]
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import re
import sys
import time
import traceback
from zoneinfo import ZoneInfo

import requests

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is required:  pip install yfinance")

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")
NSE_CLOSE = dt.time(15, 30)          # NSE cash market close
DRIFT_SESSIONS = (5, 10, 20)        # post-event drift windows (trading sessions)
DELAYED_WINDOW = 5                  # sessions to look for delayed positive reaction
HISTORY_PERIOD = "2y"               # price history
KEEP_EVENTS_YEARS = 2               # events kept in output
UPCOMING_DAYS = 90                  # upcoming announcements window
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HTTP_HEADERS)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def safe_float(v):
    """Parse a number that may be None/NaN/string."""
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def growth_pct(cur, prev):
    """YoY growth as a fraction; None when not computable."""
    if cur is None or prev is None:
        return None
    if prev == 0:
        return None
    return (cur - prev) / abs(prev)


def transition_label(cur, prev):
    """Explicit label for sign-changing EPS comparisons."""
    if cur is None or prev is None:
        return None
    if prev < 0 and cur > 0:
        return "loss-to-profit"
    if prev > 0 and cur < 0:
        return "profit-to-loss"
    if prev < 0 and cur < 0:
        return "loss-narrowing" if cur > prev else "loss-widening"
    return None


# --------------------------------------------------------------------------
# screener.in quarterly table
# --------------------------------------------------------------------------

def quarter_to_iso(label):
    """'Sep 2026' -> '2026-09-30' (quarter-end dates)."""
    months = {m: i + 1 for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
    parts = label.split()
    if len(parts) != 2:
        return None
    mon, year = parts[0], int(parts[1])
    if mon not in months:
        return None
    mo = months[mon]
    day = {3: 31, 6: 30, 9: 30, 12: 31}.get(mo, 28)
    return f"{year:04d}-{mo:02d}:{day:02d}".replace(":", "-") if False else f"{year:04d}-{mo:02d}-{day:02d}" if mo in (3, 6, 9, 12) else f"{year:04d}-{mo:02d}-28"


def load_cache_entry(entry):
    """Convert a screener_cache.json entry to internal quarters format."""
    quarters = []
    for q in entry.get("quarters", []):
        end = quarter_to_iso(q.get("end", ""))
        if not end:
            continue
        try:
            sales = float(q["sales"]) if q.get("sales") not in (None, "", "-") else None
        except (ValueError, TypeError):
            sales = None
        try:
            eps = float(q["eps"]) if q.get("eps") not in (None, "", "-") else None
        except (ValueError, TypeError):
            eps = None
        quarters.append({"end": end, "sales": sales, "eps": eps})
    return quarters, entry.get("name")


SCREENER_CACHE = {}


def screener_search(query):
    """Resolve a query to a screener company code via the search API."""
    try:
        r = session.get(
            "https://www.screener.in/api/company/search/",
            params={"q": query},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                return data[0].get("url", "").rstrip("/").split("/")[-1], data[0].get("name")
    except Exception:
        pass
    return None, None


def fetch_screener_quarters(code):
    """Return (quarters, name) from a screener consolidated page.

    quarters: list of dicts {end: 'YYYY-MM-DD', sales: float|None, eps: float|None}
    """
    for attempt in range(2):
        try:
            r = session.get(
                f"https://www.screener.in/company/{code}/consolidated/",
                timeout=20,
            )
            if r.status_code == 404:
                return None, None
            if r.status_code != 200:
                time.sleep(1.5)
                continue
            html = r.text
            m = re.search(r"Quarterly Results.*?<table[^>]*>(.*?)</table>", html, re.S)
            if not m:
                return None, None
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
            header = None
            sales = eps = None
            for row in rows:
                cells = [
                    re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip()
                    for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
                ]
                if not cells:
                    continue
                label = cells[0]
                if header is None and re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$", cells[-1] or ""):
                    header = cells[1:]  # first cell is empty corner
                    continue
                if label.startswith("Sales") or label.startswith("Revenue"):
                    sales = cells[1:]
                elif label.startswith("EPS in Rs"):
                    eps = cells[1:]
            if header is None or sales is None:
                return None, None
            name_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
            name = re.sub(r"<[^>]+>", "", name_m.group(1)).strip() if name_m else None

            quarters = []
            months = {m_: i + 1 for i, m_ in enumerate(
                ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
            for i, col in enumerate(header):
                mm = re.match(r"([A-Z][a-z]{2})\s+(\d{4})", col or "")
                if not mm:
                    continue
                end = f"{mm.group(2)}-{months[mm.group(1)]:02d}-28"
                try:
                    y, mo = int(mm.group(2)), months[mm.group(1)]
                except Exception:
                    continue
                # quarter ends: Mar/Jun/Sep/Dec -> 31/30/30/31
                day = {3: 31, 6: 30, 9: 30, 12: 31}[mo] if mo in (3, 6, 9, 12) else 28
                end = f"{y:04d}-{mo:02d}-{day:02d}"

                def num_from(series, i):
                    if series is None or i >= len(series):
                        return None
                    s = series[i].replace(",", "").replace("%", "").strip()
                    if s in ("", "-"):
                        return None
                    try:
                        return float(s)
                    except ValueError:
                        return None

                quarters.append({
                    "end": end,
                    "sales": num_from(sales, i),
                    "eps": num_from(eps, i),
                })
            if not quarters:
                return None, None
            return quarters, name
        except Exception:
            time.sleep(1.5)
    return None, None


# --------------------------------------------------------------------------
# price history & event metrics
# --------------------------------------------------------------------------

class Prices:
    """Sorted daily price series with lookup helpers."""

    def __init__(self, hist):
        hist = hist.dropna(subset=["Close"])
        self.dates = [d.date() for d in hist.index]  # index tz-naive dates (IST)
        self.closes = [float(v) for v in hist["Close"]]
        self.highs = [float(v) for v in hist["High"]]
        self._idx = {d: i for i, d in enumerate(self.dates)}

    def first_close_after(self, when_ist):
        """Index of the first session whose close (15:30 IST) is strictly after `when_ist`."""
        for i, d in enumerate(self.dates):
            close_dt = dt.datetime.combine(d, NSE_CLOSE, tzinfo=IST)
            if close_dt > when_ist:
                return i
        return None

    def at(self, i):
        return self.dates[i], self.closes[i] if 0 <= i < len(self.dates) else None

    def fwd(self, i, n):
        j = i + n
        if 0 <= j < len(self.dates):
            return self.closes[j]
        return None


def build_event(ev_date, er_row, prices, quarters_by_end):
    """Assemble one earnings event dict."""
    when_ist = ev_date.astimezone(IST)
    q_end = latest_quarter_end(quarters_by_end, when_ist.date())
    # guard: if the matched quarter-end is far older than the announcement,
    # the expected quarter is missing from the source — don't pair it (wrong YoY)
    if q_end and (when_ist.date() - dt.date.fromisoformat(q_end)).days > 150:
        q_end = None
    quarter = quarters_by_end.get(q_end) if q_end else None

    # --- event day + reaction session
    ri = prices.first_close_after(when_ist)
    if ri is None or ri == 0:
        return None
    prev_close = prices.closes[ri - 1]
    r_close = prices.closes[ri]
    r_high = prices.highs[ri]
    reaction_pct = (r_close / prev_close - 1) if prev_close else None

    drift = {}
    for n in DRIFT_SESSIONS:
        v = prices.fwd(ri, n)
        drift[f"d{n}"] = (v / r_close - 1) if (v and r_close) else None

    # --- delayed positive reaction: day-1 flat-or-down, then close above
    #     the reaction session's high within the next DELAYED_WINDOW sessions
    delayed = {"flag": False, "day": None, "pending": False}
    if reaction_pct is not None and reaction_pct <= 0:
        triggered = False
        for k in range(1, DELAYED_WINDOW + 1):
            j = ri + k
            if j >= len(prices.dates):
                break  # window still open
            if prices.closes[j] > r_high:
                delayed = {"flag": True, "day": k, "pending": False}
                triggered = True
                break
        if not triggered and ri + DELAYED_WINDOW >= len(prices.dates):
            delayed = {"flag": False, "day": None, "pending": True}

    # --- YoY
    yearago_end = shift_year(q_end) if q_end else None
    yearago = quarters_by_end.get(yearago_end) if yearago_end else None
    sales_yoy = growth_pct(quarter["sales"], yearago["sales"]) if (quarter and yearago) else None
    eps_cur = quarter["eps"] if quarter else None
    eps_prev = yearago["eps"] if yearago else None
    eps_yoy = growth_pct(eps_cur, eps_prev) if (eps_prev is not None and eps_prev > 0) else None
    eps_transition = transition_label(eps_cur, eps_prev)

    est = safe_float(er_row.get("EPS Estimate"))
    act = safe_float(er_row.get("Reported EPS"))
    surprise = safe_float(er_row.get("Surprise(%)"))

    return {
        "announcedAt": when_ist.isoformat(),
        "quarterEnd": q_end,
        "salesCr": quarter["sales"] if quarter else None,
        "salesYoy": sales_yoy,
        "eps": eps_cur,
        "epsYoy": eps_yoy,
        "epsTransition": eps_transition,
        "epsEstimate": est,
        "epsActualYahoo": act,
        "surprisePct": surprise,
        "reaction": {
            "date": prices.dates[ri].isoformat(),
            "pct": reaction_pct,
            "high": r_high,
        },
        "drift": drift,
        "delayedPositive": delayed,
    }


def latest_quarter_end(quarters_by_end, on_date):
    """The most recent quarter-end on or before the announcement date."""
    best = None
    for end in quarters_by_end:
        d = dt.date.fromisoformat(end)
        if d <= on_date and (best is None or d > dt.date.fromisoformat(best)):
            best = end
    return best


def shift_year(end):
    if not end:
        return None
    y, m, d = map(int, end.split("-"))
    return f"{y - 1:04d}-{m:02d}-{d:02d}"


# --------------------------------------------------------------------------
# per-ticker job
# --------------------------------------------------------------------------

def process_ticker(symbol):
    tk = yf.Ticker(symbol)
    out = {"ticker": symbol}

    # company meta
    try:
        info = tk.info
        out["name"] = info.get("longName") or info.get("shortName") or symbol.replace(".NS", "")
        out["sector"] = info.get("sector")
        out["industry"] = info.get("industry")
    except Exception:
        out["name"] = symbol.replace(".NS", "")
        out["sector"] = None

    # earnings dates (announcements + estimates)
    try:
        er = tk.get_earnings_dates(limit=20)
    except Exception:
        er = None
    if er is None or len(er) == 0:
        raise RuntimeError("no earnings dates")

    # price history
    hist = tk.history(period=HISTORY_PERIOD, interval="1d", auto_adjust=True)
    if hist is None or len(hist) == 0:
        raise RuntimeError("no price history")
    prices = Prices(hist)

    # screener quarterly fundamentals (live or from cache)
    code = symbol.replace(".NS", "")
    quarters = screener_name = None
    if code in SCREENER_CACHE:
        quarters, screener_name = load_cache_entry(SCREENER_CACHE[code])
    if quarters is None and len(symbol) >= 4:
        alt, alt_name = screener_search(out.get("name") or code)
        if alt and alt in SCREENER_CACHE:
            quarters, screener_name = load_cache_entry(SCREENER_CACHE[alt])
            code = alt
    if quarters is None:
        quarters, screener_name = fetch_screener_quarters(code)
    if quarters is None and len(symbol) >= 4:
        alt, alt_name = screener_search(out.get("name") or code)
        if alt:
            quarters, screener_name = fetch_screener_quarters(alt)
            code = alt
    if quarters is None:
        # Non-fatal: keep the stock with null sales/EPS YoY rather than dropping it
        quarters, screener_name = [], None
    if screener_name and not out.get("name"):
        out["name"] = screener_name
    out["screener"] = code

    quarters_by_end = {q["end"]: q for q in quarters}
    if not quarters_by_end:
        print(f"  WARN {symbol}: no screener quarterly data (sales/EPS YoY will be blank)", flush=True)

    # events (past)
    now = dt.datetime.now(IST)
    cutoff = now - dt.timedelta(days=int(365 * KEEP_EVENTS_YEARS))
    events, upcoming = [], []
    for ev_date, row in er.iterrows():
        when_ist = ev_date.astimezone(IST)
        if when_ist > now + dt.timedelta(days=UPCOMING_DAYS):
            continue
        if when_ist > now:
            est = safe_float(row.get("EPS Estimate"))
            upcoming.append({
                "date": when_ist.date().isoformat(),
                "epsEstimate": est,
            })
            continue
        if when_ist < cutoff:
            continue
        try:
            ev = build_event(ev_date, row, prices, quarters_by_end)
            if ev:
                events.append(ev)
        except Exception:
            continue

    if not events and not upcoming:
        raise RuntimeError("no usable events")
    out["events"] = sorted(events, key=lambda e: e["announcedAt"], reverse=True)
    out["upcoming"] = sorted(upcoming, key=lambda u: u["date"])

    # compact price series for the detail view (~1 year)
    series_dates = prices.dates[-260:]
    out["priceSeries"] = {
        "dates": [d.isoformat() for d in series_dates],
        "closes": [round(c, 2) for c in prices.closes[-260:]],
    }
    out["lastPrice"] = prices.closes[-1]
    out["lastPriceDate"] = prices.dates[-1].isoformat()
    time.sleep(0.35)
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="tickers.txt")
    ap.add_argument("--out", default=os.path.join("data", "earnings.json"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--screener-cache", default=None,
                    help="Optional screener_cache.json (browser-collected quarterly data)")
    ap.add_argument("--cache-only", action="store_true",
                    help="Only process tickers present in the screener cache")
    args = ap.parse_args()

    if args.screener_cache and os.path.exists(args.screener_cache):
        with open(args.screener_cache) as f:
            SCREENER_CACHE.update(json.load(f))
        print(f"Loaded screener cache: {len(SCREENER_CACHE)} companies")

    with open(args.tickers) as f:
        symbols = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    if args.cache_only:
        symbols = [s for s in symbols if s.replace(".NS", "") in SCREENER_CACHE]
        print(f"Cache-only mode: {len(symbols)} tickers")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    results, failures = [], []
    t0 = time.time()

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_ticker, s): s for s in symbols}
        done = 0
        for fut in cf.as_completed(futs):
            sym = futs[fut]
            done += 1
            try:
                results.append(fut.result())
            except Exception as e:
                failures.append((sym, str(e)))
            if done % 10 == 0:
                print(f"  {done}/{len(symbols)} done ({time.time() - t0:.0f}s)", flush=True)

    results.sort(key=lambda r: r["ticker"])

    n_events = sum(len(r["events"]) for r in results)
    n_surprise = sum(
        1 for r in results for e in r["events"] if e["surprisePct"] is not None)
    n_delayed = sum(
        1 for r in results for e in r["events"] if e["delayedPositive"]["flag"])
    n_upcoming = sum(len(r["upcoming"]) for r in results)

    payload = {
        "generatedAt": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "sources": {
            "earnings": "Yahoo Finance (announcement dates, EPS estimates, surprise %)",
            "fundamentals": "screener.in consolidated quarterly results (sales, EPS in Rs crore)",
            "prices": "Yahoo Finance adjusted daily prices",
        },
        "stats": {
            "tickers": len(results),
            "failed": [f[0] for f in failures],
            "events": n_events,
            "eventsWithSurprise": n_surprise,
            "delayedPositiveEvents": n_delayed,
            "upcoming": n_upcoming,
        },
        "stocks": results,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"\nWrote {args.out}: {len(results)} stocks, {n_events} events "
          f"({n_surprise} with surprise data, {n_delayed} delayed-positive), "
          f"{n_upcoming} upcoming — {time.time() - t0:.0f}s")
    if failures:
        print("Failed tickers:")
        for sym, err in failures:
            print(f"  {sym}: {err}")


if __name__ == "__main__":
    main()
