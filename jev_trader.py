"""Jev paper-trader: starts from your IBKR holdings, lets Jev decide buy/sell/hold
and when to look again, runs for 30 days, then compares against your untouched holdings.

Commands:
  init     Create the experiment from holdings.json (snapshot prices, start clock)
  tick     Review any tickers that are due (run by the scheduler every 30 min)
  status   Show the current Jev portfolio vs your actual holdings
  real     Record a trade you made in your real account, e.g. `real sell EQX 500 --price 11.20`
  report   Final comparison: Jev vs your holdings untouched vs your real trades
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import urllib.parse
import urllib.request
import sys
from datetime import datetime, timedelta, time as dtime
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import yfinance as yf
from typesafe_sdk import Choice, Noul, TypeSafeClient

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
HOLDINGS_FILE = ROOT / "holdings.json"
PORTFOLIO_FILE = STATE_DIR / "portfolio.json"
DECISIONS_LOG = STATE_DIR / "decisions.jsonl"
TRADES_LOG = STATE_DIR / "trades.jsonl"
SNAPSHOTS_LOG = STATE_DIR / "snapshots.jsonl"
DAILY_LOG = STATE_DIR / "daily.jsonl"
REAL_TRADES_LOG = STATE_DIR / "real_trades.jsonl"
DAILY_DIR = STATE_DIR / "daily"
DAILY_REPORT_AFTER = dtime(16, 10)

ET = ZoneInfo("America/New_York")
MARKET_OPEN, MARKET_CLOSE = dtime(9, 30), dtime(16, 0)
EXPERIMENT_DAYS = 30
MODEL = "jev-latest"

# Trading policy (code owns this; Jev only supplies the judgment)
MIN_ACTION_PROB = 0.5        # act only when Jev puts >= 50% on a non-hold action
TRIM_FRACTION = 1 / 3        # "trim" sells a third of the position
BUY_UNIT = 0.05              # "add" buys 5% of total portfolio value; "add_more" buys 10%
MAX_POSITION_WEIGHT = 0.40   # never let one stock exceed 40% of the portfolio
FEE_PER_TRADE = 1.00         # rough IBKR fixed-tier commission

ACTIONS = {
    "sell_all": "Exit the entire position now: the outlook over the remaining experiment is clearly negative "
                "(broken trend, serious bad news, or the risk clearly outweighs the upside).",
    "trim": "Sell about a third: take some profit or reduce risk, but still expect the stock to hold up reasonably.",
    "hold": "Keep the position unchanged: no clear edge either way, or the evidence is mixed.",
    "add": "Buy a modest amount more (about 5% of the portfolio): the outlook is moderately positive.",
    "add_more": "Buy a larger amount (about 10% of the portfolio): strong positive momentum or catalyst "
                "with acceptable risk.",
}

REVIEW_CADENCE = {
    "1_hour": "Look again in about an hour: something is moving fast right now (big intraday move, breaking "
              "news, earnings imminent or just released).",
    "end_of_day": "Look again in about 3-4 hours, before the close: elevated volatility worth watching today.",
    "next_day": "Look again next trading day: normal conditions, worth a daily check.",
    "3_days": "Look again in about 3 trading days: calm, trending steadily, nothing scheduled soon.",
    "1_week": "Look again in about a week: very stable, low volatility, no upcoming events.",
}


# ---------- persistence ----------

def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------- market time ----------

def now_et() -> datetime:
    return datetime.now(ET)


def market_is_open() -> bool:
    """True if SPY printed a 1-minute bar in the last 20 minutes (handles weekends and holidays)."""
    now = now_et()
    if now.weekday() >= 5 or not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        return False
    bars = yf.Ticker("SPY").history(period="1d", interval="1m")
    if bars.empty:
        return False
    return (now - bars.index[-1].to_pydatetime().astimezone(ET)) < timedelta(minutes=20)


def next_session_time(after: datetime, at: dtime = dtime(10, 0)) -> datetime:
    d = after.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, at, ET)


def add_trading_days(start: datetime, n: int) -> datetime:
    t = start
    for _ in range(n):
        t = next_session_time(t)
    return t


def next_review_time(cadence: str, now: datetime) -> datetime:
    if cadence in ("1_hour", "end_of_day"):
        t = now + timedelta(hours=1 if cadence == "1_hour" else 3.5)
        close = datetime.combine(now.date(), MARKET_CLOSE, ET) - timedelta(minutes=15)
        if t <= close:
            return t
        return close if close - now >= timedelta(minutes=30) else next_session_time(now)
    return add_trading_days(now, {"next_day": 1, "3_days": 3, "1_week": 5}[cadence])


# ---------- market data ----------

def pct(a: float, b: float) -> float | None:
    return None if not b or math.isnan(b) else round((a / b - 1) * 100, 2)


def rsi(closes, period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period).mean().iloc[-1]
    return 100.0 if loss == 0 else round(100 - 100 / (1 + gain / loss), 1)


def last_price(ticker: str) -> float:
    return float(yf.Ticker(ticker).fast_info["lastPrice"])


def market_features(ticker: str) -> dict:
    hist = yf.Ticker(ticker).history(period="1y", interval="1d")
    c, v = hist["Close"], hist["Volume"]
    price = last_price(ticker)
    sma = lambda n: c.tail(n).mean() if len(c) >= n else float("nan")
    vol20 = c.pct_change().tail(20).std() * math.sqrt(252) * 100
    return {
        "price": round(price, 2),
        "change_today_pct": pct(price, c.iloc[-2]) if len(c) > 1 else None,
        "return_5d_pct": pct(price, c.iloc[-6]) if len(c) > 5 else None,
        "return_1mo_pct": pct(price, c.iloc[-22]) if len(c) > 21 else None,
        "return_3mo_pct": pct(price, c.iloc[-64]) if len(c) > 63 else None,
        "rsi_14": rsi(c),
        "vs_sma20_pct": pct(price, sma(20)),
        "vs_sma50_pct": pct(price, sma(50)),
        "vs_sma200_pct": pct(price, sma(200)),
        "volatility_20d_annualized_pct": round(vol20, 1),
        "volume_vs_20d_avg": round(v.iloc[-1] / v.tail(20).mean(), 2) if v.tail(20).mean() else None,
        "from_52w_high_pct": pct(price, c.max()),
        "from_52w_low_pct": pct(price, c.min()),
    }


NEWS_MAX_AGE = timedelta(days=7)
NEWS_KEEP = 10
# SEC requires a contact in the User-Agent, e.g. export SEC_USER_AGENT="jev-trader you@example.com"
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT")
SEC_TICKERS_FILE = STATE_DIR / "sec_tickers.json"
SEC_FORMS = {
    "8-K": "material event", "6-K": "foreign issuer report", "10-Q": "quarterly report",
    "10-K": "annual report", "20-F": "foreign annual report", "40-F": "Canadian annual report",
    "4": "insider buy/sell", "144": "insider plans to sell shares", "S-1": "share offering (dilution)",
    "S-3": "shelf registration (possible future share sales)", "424B5": "share offering priced (dilution)",
    "424B3": "prospectus (possible share sales)", "SC 13D": "activist / large holder stake",
    "SC 13G": "large passive holder stake",
}
EIGHT_K_ITEMS = {
    "1.01": "major agreement", "1.03": "bankruptcy", "2.02": "earnings results", "2.03": "new debt",
    "3.02": "unregistered share sale (dilution)", "4.02": "restated financials", "5.02": "executive change",
    "7.01": "investor disclosure", "8.01": "other event",
}


def company_name(ticker: str, _cache={}) -> str:
    if ticker not in _cache:
        name = yf.Ticker(ticker).info.get("shortName") or ticker
        _cache[ticker] = re.sub(r",?\s+(Inc\.?|Corp\.?|Corporation|Group N\.V\.|N\.V\.|Ltd\.?|plc|Holdings?)\b.*$",
                                "", name).strip() or name
    return _cache[ticker]


def http_get(url: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    return urllib.request.urlopen(req, timeout=15).read()


def yahoo_news(ticker: str) -> list[dict]:
    out = []
    for n in yf.Search(ticker, news_count=10).news:
        if n.get("providerPublishTime"):
            out.append({"title": n["title"], "source": n.get("publisher"),
                        "published": datetime.fromtimestamp(n["providerPublishTime"], ET)})
    return out


def google_news(query: str) -> list[dict]:
    """Google News RSS: aggregates Reuters, Bloomberg, FT, WSJ, CNBC, Barron's, MarketWatch, etc. (headlines only)."""
    q = urllib.parse.quote(f"{query} when:7d")
    root = ElementTree.fromstring(http_get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"))
    out = []
    for item in root.iter("item"):
        source = item.findtext("source") or ""
        title = item.findtext("title") or ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        out.append({"title": title, "source": source,
                    "published": parsedate_to_datetime(item.findtext("pubDate")).astimezone(ET)})
    return out


def gather_news(client: TypeSafeClient, ticker: str, now: datetime) -> list[dict]:
    """Recent headlines from several outlets, de-duplicated, with Jev filtering out ones not about this company."""
    name = company_name(ticker)
    candidates = []
    for fetch in (lambda: yahoo_news(ticker), lambda: google_news(f'"{name}"')):
        try:
            candidates += fetch()
        except Exception as e:
            print(f"{ticker}: news source failed ({e})")
    seen, fresh = set(), []
    for n in sorted(candidates, key=lambda n: n["published"], reverse=True):
        key = re.sub(r"\W+", "", n["title"].lower())[:60]
        if key not in seen and now - n["published"] <= NEWS_MAX_AGE:
            seen.add(key)
            fresh.append(n)
    fresh = fresh[:30]
    if not fresh:
        return []

    questions = {
        str(i): Noul(instructions=(
            f"Is the headline `headlines[{i}]` news or analysis that directly concerns {name} (ticker {ticker}) "
            f"as described in `company`? A headline about a different company or organisation that merely shares "
            f"a similar name or ticker is no; a market roundup that lists {name} among stocks that moved is yes."
        ))
        for i in range(len(fresh))
    }
    state = {"company": {"name": name, "ticker": ticker},
             "headlines": [{"title": n["title"], "source": n["source"]} for n in fresh]}
    try:
        verdicts = client.system_one(state, questions).nouls
        relevant = [n for i, n in enumerate(fresh) if verdicts[str(i)].noul >= 0.5]
    except Exception as e:
        print(f"{ticker}: relevance filter failed ({e}); using unfiltered news")
        relevant = fresh
    return [{"title": n["title"], "source": n["source"],
             "published": n["published"].strftime("%Y-%m-%d %H:%M ET")} for n in relevant[:NEWS_KEEP]]


def sec_filings(ticker: str, now: datetime, days: int = 30) -> list[dict]:
    """Recent SEC filings with a plain-language label (offerings, insider sales, earnings, exec changes...)."""
    if not SEC_USER_AGENT:
        return []
    try:
        if not SEC_TICKERS_FILE.exists():
            SEC_TICKERS_FILE.write_bytes(http_get("https://www.sec.gov/files/company_tickers.json",
                                                  {"User-Agent": SEC_USER_AGENT}))
        cik = next((v["cik_str"] for v in json.loads(SEC_TICKERS_FILE.read_text()).values()
                    if v["ticker"] == ticker), None)
        if cik is None:
            return []
        recent = json.loads(http_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
                                     {"User-Agent": SEC_USER_AGENT}))["filings"]["recent"]
    except Exception as e:
        print(f"{ticker}: SEC lookup failed ({e})")
        return []
    cutoff = (now - timedelta(days=days)).date().isoformat()
    out = []
    for form, filed, items in zip(recent["form"], recent["filingDate"], recent["items"]):
        if filed < cutoff:
            break
        meaning = SEC_FORMS.get(form)
        if not meaning:
            continue
        if form == "8-K" and items:
            meaning += ": " + ", ".join(EIGHT_K_ITEMS[i] for i in items.split(",") if i in EIGHT_K_ITEMS)
        out.append({"filed": filed, "form": form, "meaning": meaning})
    return out[:12]


def market_context() -> dict:
    spy = market_features("SPY")
    try:
        vix = round(last_price("^VIX"), 2)
    except Exception:
        vix = None
    return {
        "sp500_change_today_pct": spy["change_today_pct"],
        "sp500_return_5d_pct": spy["return_5d_pct"],
        "sp500_return_1mo_pct": spy["return_1mo_pct"],
        "sp500_vs_sma50_pct": spy["vs_sma50_pct"],
        "vix": vix,
    }


# ---------- portfolio math ----------

def value(positions: dict, cash: float, prices: dict) -> float:
    return cash + sum(p["shares"] * prices[t] for t, p in positions.items())


def current_prices(tickers) -> dict:
    return {t: last_price(t) for t in tickers}


def real_account(pf: dict) -> dict:
    """Your real account: starting holdings plus the trades you recorded with `real`."""
    return pf.setdefault("real", {"positions": dict(pf["baseline"]["positions"]), "cash": pf["baseline"]["cash"]})


def all_tickers(pf: dict) -> set:
    return set(pf["positions"]) | set(pf["baseline"]["positions"]) | set(real_account(pf)["positions"])


def three_values(pf: dict, prices: dict) -> tuple[float, float, float]:
    """(holdings untouched, your real account, Jev)"""
    base, real = pf["baseline"], real_account(pf)
    untouched = base["cash"] + sum(sh * prices[t] for t, sh in base["positions"].items())
    real_value = real["cash"] + sum(sh * prices[t] for t, sh in real["positions"].items())
    return untouched, real_value, value(pf["positions"], pf["cash"], prices)


# ---------- Jev ----------

def ask_jev(client: TypeSafeClient, state: dict):
    questions = {
        "action": Choice(
            instructions=(
                "You manage the paper portfolio in `portfolio`. Decide what to do with the stock in `stock` right "
                "now, using its price action in `stock.market`, recent headlines from several outlets in `stock.news`, "
                "its recent SEC filings in `stock.sec_filings_last_30d` (offerings mean dilution, insider sales, "
                "earnings, executive changes), the broad market in `market`, the current position in `stock.position`, and your earlier calls in "
                "`stock.your_recent_decisions`. The goal is to finish `experiment` with a higher total portfolio "
                "value than simply holding the starting positions. Trading has a small cost, so only trade when "
                "there is a real edge; buying requires available cash in `portfolio.cash`."
            ),
            criteria=ACTIONS,
        ),
        "cadence": Choice(
            instructions=(
                "Given how fast the situation for the stock in `stock` is changing (its volatility, today's move, "
                "volume, fresh headlines in `stock.news`, recent filings, and the broad `market`), when should this stock be reviewed again?"
            ),
            criteria=REVIEW_CADENCE,
        ),
    }
    r = client.system_one(state, questions)
    return r.choices["action"], r.choices["cadence"]


def execute(action: str, ticker: str, price: float, pf: dict, total_value: float) -> dict | None:
    pos = pf["positions"].setdefault(ticker, {"shares": 0.0, "avg_cost": price})
    shares = 0.0
    if action in ("sell_all", "trim") and pos["shares"] > 0:
        shares = -(pos["shares"] if action == "sell_all" else math.floor(pos["shares"] * TRIM_FRACTION) or pos["shares"])
    elif action in ("add", "add_more"):
        budget = total_value * BUY_UNIT * (2 if action == "add_more" else 1)
        room = total_value * MAX_POSITION_WEIGHT - pos["shares"] * price
        budget = min(budget, room, pf["cash"] - FEE_PER_TRADE)
        shares = math.floor(budget / price) if budget > 0 else 0
    if shares == 0:
        return None
    cost = shares * price
    if shares > 0:
        pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + cost) / (pos["shares"] + shares)
    pos["shares"] += shares
    pf["cash"] -= cost + FEE_PER_TRADE
    return {"ticker": ticker, "action": action, "shares": shares, "price": round(price, 4),
            "cash_after": round(pf["cash"], 2)}


# ---------- commands ----------

def cmd_init(args) -> None:
    if PORTFOLIO_FILE.exists() and not args.force:
        sys.exit("Experiment already initialised. Use `init --force` to restart (this wipes state/).")
    holdings = load_json(HOLDINGS_FILE)
    if not holdings or not holdings.get("positions"):
        sys.exit(f"Fill in {HOLDINGS_FILE} first.")
    for f in (PORTFOLIO_FILE, DECISIONS_LOG, TRADES_LOG, SNAPSHOTS_LOG):
        f.unlink(missing_ok=True)

    tickers = list(holdings["positions"])
    prices = current_prices(tickers)
    start = now_et()
    positions = {
        t: {"shares": float(p["shares"]), "avg_cost": float(p.get("avg_cost") or prices[t])}
        for t, p in holdings["positions"].items()
    }
    cash = float(holdings.get("cash", 0))
    pf = {
        "start": start.isoformat(),
        "end": (start + timedelta(days=EXPERIMENT_DAYS)).isoformat(),
        "start_prices": prices,
        "start_value": value(positions, cash, prices),
        "spy_start": last_price("SPY"),
        "baseline": {"positions": {t: p["shares"] for t, p in positions.items()}, "cash": cash},
        "positions": positions,
        "cash": cash,
        "next_review": {t: start.isoformat() for t in tickers},  # review everything on the first tick
        "finished": False,
    }
    save_json(PORTFOLIO_FILE, pf)
    print(f"Experiment started {start:%Y-%m-%d %H:%M ET}, ends {pf['end'][:10]}. "
          f"Starting value ${pf['start_value']:,.2f} across {len(tickers)} tickers + ${cash:,.2f} cash.")


def snapshot(pf: dict, prices: dict) -> dict:
    prices = {**current_prices(all_tickers(pf) - set(prices)), **prices}
    untouched, real, jev = three_values(pf, prices)
    snap = {
        "time": now_et().isoformat(),
        "jev_value": round(jev, 2),
        "actual_value": round(untouched, 2),
        "real_value": round(real, 2),
        "spy_return_pct": pct(last_price("SPY"), pf["spy_start"]),
    }
    append_jsonl(SNAPSHOTS_LOG, snap)
    return snap


def cmd_tick(args) -> None:
    pf = load_json(PORTFOLIO_FILE)
    if not pf:
        sys.exit("Run `init` first.")
    if pf["finished"]:
        return
    now = now_et()
    maybe_daily_report(pf, now)
    if now >= datetime.fromisoformat(pf["end"]):
        pf["finished"] = True
        save_json(PORTFOLIO_FILE, pf)
        snapshot(pf, {})
        report_text = cmd_report(argparse.Namespace())
        notify("Jev experiment finished", report_text.splitlines()[-1])
        return
    if not args.force and not market_is_open():
        return

    due = [t for t, when in pf["next_review"].items()
           if args.force or datetime.fromisoformat(when) <= now]
    if not due:
        return

    client = TypeSafeClient(model=MODEL)
    mkt = market_context()
    prices = current_prices(pf["positions"])
    recent = read_jsonl(DECISIONS_LOG)
    days_elapsed = (now - datetime.fromisoformat(pf["start"])).days

    for ticker in due:
        total = value(pf["positions"], pf["cash"], prices)
        feats = market_features(ticker)
        prices[ticker] = feats["price"]
        pos = pf["positions"][ticker]
        state = {
            "experiment": {
                "days_elapsed": days_elapsed,
                "days_remaining": EXPERIMENT_DAYS - days_elapsed,
                "now": now.strftime("%A %Y-%m-%d %H:%M ET"),
            },
            "portfolio": {
                "total_value": round(total, 2),
                "cash": round(pf["cash"], 2),
                "cash_pct": round(pf["cash"] / total * 100, 1),
                "return_since_start_pct": pct(total, pf["start_value"]),
            },
            "market": mkt,
            "stock": {
                "ticker": ticker,
                "market": feats,
                "news": gather_news(client, ticker, now),
                "sec_filings_last_30d": sec_filings(ticker, now),
                "position": {
                    "shares": pos["shares"],
                    "avg_cost": round(pos["avg_cost"], 2),
                    "unrealized_pnl_pct": pct(feats["price"], pos["avg_cost"]) if pos["shares"] else None,
                    "weight_pct": round(pos["shares"] * feats["price"] / total * 100, 1),
                },
                "your_recent_decisions": [
                    {"time": d["time"][:16], "action": d["action"], "price": d["price"]}
                    for d in recent if d["ticker"] == ticker
                ][-3:],
            },
        }
        try:
            action, cadence = ask_jev(client, state)
        except Exception as e:  # service hiccup: retry this ticker on the next tick
            print(f"{ticker}: Jev call failed ({e}); will retry")
            continue

        chosen = action.choice
        acted = chosen if chosen == "hold" or action.probabilities[chosen] >= MIN_ACTION_PROB else "hold"
        trade = None if acted == "hold" else execute(acted, ticker, feats["price"], pf, total)
        nxt = next_review_time(cadence.choice, now)
        pf["next_review"][ticker] = nxt.isoformat()

        record = {
            "time": now.isoformat(), "ticker": ticker, "price": feats["price"],
            "jev_choice": chosen, "action": acted, "action_probs": action.probabilities,
            "action_confidence": action.confidence, "cadence": cadence.choice,
            "cadence_probs": cadence.probabilities, "next_review": nxt.isoformat(),
            "trade": trade, "state": state,
        }
        append_jsonl(DECISIONS_LOG, record)
        if trade:
            append_jsonl(TRADES_LOG, {"time": now.isoformat(), **trade})
        print(f"{now:%m-%d %H:%M} {ticker:6} jev={chosen:8} ({action.probabilities[chosen]:.0%}) "
              f"-> {acted:8} {'traded ' + str(trade['shares']) if trade else '':14} next={cadence.choice}")

    save_json(PORTFOLIO_FILE, pf)
    snapshot(pf, prices)


def cmd_status(args) -> None:
    pf = load_json(PORTFOLIO_FILE)
    if not pf:
        sys.exit("Run `init` first.")
    prices = current_prices(all_tickers(pf))
    snap = snapshot(pf, prices)
    real = real_account(pf)
    print(f"Day {(now_et() - datetime.fromisoformat(pf['start'])).days}/{EXPERIMENT_DAYS}  "
          f"(ends {pf['end'][:10]})")
    print(f"{'Ticker':8}{'Untouched':>11}{'Real':>10}{'Jev':>10}{'Price':>11}{'Next review':>20}")
    for t in sorted(all_tickers(pf)):
        nxt = pf["next_review"].get(t, "")[5:16].replace("T", " ")
        print(f"{t:8}{pf['baseline']['positions'].get(t, 0):>11g}{real['positions'].get(t, 0):>10g}"
              f"{pf['positions'].get(t, {}).get('shares', 0):>10g}{prices[t]:>11.2f}{nxt:>20}")
    print(f"{'Cash':8}{pf['baseline']['cash']:>11,.0f}{real['cash']:>10,.0f}{pf['cash']:>10,.0f}")
    for label, key in (("Untouched", "actual_value"), ("Your real account", "real_value"), ("Jev", "jev_value")):
        print(f"{label:20}${snap[key]:>12,.2f}  ({pct(snap[key], pf['start_value']):+.2f}%)")
    print(f"{'S&P 500':20}{snap['spy_return_pct']:>+13.2f}%")
    print(f"Decisions so far: {len(read_jsonl(DECISIONS_LOG))}, trades: {len(read_jsonl(TRADES_LOG))}")


def cmd_report(args) -> str:
    pf = load_json(PORTFOLIO_FILE)
    if not pf:
        sys.exit("Run `init` first.")
    prices = current_prices(all_tickers(pf))
    untouched, real_value, jev = three_values(pf, prices)
    real = real_account(pf)
    decisions, trades = read_jsonl(DECISIONS_LOG), read_jsonl(TRADES_LOG)
    cadences = {}
    for d in decisions:
        cadences[d["cadence"]] = cadences.get(d["cadence"], 0) + 1

    lines = [
        f"Jev vs you  |  {pf['start'][:10]} -> {now_et():%Y-%m-%d}",
        f"{'Starting value:':28}${pf['start_value']:,.2f}",
        f"{'Your holdings, untouched:':28}${untouched:,.2f}  ({pct(untouched, pf['start_value']):+.2f}%)",
        f"{'Your real account:':28}${real_value:,.2f}  ({pct(real_value, pf['start_value']):+.2f}%)"
        f"  ({len(read_jsonl(REAL_TRADES_LOG))} trades)",
        f"{'Jev portfolio:':28}${jev:,.2f}  ({pct(jev, pf['start_value']):+.2f}%)",
        f"{'S&P 500:':28}{pct(last_price('SPY'), pf['spy_start']):+.2f}%",
        f"Jev made {len(decisions)} decisions and {len(trades)} trades "
        f"(${len(trades) * FEE_PER_TRADE:.0f} est. fees). Review cadence it chose: {cadences}",
        "",
        "Shares          Untouched      Real       Jev      Price",
    ]
    for t in sorted(all_tickers(pf)):
        lines.append(f"  {t:8}{pf['baseline']['positions'].get(t, 0):>13g}{real['positions'].get(t, 0):>10g}"
                     f"{pf['positions'].get(t, {}).get('shares', 0):>10g}{prices[t]:>11,.2f}")
    lines.append("")
    for label, other in (("your untouched holdings", untouched), ("your real account", real_value)):
        diff = jev - other
        lines.append(f"Result: Jev {'beat' if diff > 0 else 'trailed'} {label} by ${abs(diff):,.2f} "
                     f"({pct(jev, other):+.2f}%).")
    text = "\n".join(lines)
    print(text)
    if pf["finished"]:
        (STATE_DIR / "final_report.txt").write_text(text)
    return text


def cmd_real(args) -> None:
    """Record a trade made in your real IBKR account so the 'real' line stays accurate."""
    pf = load_json(PORTFOLIO_FILE)
    if not pf:
        sys.exit("Run `init` first.")
    ticker, when = args.ticker.upper(), args.date or now_et().date().isoformat()
    price = args.price
    if price is None:  # default to that day's close
        hist = yf.Ticker(ticker).history(start=when, end=(datetime.fromisoformat(when) + timedelta(days=5)).date().isoformat())
        if hist.empty:
            sys.exit(f"No price for {ticker} on {when}; pass --price.")
        price = round(float(hist["Close"].iloc[0]), 4)
    real = real_account(pf)
    held = real["positions"].get(ticker, 0)
    shares = args.shares if args.side == "buy" else -args.shares
    if held + shares < 0:
        sys.exit(f"You only hold {held:g} {ticker} in the tracked real account.")
    real["positions"][ticker] = held + shares
    real["cash"] -= shares * price + args.fee
    save_json(PORTFOLIO_FILE, pf)
    append_jsonl(REAL_TRADES_LOG, {"date": when, "side": args.side, "ticker": ticker, "shares": args.shares,
                                   "price": price, "fee": args.fee, "cash_after": round(real["cash"], 2)})
    print(f"Recorded real trade: {args.side} {args.shares:g} {ticker} @ ${price:,.2f} on {when}. "
          f"Real account now: {real['positions'][ticker]:g} {ticker}, cash ${real['cash']:,.2f}")


def last_session_date():
    return yf.Ticker("SPY").history(period="5d", interval="1d").index[-1].date()


def maybe_daily_report(pf: dict, now: datetime) -> None:
    """After each session closes, write that day's report once."""
    session = last_session_date()
    if now < datetime.combine(session, DAILY_REPORT_AFTER, ET):
        return
    if any(d["date"] == session.isoformat() for d in read_jsonl(DAILY_LOG)):
        return
    text = daily_report(pf, session)
    notify(f"Jev daily report {session:%b %d}", text.splitlines()[2])


def daily_report(pf: dict, session) -> str:
    """You (holdings untouched since start) vs Jev, as of the close of `session`."""
    base, real = pf["baseline"], real_account(pf)
    tickers = all_tickers(pf)
    hist = {t: yf.Ticker(t).history(period="5d", interval="1d")["Close"] for t in tickers | {"SPY"}}
    close = {t: float(h.iloc[-1]) for t, h in hist.items()}
    prev_close = {t: float(h.iloc[-2]) if len(h) > 1 else float(h.iloc[-1]) for t, h in hist.items()}

    you, real_value, jev = three_values(pf, close)
    history = read_jsonl(DAILY_LOG)
    prev = history[-1] if history else {"you": pf["start_value"], "jev": pf["start_value"]}
    prev.setdefault("real", prev["you"])
    record = {"date": session.isoformat(), "you": round(you, 2), "real": round(real_value, 2),
              "jev": round(jev, 2), "spy_pct": pct(close["SPY"], pf["spy_start"])}
    append_jsonl(DAILY_LOG, record)
    history.append(record)

    day_no = len(history)
    todays = [d for d in read_jsonl(DECISIONS_LOG) if d["time"][:10] == session.isoformat()]
    real_today = [r for r in read_jsonl(REAL_TRADES_LOG) if r["date"] == session.isoformat()]
    money = lambda x: f"{'-' if x < 0 else '+'}${abs(x):,.2f}"
    verdict = lambda gap, other: f"{'AHEAD of' if gap >= 0 else 'BEHIND'} {{}} by {money(gap)[1:]} " \
                                 f"({pct(jev, other):+.2f}%)"
    lines = [
        f"JEV DAILY REPORT  {session:%a %b %d, %Y}  (trading day {day_no}, experiment ends {pf['end'][:10]})",
        "",
        "Jev is " + verdict(jev - you, you).format("your untouched holdings"),
        "Jev is " + verdict(jev - real_value, real_value).format("your real account"),
        "",
        f"{'':26}{'Value':>14}{'Today':>13}{'Since start':>14}",
        f"{'You (did nothing)':26}{'$' + format(you, ',.2f'):>14}{money(you - prev['you']):>13}"
        f"{pct(you, pf['start_value']):>+13.2f}%",
        f"{'You (real trades)':26}{'$' + format(real_value, ',.2f'):>14}{money(real_value - prev['real']):>13}"
        f"{pct(real_value, pf['start_value']):>+13.2f}%",
        f"{'Jev':26}{'$' + format(jev, ',.2f'):>14}{money(jev - prev['jev']):>13}"
        f"{pct(jev, pf['start_value']):>+13.2f}%",
        f"{'S&P 500':26}{'':>14}{pct(close['SPY'], prev_close['SPY']):>+12.2f}%"
        f"{record['spy_pct']:>+13.2f}%",
        "",
        "Holdings     Untouched      Real       Jev      Close    Day",
    ]
    for t in sorted(tickers):
        lines.append(f"  {t:8}{base['positions'].get(t, 0):>12g}{real['positions'].get(t, 0):>10g}"
                     f"{pf['positions'].get(t, {}).get('shares', 0):>10g}"
                     f"{close[t]:>11,.2f}{pct(close[t], prev_close[t]):>+7.2f}%")
    lines.append(f"  {'Cash':8}{base['cash']:>12,.0f}{real['cash']:>10,.0f}{pf['cash']:>10,.0f}")
    if real_today:
        lines += ["", "Your real trades today:"]
        lines += [f"  {r['side']} {r['shares']:g} {r['ticker']} @ ${r['price']:,.2f}" for r in real_today]
    lines += ["", f"Jev's calls today ({len(todays)}):"]
    for d in todays:
        trade = f"  -> {'bought' if d['trade']['shares'] > 0 else 'sold'} {abs(d['trade']['shares']):g} " \
                f"@ ${d['trade']['price']:,.2f}" if d["trade"] else ""
        lines.append(f"  {d['time'][11:16]} {d['ticker']:6} {d['action']:8} "
                     f"({d['action_probs'][d['jev_choice']]:.0%})  next look: {d['cadence']}{trade}")
    if not todays:
        lines.append("  none (no stocks were due for review)")
    lines += ["", "History         Untouched          Real           Jev   Jev vs real"]
    for h in history:
        r = h.get("real", h["you"])
        lines.append(f"  {h['date']}  {h['you']:>12,.2f}  {r:>12,.2f}  {h['jev']:>12,.2f}  {money(h['jev'] - r):>12}")

    text = "\n".join(lines)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    (DAILY_DIR / f"{session.isoformat()}.txt").write_text(text)
    (STATE_DIR / "latest_daily_report.txt").write_text(text)
    print(text)
    return text


def cmd_daily(args) -> None:
    pf = load_json(PORTFOLIO_FILE)
    if not pf:
        sys.exit("Run `init` first.")
    latest = STATE_DIR / "latest_daily_report.txt"
    if args.regenerate or not latest.exists():
        maybe_daily_report(pf, now_et())
    print(latest.read_text() if latest.exists() else "No daily report yet (first one is written after today's close).")


def notify(title: str, msg: str) -> None:
    subprocess.run(["osascript", "-e", f'display notification {json.dumps(msg)} with title {json.dumps(title)}'],
                   check=False)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("tick"); s.add_argument("--force", action="store_true",
                                               help="review all tickers now, even if market closed")
    s.set_defaults(fn=cmd_tick)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("daily", help="show the latest daily report")
    s.add_argument("--regenerate", action="store_true", help="write today's report now if it's due")
    s.set_defaults(fn=cmd_daily)
    s = sub.add_parser("real", help="record a trade you made in your real account")
    s.add_argument("side", choices=["buy", "sell"]); s.add_argument("ticker"); s.add_argument("shares", type=float)
    s.add_argument("--price", type=float, help="fill price (default: that day's close)")
    s.add_argument("--date", help="YYYY-MM-DD (default: today)")
    s.add_argument("--fee", type=float, default=0.0)
    s.set_defaults(fn=cmd_real)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
