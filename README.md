# Trader HQ

Claude suggests trades for your Alpaca account: longer-term stocks, day trades, and calls/puts. Claude never places orders itself. You approve every order, and the script blocks any order that breaks your limits.

It starts on **paper** (simulated money). You can link a **live** account now, but it is only used if you switch it on and type `LIVE` each time you start.

## Install or update on a Mac (one line)
Open **Terminal**, paste this and press Return:
```bash
curl -fsSL https://raw.githubusercontent.com/amharms11/Trader-HQ/main/install.sh | bash
```
It downloads the latest Trader HQ into `~/Trader-HQ`, sets it up, puts a **Trader HQ** icon on your Desktop and opens it. Your keys, settings and history (in `~/.amh-trader`) are kept. Run the same line any time to repair or reinstall.

**It keeps itself up to date:** every time you open Trader HQ it checks GitHub and installs any new version before the window opens, and refreshes the Desktop icon if needed. If an update arrives while it's open, a banner offers **Restart now**.

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

## Linking Alpaca
**Paper (fake money, start here):**
1. Sign up at [alpaca.markets](https://alpaca.markets). A paper account with simulated money is created for you, with no deposit or ID check.
2. In the dashboard, make sure the account switcher at the top says **Paper**. On the home page, find **API Keys** and click **Generate New Keys**.
3. Copy the **Key** (starts with `PK`) and the **Secret**. The secret is shown only once; if you lose it, generate new keys.
4. On your Mac, in the Trader-HQ folder, run `python trader.py setup` and paste them.
5. Run `python trader.py status` to check the connection.

Paper accounts can trade calls and puts straight away.

**Live (real money, later):**
1. In the dashboard, switch to **Live** and complete the account application (identity, address, employment, investing experience).
2. Fund it from your bank under **Banking**.
3. To trade calls and puts, apply for **options trading**. Buying calls and puts needs **level 2**.
4. Generate **Live** API keys (they start with `AK`) and run `python trader.py setup live`.
5. Trading stays on paper until you change `"mode"` to `"live"` (see below).

## Desktop app (no Terminal needed)
Trader HQ has a dashboard that runs the bot in its own window:

- Account value, today's change, cash and options at a glance, with an account-value chart (1W / 1M / 3M / 1Y)
- Buttons to ask Claude for a long-term plan or an options plan, and to start or stop day trading
- Each suggestion appears as a card with **Why**, **Risk**, and **Approve** / **Skip** buttons
- Tabs for positions, open orders (with Cancel), activity and history
- **Settings** for every limit and strategy, plus your Alpaca keys and an optional Anthropic API key

**Keep the Trader-HQ folder in your home folder** (for example `~/Trader-HQ`), not on the Desktop or in Documents or Downloads. macOS doesn't let a double-clicked app read those three folders, so the icon couldn't start the bot.

**Set it up once** (in Terminal, inside the Trader HQ folder):
```bash
source .venv/bin/activate
python trader.py install-app
```
This puts a **Trader HQ** icon on your Desktop. From then on, just double-click it. It opens in its own window (an app-style Chrome window if you have Google Chrome, otherwise Safari).

**Good to know:**
- The bot keeps running in the background while the dashboard is open. Closing the window doesn't stop it; press **Quit** in the dashboard to stop it.
- Suggestions expire so you never approve on stale prices: day-trade suggestions after 3 minutes, others after 30 minutes. Every approval is checked against your limits again at the moment you click.
- In live mode, the app asks you to type LIVE each time it starts. Auto-approve only works on paper.
- Only your own Mac can reach the dashboard (it listens on 127.0.0.1 only), and other websites can't send it orders.
- If you move the Trader HQ folder, run `python trader.py install-app` again from its new location.
- Problems starting? See `~/.amh-trader/app.log`.

## Markets, charts, scanner and order ticket
The dashboard is also a stock, ETF and crypto research tool. Use the tabs at the top: **Bot**, **Markets**, **Scanner**, **Orders**, or search any symbol or company name.

- **Markets:** every stock, ETF and crypto pair Alpaca can trade, with price, daily change and volume. Over-the-counter stocks are left out. ETFs are recognized by their names, so a few may show under Stocks.
- **Symbol pages:** click any row, search result or holding to open its page:
  - **Chart:** 1 day, 1 week, 1 month, 3 months, year to date, 1 year, 5 years or all, as candles or a line. It updates every 2 seconds while the market is open, and any time for crypto.
  - **Technical studies:** volume, moving averages (SMA 20/50/200, EMA 9/21), VWAP, Bollinger bands, RSI and MACD.
  - **Key stats:** open, high, low, volume, 52-week range, market cap, P/E, EPS and shares outstanding.
  - **About:** the company profile and a description, plus yearly revenue, profit and EPS from SEC filings.
  - **News**, and **your position** with *Protect with OCO* and *Sell all* buttons.
- **Scanner:** today's top gainers and losers (stocks and crypto) and the most active stocks. A **custom scan** checks every listed stock, ETF or crypto pair against filters for price, % change, volume, gap and volume vs yesterday. One-click presets include gainers, losers, gap up/down, unusual volume and near the day's high.
- **Order ticket** on every symbol page:
  - **Order types:** market, limit, stop (stop-loss), stop-limit and trailing stop.
  - **Strategies:**
    - **Bracket:** buy with a take-profit and a stop-loss attached.
    - **OTO:** buy, then place one exit order once it fills.
    - **OCO:** a take-profit and a stop-loss on shares you own, where one cancels the other.
  - **Amounts and timing:** dollar amounts or fractional shares where Alpaca allows them, Day or GTC, and pre/after-market for limit orders.
  - **Speed:** orders go straight to Alpaca, and the app shows how many milliseconds the round trip took.
- **Orders:** your last 60 orders, including the take-profit and stop-loss parts, with Cancel buttons.

**Safety for orders you place yourself:**
- The ticket won't sell shares you don't own (no short selling).
- It won't spend more cash than you have.
- It won't place a buy larger than **Largest buy you place yourself** in Settings (default $1,000).
- In live mode you must type LIVE, and every order goes through a review step. On paper you can turn the review step off for one-click orders.

**Where the data comes from:**
- **Prices, charts, screeners and news:** Alpaca.
  - The free **IEX** feed is real-time but covers only one exchange, so its volume is a small share of the market.
  - If you buy Alpaca's full-market data plan, choose **SIP** in Settings.
  - Free price history starts in 2016.
- **Company facts and financials:** SEC EDGAR, free. Adding your email in Settings is optional, but the SEC asks for it.
- **Descriptions:** Wikipedia, matched by company name.
- **Optional:** a free **Finnhub** API key (Settings) adds logos, dividend yield and beta.
- The asset lists and fetched data are cached on your Mac in `~/.amh-trader/market.db`.
- None of this text is sent to Claude. The bot still only sees prices and account numbers.

Charts use [TradingView Lightweight Charts™](https://www.tradingview.com/) (Apache 2.0, see `vendor/`).

## Live: watch the bot trade
The **Live** tab is for watching the bot work:
- **Start/stop controls** at the top for day trading and the crypto bot, with a countdown to the next check.
- **Waiting for your approval:** the bot's suggestions, with Approve and Skip buttons.
- **A live chart** of the stock it's trading (today's 5-minute candles, updating every 2 seconds). It shows **▲ buy** and **▼ sell** markers where the bot traded, and lines for the **stop-loss**, **target** and **your entry**. Click a holding or a symbol chip to switch.
- **Live feed:** each check, Claude's reasoning, and every order as it happens.
- **Open trades** with live profit or loss and how far each is from its stop and target, plus **today's scoreboard**.

**Auto-approve** (on the Live tab and the Bot page) lets the bot trade on its own while you watch, on paper only:
- a **switch** to turn it on or off
- a **slider** for the largest trade it may approve without asking ($0 to $2,000)
- **checkboxes** for which bots it applies to: long-term, day trading, options, crypto

Bigger trades, and trades from bots you didn't tick, still wait for your Approve. All the normal limits still apply. In live mode, every trade always asks.

## Calendar
The **Calendar** tab shows every day's profit or loss on a monthly calendar:
- ▲ green days and ▼ red days, with the dollar and % change; the color is deeper for bigger moves
- how many trades happened that day
- month totals, up vs. down days, best and worst day, and realized gains from closed trades
- arrows to move between months, plus a month-by-month summary below

It uses your account's daily value, so it includes open positions as well as closed trades.

## Watchlists, alerts, recurring buys, reports and tools
- **Watchlists** (Bot page): keep lists of stocks, ETFs and crypto with live prices. Star any symbol with **☆ Watch** on its page. One click lets the long-term, day-trading, options or crypto bot watch a list, and the Scanner can scan your watchlists.
- **Price alerts** (Tools page or any symbol page): for example "AAPL below $180" or "BTC moves 5% today". Checked every 20 seconds while Trader HQ is open, with a Mac notification.
- **Recurring buys** (dollar-cost averaging): a fixed dollar amount every week, every two weeks or monthly. Fractional shares are used where allowed. They stay within your cash and your *Largest buy you place yourself* limit. In live mode they only run after you've typed LIVE that session.
- **Automatic trailing stops** (Tools): every stock you own gets a trailing stop, for example 8% below its highest price. Positions that already have a bracket or OCO are left alone. Selling yourself or with the bot removes the stop first.
- **Crypto bot** (Bot page): Claude suggests small crypto buys and sells, on demand or 24/7 while Trader HQ is open, within its own limits (Settings → Crypto bot). Because Alpaca crypto has no bracket orders, each buy gets a stop-loss placed right after it fills.
- **Ask Claude about this stock:** a plain-English explanation on every symbol page covering what it is, how it's been doing, what the numbers mean and the main risks. It explains only. It never places trades or tells you to buy or sell.
- **Compare charts:** type up to 3 other symbols under any chart to see their % change on one chart.
- **Earnings and dividends:** upcoming dividends from Alpaca, and earnings dates with a free Finnhub key. Buy suggestions from the bot warn you when earnings are within a week. Tools has a calendar for everything you own or watch.
- **Reports:**
  - **Performance vs. the S&P 500:** your account against simply holding SPY, plus win rate, average win and loss, worst drop and worst losing streak, broken down by strategy (long-term, day trading, options, crypto, recurring buys, your own orders).
  - **Daily summaries:** created after the close on weekdays, with a Mac notification.
  - **Tax report:** realized gains and losses for any year, short vs. long term, with a CSV download. It's an estimate from your Alpaca orders using first-in, first-out. Your broker's 1099 is the official record.
- **Backtester** (Tools): test simple rules on past daily prices against buy-and-hold: moving-average crossover, RSI buy-the-dip, breakouts, and monthly recurring buys. It assumes no fees or slippage, so real results are usually worse.

Background jobs (alerts, recurring buys, trailing stops, crypto stop-losses and daily summaries) run only while Trader HQ is open.

## Commands
```bash
python trader.py status      # account value, cash, positions (stocks and options)
python trader.py run         # longer-term: Claude proposes trades; type y to place each one
python trader.py day         # day trading until near the close (see below)
python trader.py day once    # one day-trading check
python trader.py options     # calls and puts (see below)
python trader.py flatten     # sell everything day trading bought today, right now
python trader.py history     # every plan, approval, skip, block and order
python trader.py app         # open the dashboard from Terminal
python trader.py install-app # put the Trader HQ icon on your Desktop
```
Orders can only be placed while the market is open (9:30am–4pm ET, weekdays).

## Day trading
`python trader.py day` checks the market every 15 minutes. Each time, Claude looks at today's 5-minute price bars (VWAP, trend, volume) for your day watchlist and proposes up to 2 actions, or none.

- **Every buy comes with a stop-loss and a take-profit** that are placed with Alpaca as one bracket order. They protect the position even if your computer goes to sleep.
- **Risk per trade is capped.** Shares × (entry − stop) must stay under `max_risk_per_trade_usd` ($25 by default).
- **No new entries in the last 30 minutes**, and **everything bought by day trading is sold 10 minutes before the close.**
- Day trading never touches your long-term holdings. It only sells shares it bought that day.
- If you stop it with Ctrl+C, your stops and targets stay active until the close. Run `flatten` to close day positions right away, or `day` again to resume.

Keep the window open while it runs: every trade waits for your `y`. On paper only, you can set `"auto_approve": true` to let trades that pass every limit go through without asking, so you can test it unattended. Auto-approve is ignored in live mode.

## Calls and puts
`python trader.py options` asks Claude to look at a few stocks/ETFs and a short list of option contracts, then propose up to 3 actions.

**In plain English:**
- A **call** gains value if the stock goes **up**. A **put** gains value if it goes **down**.
- Each contract covers 100 shares. A price of $1.20 means $120 per contract.
- Every option has an **expiration date**. If the stock hasn't moved enough by then, it can be worth $0.

**How your money is protected:**
- The bot only **buys** calls and puts, and later **sells the ones you own**. It never sells options you don't own (that's where unlimited losses come from), and every order tells Alpaca it's "buy to open" or "sell to close", so Alpaca also rejects anything else. **The most a trade can lose is what you paid.**
- Claude can only pick from a list the script builds: contracts within 10% of the stock price, 7–60 days to expiry, with a narrow gap between the buying and selling price, costing at most $200.
- Limits: $200 per trade, at most 2 contracts per trade, 3 buys a day, and all options together no more than 10% of the account.
- Options you hold within **2 days of expiry** are offered for sale first every time you run it. If you keep one through expiry and it's worth something, it can be turned into 100 shares per contract, which needs much more cash. Selling first avoids that.
- Orders are limit orders between the bid and ask, so you never pay more than the quoted price.

Run it during market hours. Option quotes come from Alpaca's free feed, which can lag slightly; the limit price still caps what you pay.

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
| `options.watchlist` | SPY, QQQ, AAPL, MSFT, NVDA | Stocks/ETFs whose calls and puts may be bought |
| `options.strategy` | Small directional bets, 3–8 weeks out | Your options instructions to Claude |
| `options.max_premium_per_trade_usd` | 200 | Most one options order can cost (and lose) |
| `options.max_total_options_pct` | 10 | All options held may cost at most this % of the account |
| `options.max_contracts_per_trade` | 2 | Contracts per order |
| `options.max_trades_per_day` | 3 | Options buys per day |
| `options.min_days_to_expiry` / `max_days_to_expiry` | 7 / 60 | Only contracts expiring in this window |
| `options.close_days_before_expiry` | 2 | Offer to sell options this close to expiry |
| `options.max_spread_pct` | 15 | Skip contracts with a wide gap between bid and ask |

You can leave out any setting to keep its default. Settings inside `day` and `options` go in those sections of the file, for example `"options": {"max_premium_per_trade_usd": 100}`. The script also allows whole shares only, with no short selling and no borrowing.

## Safety
- Claude sees only prices and your account numbers, not news or web pages, so nothing it reads can steer it into a trade.
- Keys, settings, the day-trading state and the trade journal are stored only on your computer in `~/.amh-trader/`.
- Every check uses Claude, which costs money if you use an API key. That's about 26 calls per day at the default 15-minute interval.

## NinjaTrader 8: Gold ORB
`ninjatrader/` holds a rules-based gold opening range breakout strategy for NinjaTrader 8 (futures, Windows). It runs on its own, without Claude. See [ninjatrader/README.md](ninjatrader/README.md) for a beginner's guide to installing and testing it.

## Tests
```bash
python -m unittest test_trader.py test_desktop.py test_market.py test_updater.py test_features.py
```
No network or keys needed.

Most day traders and most options buyers lose money. Run it on paper for a few months, compare the result with simply holding SPY, and only consider live trading with money you can afford to lose. This is not financial advice.
