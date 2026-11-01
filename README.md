# Trader HQ

Claude suggests stock trades for an **Alpaca paper account** (simulated money). Claude never places orders itself: you approve every order, and the script blocks any order that breaks your limits.

## Setup
1. Make a free account at [alpaca.markets](https://alpaca.markets). In the dashboard, switch to your **Paper** account and generate API keys (they start with `PK`).
2. Install and connect:
   ```bash
   git clone https://github.com/amharms11/Trader-HQ.git
   cd Trader-HQ
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python paper_trader.py setup
   ```
3. Claude signs in through Claude Code if you're logged in (`claude`), or with `ANTHROPIC_API_KEY=...` added to `~/.amh-trader/config.env`.

## Use
```bash
python paper_trader.py status    # account value, cash, positions, today's change
python paper_trader.py run       # Claude proposes trades; type y to place each one
python paper_trader.py history   # every plan, approval, skip and blocked trade
```
Orders can only be placed while the market is open (9:30am–4pm ET, weekdays). Outside those hours the plan is shown for reference only.

## Limits
Edit `~/.amh-trader/settings.json`:

| Setting | Default | Meaning |
|---|---|---|
| `watchlist` | SPY, QQQ, AAPL, MSFT, NVDA, AMZN, GOOGL, META | Only these can be bought |
| `strategy` | Long-term, low-turnover | Your instructions to Claude, in plain English |
| `max_order_usd` | 500 | Largest single order |
| `max_position_pct` | 20 | No stock may grow past this % of the account |
| `max_orders_per_day` | 5 | Orders placed by this tool per day |
| `daily_loss_stop_pct` | 3 | No new buys once the account is down this much today |
| `limit_slippage_pct` | 0.5 | Limit price stays within this % of the last price |

The script also allows whole shares only, with no short selling and no borrowing.

## Safety
- Only paper keys are accepted, and the broker address is fixed to Alpaca's paper endpoint, so this tool cannot trade real money.
- Claude sees only prices and your paper account numbers, not news or web pages, so nothing it reads can steer it into a trade.
- Keys, settings and the trade journal are stored only on your computer in `~/.amh-trader/`.

## Tests
```bash
python -m unittest test_paper_trader.py
```
No network or keys needed.

Run it on paper for a few months and compare the result with simply holding SPY before you consider anything else. This is not financial advice.
