# jev-trader

A 30-day paper-trading experiment. It takes a snapshot of your brokerage holdings (IBKR in my case) and hands a
**paper copy** to [Jev](https://docs.typesafe.ai), TypeSafe's System One model. Jev decides, stock by stock,
whether to buy, sell or hold, **and how soon to look at each stock again**. Every trading day you get a report comparing:

| Portfolio | What it is |
|---|---|
| **Untouched** | your starting holdings, never traded |
| **Real** | your actual account, including the trades you record with `real` |
| **Jev** | the paper portfolio Jev manages |

Nothing here places real orders. Your brokerage account is never touched.

## How it works

Every 30 minutes during market hours, a launchd job runs `jev_trader.py tick`. It reviews only the stocks that are
due. For each one, the script builds fresh **state** and asks Jev two `Choice` questions in one call:

- **What to do:** `sell_all` · `trim` (sell ~1/3) · `hold` · `add` (~5% of portfolio) · `add_more` (~10%)
- **When to look again:** `1_hour` · `end_of_day` · `next_day` · `3_days` · `1_week`

The state Jev sees:

- **Price action** (Yahoo Finance): today's move, 5d/1mo/3mo returns, RSI-14, distance from SMA 20/50/200,
  20-day volatility, volume vs average, distance from 52-week high/low
- **Market:** S&P 500 trend and the VIX
- **News:** the last 7 days of headlines from Yahoo Finance and Google News (Reuters, Bloomberg, FT, WSJ, CNBC,
  Barron's, …), de-duplicated. A batch of Jev `Noul` questions filters out headlines that aren't about the company.
- **SEC filings** (optional): offerings, insider sales, 8-K items, from EDGAR
- **Position:** shares, cost basis, P&L, portfolio weight, and Jev's own last 3 calls on the stock

Jev supplies the judgment. **Code owns the policy:**

| Rule | Value |
|---|---|
| Act on a non-hold call only if Jev's probability is at least | 50% (`MIN_ACTION_PROB`) |
| Funding | cash only: no margin, no shorting |
| Max single-position weight | 40% |
| Fee per trade | $1 |
| Universe | only the stocks you started with |

Every decision is logged with the full state, probabilities and resulting trade (`state/decisions.jsonl`), so any
call can be audited afterwards.

## Setup

Requires macOS (for launchd and notifications), [uv](https://docs.astral.sh/uv/) and a TypeSafe API key.

```bash
export TYPESAFE_API_KEY=...            # put it in ~/.zshrc; the scheduler reads it from there
cp holdings.example.json holdings.json # then enter your positions (avg_cost is optional)
uv sync
uv run python jev_trader.py init       # snapshot prices, start the 30-day clock
uv run python jev_trader.py tick --force   # first review right now
./install_schedule.sh                  # tick every 30 min + keep the Mac awake during market hours
```

Optional:

```bash
sudo pmset repeat wakeorpoweron MTWRF 09:15:00          # wake the Mac before the open
export SEC_USER_AGENT="jev-trader you@example.com"      # enable SEC filings (SEC requires a contact)
```

## Usage

```bash
uv run python jev_trader.py status    # live: untouched vs real vs Jev, next review time per stock
uv run python jev_trader.py daily     # latest daily report
uv run python jev_trader.py real sell EQX 500 --price 11.20 --date 2026-10-16   # record a real trade
uv run python jev_trader.py report    # full comparison (written to state/final_report.txt at day 30)
```

Daily reports are written once per session after 4:10pm ET to `state/daily/YYYY-MM-DD.txt`, with a macOS
notification. After 30 days the experiment stops trading and writes the final report.

## Files

| Path | Contents |
|---|---|
| `jev_trader.py` | everything: data, Jev questions, trading policy, reports, CLI |
| `install_schedule.sh` | launchd jobs (30-minute tick, keep-awake during market hours) |
| `holdings.json` | your starting positions (git-ignored) |
| `state/` | portfolio, decision/trade logs, daily reports (git-ignored) |

To stop the scheduler:
`launchctl unload ~/Library/LaunchAgents/ai.typesafe.jev-trader*.plist && rm ~/Library/LaunchAgents/ai.typesafe.jev-trader*.plist`

## Caveats

- The scheduler only runs while the Mac is awake. With the lid closed it sleeps unless an external display is
  connected.
- Prices come from Yahoo and can be ~15 minutes delayed. Trades fill at the last price, with no slippage modelled.
- Jev sees headlines only, not article text.
- One month and a handful of stocks is a small sample; luck dominates. This is an experiment, not investment advice.
