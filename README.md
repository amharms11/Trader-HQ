# Trader HQ

Claude suggests stock trades for your Alpaca account, either longer-term or day trades. Claude never places orders itself. You approve every order, and the script blocks any order that breaks your limits.

It starts on **paper** (simulated money). You can link a **live** account now, but it is only used if you switch it on and type `LIVE` each time you start.

## Setup
1. Make a free account at [alpaca.markets](https://alpaca.markets). In the dashboard, switch to your **Paper** account and generate API keys (they start with `PK`).
2. Install and connect:
   ```bash
   git clone https://github.com/amharms11/Trader-HQ.git
   cd Trader-HQ
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python trader.py setup
   ```
3. Claude signs in through Claude Code if you're logged in (`claude`), or with `ANTHROPIC_API_KEY=...` added to `~/.amh-trader/config.env`.

## Commands
```bash
python trader.py status      # account value, cash, positions, day trades used
python trader.py run         # longer-term: Claude proposes trades; type y to place each one
python trader.py day         # day trading until near the close (see below)
python trader.py day once    # one day-trading check
python trader.py flatten     # sell everything day trading bought today, right now
python trader.py history     # every plan, approval, skip, block and order
```
Orders can only be placed while the market is open (9:30am–4pm ET, weekdays).

## Day trading
`python trader.py day` checks the market every 15 minutes. Each time, Claude looks at today's 5-minute price bars (VWAP, trend, volume) for your day watchlist and proposes up to 2 actions, or none.

- **Every buy comes with a stop-loss and a take-profit** that are placed with Alpaca as one bracket order. They protect the position even if your computer goes to sleep.
- **Risk per trade is capped.** Shares × (entry − stop) must stay under `max_risk_per_trade_usd` ($25 by default).
- **No new entries in the last 30 minutes**, and **everything bought by day trading is sold 10 minutes before the close.**
- Day trading never touches your long-term holdings. It only sells shares it bought that day.
- **Pattern-day-trader rule:** US rules allow accounts under $25,000 only 3 day trades in 5 business days. The bot stops opening day trades when you reach that limit. `status` shows how many you've used.
- If you stop it with Ctrl+C, your stops and targets stay active until the close. Run `flatten` to close day positions right away, or `day` again to resume.

Keep the window open while it runs: every trade waits for your `y`. On paper only, you can set `"auto_approve": true` to let trades that pass every limit go through without asking, so you can test it unattended. Auto-approve is ignored in live mode.

## Linking a live account (for later)
```bash
python trader.py setup live   # paste your LIVE keys (they start with AK)
```
This saves the keys and checks they work. Trading stays on paper. To trade real money, set `"mode": "live"` in `~/.amh-trader/settings.json`. Every command will then show a warning and ask you to type `LIVE` before doing anything, and every order still needs your `y`. Set it back to `"paper"` at any time.

Paper keys only ever go to Alpaca's paper address and live keys only to the live one, so they can't be mixed up.

## Limits
Edit `~/.amh-trader/settings.json`:

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `paper` | `paper` or `live` |
| `watchlist` | SPY, QQQ, AAPL, MSFT, NVDA, AMZN, GOOGL, META | Longer-term: only these can be bought |
| `strategy` | Long-term, low-turnover | Your instructions to Claude, in plain English |
| `max_order_usd` | 500 | Largest single order (both modes) |
| `max_position_pct` | 20 | No stock may grow past this % of the account |
| `max_orders_per_day` | 5 | Longer-term orders per day |
| `daily_loss_stop_pct` | 3 | No new buys once the account is down this much today |
| `limit_slippage_pct` | 0.5 | Limit price stays within this % of the last price |
| `auto_approve` | false | Paper only: skip the y/n for trades that pass every limit |
| `day.watchlist` | SPY, QQQ, AAPL, MSFT, NVDA, AMZN, META | Day trading: only these can be bought |
| `day.strategy` | Cautious, pullbacks to VWAP | Your day-trading instructions to Claude |
| `day.check_every_min` | 15 | Minutes between checks |
| `day.max_entries_per_day` | 6 | New day trades per day |
| `day.max_risk_per_trade_usd` | 25 | Most you can lose on one trade if its stop is hit |
| `day.max_stop_loss_pct` | 2 | Widest stop-loss allowed |
| `day.max_take_profit_pct` | 6 | Furthest profit target allowed |
| `day.no_new_entries_min_before_close` | 30 | No new day trades this close to 4pm |
| `day.flatten_min_before_close` | 10 | Sell all day positions this close to 4pm |

You can leave out any setting to keep its default. The script also allows whole shares only, with no short selling and no borrowing.

## Safety
- Claude sees only prices and your account numbers, not news or web pages, so nothing it reads can steer it into a trade.
- Keys, settings, the day-trading state and the trade journal are stored only on your computer in `~/.amh-trader/`.
- Every check uses Claude, which costs money if you use an API key. That's about 26 calls per day at the default 15-minute interval.

## NinjaTrader 8: Gold ORB
`ninjatrader/` holds a rules-based gold opening range breakout strategy for NinjaTrader 8 (futures, Windows). It runs on its own, without Claude. See [ninjatrader/README.md](ninjatrader/README.md) for a beginner's guide to installing and testing it.

## Tests
```bash
python -m unittest test_trader.py
```
No network or keys needed.

Most day traders lose money. Run it on paper for a few months, compare the result with simply holding SPY, and only consider live trading with money you can afford to lose. This is not financial advice.
