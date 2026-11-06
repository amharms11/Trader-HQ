#!/usr/bin/env python3
"""
Trader HQ - Claude suggests stock trades, you approve each one, and they are placed in your
Alpaca account. Paper (simulated money) is the default; a live account can be linked for later.

Commands:
  python trader.py setup            # save your Alpaca PAPER keys and default settings
  python trader.py setup live       # link a LIVE account for later (keeps trading on paper)
  python trader.py status           # account value, cash, positions, today's change
  python trader.py run              # longer-term: Claude proposes trades; you approve each one
  python trader.py day              # day trading: checks every few minutes until near the close,
                                    #   then sells everything it bought today
  python trader.py day once         # a single day-trading check
  python trader.py options          # calls and puts: Claude proposes buys (and sells of ones you own)
  python trader.py flatten          # sell everything day trading bought today, right now
  python trader.py history          # every plan, approval, skip, block and order
  python trader.py app              # open the desktop dashboard (runs the bot in a window)
  python trader.py install-app      # put a "Trader HQ" icon on your Desktop that opens the dashboard
  python trader.py update           # install the latest version from GitHub (the app also does this itself)

Safety by design:
- Paper unless you set "mode": "live" in settings.json AND type LIVE each time you start.
  Paper keys (PK...) only ever go to the paper endpoint; live keys (AK...) only to the live one.
- Claude never places orders. It returns proposals; this script checks them against hard limits
  (settings.json) and asks you before each order. Auto-approve exists for paper only.
- Options: only buying calls/puts and selling ones you own, so a trade can't lose more than was paid.
- Every day-trade entry carries a stop-loss and a take-profit with the broker, and day positions
  are closed before the market closes.
- Claude only sees prices and your account numbers, never web pages or news, so nothing it
  reads can steer it into placing trades.
Files live in ~/.amh-trader/ (keys in config.env, limits in settings.json, log in journal.jsonl).
"""
import copy
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path.home() / ".amh-trader"
CONFIG_FILE = DATA / "config.env"
SETTINGS_FILE = DATA / "settings.json"
JOURNAL_FILE = DATA / "journal.jsonl"
SOFI_CONFIG = Path.home() / ".sofi-tracker" / "config.env"  # reuse an Anthropic key saved for AMH Finance

ENDPOINTS = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
KEY_PREFIX = {"paper": "PK", "live": "AK"}
DATA_URL = "https://data.alpaca.markets"
ET = ZoneInfo("America/New_York")
CLAUDE_MODEL = "claude-opus-5-5"

DEFAULT_SETTINGS = {
    "mode": "paper",               # "paper" or "live"
    "watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"],
    "strategy": ("Long-term, low-turnover investing. Prefer broad index funds (SPY, QQQ) as the core. "
                 "Only add individual stocks in small amounts. Do nothing unless there is a clear reason; "
                 "'no trades' is a good answer most days."),
    "max_order_usd": 500,          # largest single order
    "max_position_pct": 20,        # no symbol may grow above this % of account value
    "max_orders_per_day": 5,       # orders placed by `run` per day
    "daily_loss_stop_pct": 3,      # no new buys once the account is down this much today
    "limit_slippage_pct": 0.5,     # limit price = last price +/- this %, so a fast move can't fill badly
    "auto_approve": False,         # paper only: place orders that pass every limit without asking
    "use_playbook": True,          # give Claude the research playbook (knowledge/*.md) for each bot
    "learn_from_trades": True,     # after trading, Claude reviews each bot's closed trades and keeps short lessons
    "phone": {
        # Phone access over your private Tailscale network, and push notifications through the ntfy app.
        "enabled": False,
        "keep_mac_awake": True,            # stop the Mac from sleeping while phone access or a bot is on
        "push_topic": "",                  # your private ntfy topic; empty = no phone notifications
        "push_approvals": True, "push_trades": True, "push_goals_alerts": True, "push_summary": True, "push_errors": True,
    },
    "auto_rules": {
        # What auto-approve may place on its own (paper only). Everything else still asks.
        "max_usd": 250,                # largest trade (buy or sell) it may approve, in dollars
        "long_term": True, "day": True, "options": False, "crypto": True,
    },
    "day": {
        "watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "META"],
        "strategy": ("Cautious intraday trading of liquid stocks and ETFs. Only enter with a clear setup in "
                     "the intraday data (e.g. a pullback to VWAP in an uptrend). Small size, tight stops. "
                     "Skipping is fine."),
        "check_every_min": 15,           # how often Claude looks for trades
        "max_entries_per_day": 6,        # new day-trade positions per day
        "max_risk_per_trade_usd": 25,    # loss if the stop-loss is hit: shares x (entry - stop)
        "max_stop_loss_pct": 2,          # widest stop-loss allowed
        "max_take_profit_pct": 6,
        "no_new_entries_min_before_close": 30,
        "flatten_min_before_close": 10,  # sell all day positions this many minutes before the close
        # Fast mode: Claude arms setups every few minutes; the app watches prices every few seconds and buys
        # the moment one triggers, with a bracket order (stop-loss and target at Alpaca).
        "fast_mode": False,
        "fast_orb": True,                  # opening range breakout setups
        "fast_vwap_pullback": True,        # VWAP pullback setups
        "fast_plan_every_min": 5,          # how often Claude looks for new setups in fast mode
        "fast_watch_sec": 2,               # how often the app checks prices of armed setups
        "fast_arm_minutes": 20,            # longest a setup stays armed without triggering
        "max_chase_pct": 0.3,              # don't buy if the price is already this far past the trigger
        "max_spread_pct": 0.15,            # don't buy if the bid/ask gap is wider than this
        "time_stop_min": 45,               # sell a fast trade that hasn't hit its target or stop after this (0 = off)
        "cooldown_after_loss_min": 15,     # no new fast entries for this long after a losing trade
        "max_losses_per_day": 3,           # stop fast entries for the day after this many losing trades
        "max_open_fast_trades": 2,
        "daily_profit_target_pct": 0,      # >0: shut this bot off for the day once it's up this % of the portfolio
        "daily_loss_limit_pct": 0,         # >0: shut this bot off for the day once it's down this % of the portfolio
        "run_from_hhmm": 0,                # run hours in New York time, e.g. 945 to 1530; 0 and 0 = any time
        "run_to_hhmm": 0,
    },
    "options": {
        # Buying calls and puts only: the most any trade can lose is what was paid for it.
        "watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
        "strategy": ("Occasional, small directional bets with calls (expecting a rise) or puts (expecting a "
                     "fall) only when the price data shows a clear trend. Prefer 3-8 weeks to expiry and strikes "
                     "near the current price. 'No trades' is a good answer most days."),
        "max_premium_per_trade_usd": 200,   # most one options order can cost (= the most it can lose)
        "max_total_options_pct": 10,        # all options held may cost at most this % of the account
        "max_contracts_per_trade": 2,
        "max_trades_per_day": 3,
        "min_days_to_expiry": 7,            # options lose value fastest in their last week
        "max_days_to_expiry": 60,
        "close_days_before_expiry": 2,      # offer to sell options this close to expiry
        "max_spread_pct": 15,               # skip contracts whose bid/ask gap is wider than this % of the price
    },
    "market": {
        # The dashboard's charts, screeners and the order ticket for trades you place yourself.
        "data_feed": "iex",                 # "iex" is free; "sip" (all exchanges) needs a paid Alpaca data plan
        "max_manual_order_usd": 1000,       # largest buy you can place yourself from the order ticket
        "confirm_manual_orders": True,      # show a review step before sending (always on in live mode)
        "sec_contact_email": "",            # optional: SEC asks data users for a contact email
        "auto_trailing_stop_pct": 0,        # >0: give every stock position a trailing stop this % below its high
        "mac_notifications": True,          # show alerts and summaries as Mac notifications
    },
    "crypto": {
        # The Claude crypto bot (24/7). Crypto is very volatile: keep these small.
        "watchlist": ["BTC/USD", "ETH/USD", "SOL/USD"],
        "strategy": ("Cautious crypto trading. Buy small amounts only after a clear pullback within an uptrend; "
                     "take partial profits after strong runs. Doing nothing is usually right."),
        "check_every_min": 30,
        "max_order_usd": 200,
        "max_position_pct": 10,             # no coin above this % of the account
        "max_stop_loss_pct": 8,             # widest stop-loss allowed on a crypto buy
        "max_take_profit_pct": 15,          # furthest target allowed on a crypto buy
        "daily_profit_target_pct": 0,      # >0: shut this bot off for the day once it's up this % of the portfolio
        "daily_loss_limit_pct": 0,         # >0: shut this bot off for the day once it's down this % of the portfolio
        "run_from_hhmm": 0,                # run hours in New York time, e.g. 945 to 1530; 0 and 0 = any time
        "run_to_hhmm": 0,
    },
    "crypto_day": {
        # Crypto day trading: short trades with a stop-loss and a target, closed by a daily time (crypto never closes).
        "watchlist": ["BTC/USD", "ETH/USD"],
        "strategy": ("Cautious intraday crypto trading. Only enter on a clear setup in the 5-minute data, like a "
                     "pullback to VWAP in an uptrend. Small size, tight stops. Skipping is fine."),
        "check_every_min": 15,
        "max_entries_per_day": 6,
        "max_order_usd": 300,
        "max_risk_per_trade_usd": 15,       # loss if the stop-loss is hit
        "max_stop_loss_pct": 3,
        "max_take_profit_pct": 6,
        "close_all_hhmm": 2330,             # sell all crypto day trades at this New York time every day
        "around_the_clock": False,          # True: trade 24/7 with no daily close-all and no run hours
        "max_hold_hours": 8,                # sell any crypto day trade still open after this many hours (0 = no limit)
        "meme_max_order_usd": 75,           # meme coins (DOGE, SHIB, PEPE, BONK, WIF, TRUMP) get smaller trades...
        "meme_max_stop_loss_pct": 8,        # ...wider stops, because they swing 10-20% in a day...
        "meme_max_take_profit_pct": 20,
        "max_meme_trades_open": 1,          # ...and only this many open at once
        # Fast mode: catch small swings with limit orders (about 0.15% fee each way instead of 0.25%).
        # Claude arms dip-buy setups; a limit buy waits at the entry, a limit sell at the target, the app watches the stop.
        "fast_mode": False,
        "fast_plan_every_min": 5,           # how often Claude looks for swing setups
        "fast_watch_sec": 2,                # how often the app checks the price against stops
        "fast_arm_minutes": 30,             # longest a limit buy waits to fill
        "fast_min_target_pct": 0.6,         # smallest target allowed (the limit-order round trip costs about 0.3%)
        "max_open_fast_trades": 2,
        "cooldown_after_loss_min": 15,
        "max_losses_per_day": 3,
        "daily_profit_target_pct": 0,
        "daily_loss_limit_pct": 0,
        "run_from_hhmm": 800,
        "run_to_hhmm": 2300,
    },
}


# ---------- config ----------

def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def load_config():
    load_env(CONFIG_FILE)
    load_env(SOFI_CONFIG)


def load_settings():
    s = copy.deepcopy(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        saved = json.loads(SETTINGS_FILE.read_text())
        sections = {k: saved.pop(k, {}) for k in ("day", "options", "market", "crypto", "crypto_day", "phone", "auto_rules")}
        s.update(saved)
        for k, v in sections.items():
            s[k].update(v)
    for section in (s, s["day"], s["options"], s["crypto"], s["crypto_day"]):
        section["watchlist"] = [str(x).upper().strip() for x in section["watchlist"] if str(x).strip()]
    if s["market"]["data_feed"] not in ("iex", "sip"):
        s["market"]["data_feed"] = "iex"
    if s["mode"] not in ENDPOINTS:
        raise RuntimeError(f'"mode" in {SETTINGS_FILE} must be "paper" or "live".')
    return s


def save_private(path, text):
    DATA.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def key_names(mode):
    return f"ALPACA_{mode.upper()}_KEY_ID", f"ALPACA_{mode.upper()}_SECRET_KEY"


def has_keys(mode):
    load_config()
    kid, _ = key_names(mode)
    key = os.environ.get(kid) or (os.environ.get("ALPACA_API_KEY_ID", "") if mode == "paper" else "")
    return key.startswith(KEY_PREFIX[mode])


def save_keys(mode, key, secret):
    """Check the keys with Alpaca, then save them. Returns the account. Raises ValueError if they're wrong."""
    key, secret = key.strip(), secret.strip()
    if not key.startswith(KEY_PREFIX[mode]):
        raise ValueError(f"{mode.title()} keys start with {KEY_PREFIX[mode]}. Nothing saved.")
    if not secret:
        raise ValueError("The secret is empty. Nothing saved.")
    kid, ksec = key_names(mode)
    old = os.environ.get(kid), os.environ.get(ksec)
    os.environ[kid], os.environ[ksec] = key, secret
    try:
        acct = Broker(mode).account()
    except (RuntimeError, SystemExit) as e:
        for name, value in zip((kid, ksec), old):  # put back whatever was there
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        raise ValueError(f"Alpaca didn't accept those keys ({e}). Nothing saved.") from None
    keep = []
    if CONFIG_FILE.exists():  # keep everything else, e.g. the other account's keys or ANTHROPIC_API_KEY
        keep = [l for l in CONFIG_FILE.read_text().splitlines() if l.split("=", 1)[0].strip() not in (kid, ksec)]
    save_private(CONFIG_FILE, "\n".join([f"{kid}={key}", f"{ksec}={secret}"] + keep) + "\n")
    if not SETTINGS_FILE.exists():
        save_private(SETTINGS_FILE, json.dumps(DEFAULT_SETTINGS, indent=2) + "\n")
    return acct


def setup(mode="paper"):
    if mode not in ENDPOINTS:
        sys.exit("Use `setup` for paper or `setup live` for a live account.")
    print(f"Trader HQ setup - {mode.upper()} account\n")
    print("1. Make an account at https://alpaca.markets.")
    if mode == "paper":
        print("2. In the dashboard, switch to your Paper account and click 'Generate API keys'.")
        print("   Paper keys start with PK. No deposit is needed.\n")
    else:
        print("2. Fund your live account, then in the dashboard (Live) click 'Generate API keys'.")
        print("   Live keys start with AK. Saving them does NOT switch trading to live.\n")
    key = input("API Key ID: ").strip()
    secret = input("API Secret: ").strip()
    try:
        acct = save_keys(mode, key, secret)
    except ValueError as e:
        sys.exit(str(e))
    print(f"\nConnected to your {mode} account. Value: {money(acct['equity'])}")
    if mode == "live":
        print(f'Trading stays on paper. To trade real money later, set "mode": "live" in {SETTINGS_FILE};\n'
              "you will be asked to type LIVE every time you start.")
    print(f"Edit your watchlists, strategies and limits in {SETTINGS_FILE}")
    if not claude_backend():
        print("\nClaude isn't set up yet: sign in to Claude Code (`claude`), or add "
              f"ANTHROPIC_API_KEY=... to {CONFIG_FILE}")


def confirm_mode(settings):
    if settings["mode"] != "live":
        print("Mode: PAPER (simulated money)\n")
        return
    print("=" * 64)
    print("  LIVE MODE: orders you approve will use REAL MONEY.")
    print(f'  To go back to paper, set "mode": "paper" in {SETTINGS_FILE}')
    print("=" * 64)
    if input("Type LIVE to continue: ").strip() != "LIVE":
        sys.exit("Not confirmed. Nothing was done.")
    print()


# Coin lists for one-click setup. Meme coins get their own, stricter limits in crypto day trading.
BLUE_CHIP_COINS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "LINK/USD", "AVAX/USD", "LTC/USD", "BCH/USD", "AAVE/USD"]
MEME_COINS = ["DOGE/USD", "SHIB/USD", "PEPE/USD", "BONK/USD", "WIF/USD", "TRUMP/USD"]


# Meme coin names, so any Alpaca lists later are treated as meme coins too (smaller size, wider stops).
MEME_NAMES = {m.split("/")[0] for m in MEME_COINS} | {"FLOKI", "PENGU", "FARTCOIN", "POPCAT", "MOODENG", "BRETT", "MEW",
                                                      "SPX", "TURBO", "NEIRO", "GOAT", "PNUT", "MOG", "BOME", "WOJAK", "DOGS"}
STABLECOINS = {"USDC", "USDT", "DAI", "PYUSD", "USDG", "TUSD", "FDUSD", "USDE", "RLUSD", "USD1", "GUSD", "USDP"}


def is_meme(symbol):
    return str(symbol).upper().split("/")[0] in MEME_NAMES


AUTO_KINDS = {"run": "long_term", "day": "day", "options": "options", "crypto": "crypto", "crypto_day": "crypto"}


def hhmm_minutes(v):
    v = int(v or 0)
    return (v // 100) * 60 + v % 100


def in_run_window(section, now=None):
    """True if `now` (New York time) is inside the section's run hours. 0 to 0 means any time;
    a start later than the end means overnight, e.g. 2200 to 0600."""
    start, end = hhmm_minutes(section.get("run_from_hhmm")), hhmm_minutes(section.get("run_to_hhmm"))
    if start == end:
        return True
    now = now or dt.datetime.now(ET)
    m = now.hour * 60 + now.minute
    return start <= m < end if start < end else (m >= start or m < end)


def fmt_hhmm(v):
    m = hhmm_minutes(v)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d} {'AM' if h < 12 else 'PM'}"


def can_auto_approve(settings, kind=None, value=None):
    """Auto-approve only works on paper. With a kind and a dollar value, it also checks the user's rules:
    that bot must be allowed and the trade must be no bigger than the slider amount."""
    if not settings.get("auto_approve") or settings["mode"] != "paper":
        return False
    if kind is None:
        return True
    rules = settings.get("auto_rules") or {}
    if not rules.get(AUTO_KINDS.get(kind, kind)):
        return False
    return value is not None and value <= float(rules.get("max_usd") or 0)


def auto_size_cap(settings, kind):
    """With auto-approve on for this bot (paper only), the slider amount: buys are sized to fit under it so they're
    placed without asking. None when auto-approve doesn't apply."""
    if not can_auto_approve(settings) or not (settings.get("auto_rules") or {}).get(AUTO_KINDS.get(kind, kind)):
        return None
    cap = float((settings.get("auto_rules") or {}).get("max_usd") or 0)
    return cap if cap > 0 else None


def shrink_qty(t, price, cap, slip_pct=0.0):
    """A copy of a share-count buy cut down to fit under cap dollars (never below 1 share)."""
    if not cap or not price or t.get("side") != "buy":
        return t
    fit = int(cap / (price * (1 + slip_pct / 100) + 0.01))
    return dict(t, qty=fit) if 1 <= fit < t.get("qty", 0) else t


def trade_value(kind, t, o):
    """Rough dollar size of a checked trade, for the auto-approve limit."""
    o = o or {}
    for k in ("cost", "amount", "value"):
        if o.get(k) is not None:
            return float(o[k])
    mult = 100 if kind == "options" else 1
    return float(o.get("qty") or t.get("qty") or 0) * float(o.get("limit_price") or 0) * mult


# ---------- Alpaca ----------

class Broker:
    def __init__(self, mode="paper"):
        load_config()
        if mode not in ENDPOINTS:
            sys.exit(f"Unknown mode {mode!r}.")
        kid, ksec = key_names(mode)
        self.mode, self.base = mode, ENDPOINTS[mode]
        self.key = os.environ.get(kid, "")
        self.secret = os.environ.get(ksec, "")
        if mode == "paper" and not self.key:  # keys saved by the first version of this tool
            self.key = os.environ.get("ALPACA_API_KEY_ID", "")
            self.secret = os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not self.key:
            sys.exit(f"No {mode} keys saved. Run `python trader.py setup{' live' if mode == 'live' else ''}` first.")
        if not self.key.startswith(KEY_PREFIX[mode]):
            sys.exit(f"The saved {mode} key doesn't start with {KEY_PREFIX[mode]}. Run setup again.")

    def _req(self, base, path, params=None, body=None, method="GET"):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or "null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 401:
                raise RuntimeError(f"Alpaca rejected your {self.mode} keys. Run setup again.") from None
            raise RuntimeError(f"Alpaca error {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Couldn't reach Alpaca ({e.reason}). Check your internet connection.") from None

    def _api(self, path, **kw):
        return self._req(self.base, path, **kw)

    def account(self):
        return self._api("/v2/account")

    def positions(self):
        return self._api("/v2/positions")

    def stock_positions(self):
        return [p for p in self.positions() if p.get("asset_class", "us_equity") == "us_equity"]

    def option_positions(self):
        return [p for p in self.positions() if is_option(p)]

    def clock(self):
        return self._api("/v2/clock")

    def orders_today(self):
        start = dt.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return self._api("/v2/orders", params={"status": "all", "after": start.isoformat(), "limit": 500})

    def open_orders(self):
        return self._api("/v2/orders", params={"status": "open", "limit": 100})

    def portfolio_history(self, period="1M", timeframe="1D"):
        return self._api("/v2/account/portfolio/history", params={"period": period, "timeframe": timeframe})

    def get_order(self, order_id):
        return self._api(f"/v2/orders/{order_id}", params={"nested": "true"})

    def cancel_order(self, order_id):
        return self._api(f"/v2/orders/{order_id}", method="DELETE")

    def latest_prices(self, symbols):
        res = self._req(DATA_URL, "/v2/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": "iex"})
        return {s: float(t["p"]) for s, t in (res.get("trades") or {}).items()}

    def bars(self, symbols, timeframe, start, feed="iex"):
        out, token = {}, None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
                      "feed": feed, "adjustment": "all", "limit": 10000}
            if token:
                params["page_token"] = token
            res = self._req(DATA_URL, "/v2/stocks/bars", params)
            for s, bars in (res.get("bars") or {}).items():
                out.setdefault(s, []).extend(bars)
            token = res.get("next_page_token")
            if not token:
                return out

    def daily_bars(self, symbols, days=60):
        start = (dt.date.today() - dt.timedelta(days=days * 1.6)).isoformat()
        return {s: b[-days:] for s, b in self.bars(symbols, "1Day", start).items()}

    def intraday_bars(self, symbols):
        session_open = dt.datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        return self.bars(symbols, "5Min", session_open.isoformat())

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id):
        return self._api("/v2/orders", method="POST", body={
            "symbol": symbol, "side": side, "qty": str(qty), "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": "day",
            "client_order_id": client_order_id})

    def place_bracket_buy(self, symbol, qty, limit_price, take_profit, stop_price, client_order_id):
        return self._api("/v2/orders", method="POST", body={
            "symbol": symbol, "side": "buy", "qty": str(qty), "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "client_order_id": client_order_id})

    def option_snapshots(self, underlying, price, settings):
        """Quotes and greeks for calls and puts within 10% of the price and the allowed expiry window."""
        o = settings["options"]
        today = dt.datetime.now(ET).date()
        params = {"feed": "indicative", "limit": 1000,
                  "expiration_date_gte": (today + dt.timedelta(days=o["min_days_to_expiry"])).isoformat(),
                  "expiration_date_lte": (today + dt.timedelta(days=o["max_days_to_expiry"])).isoformat(),
                  "strike_price_gte": f"{price * 0.9:.2f}", "strike_price_lte": f"{price * 1.1:.2f}"}
        out = {}
        while True:
            res = self._req(DATA_URL, f"/v1beta1/options/snapshots/{underlying}", params)
            out.update(res.get("snapshots") or {})
            if not res.get("next_page_token") or len(out) >= 3000:
                return out
            params["page_token"] = res["next_page_token"]

    def option_quotes(self, symbols):
        res = self._req(DATA_URL, "/v1beta1/options/snapshots", {"symbols": ",".join(symbols), "feed": "indicative"})
        return res.get("snapshots") or {}

    def place_option_order(self, symbol, side, qty, limit_price, client_order_id):
        """Only ever buy-to-open or sell-to-close: the broker itself rejects selling options not owned."""
        return self._api("/v2/orders", method="POST", body={
            "symbol": symbol, "side": side, "qty": str(qty), "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": "day",
            "position_intent": "buy_to_open" if side == "buy" else "sell_to_close",
            "client_order_id": client_order_id})

    def place_market_sell(self, symbol, qty, client_order_id):
        return self._api("/v2/orders", method="POST", body={
            "symbol": symbol, "side": "sell", "qty": str(qty), "type": "market",
            "time_in_force": "day", "client_order_id": client_order_id})


# ---------- market snapshots sent to Claude ----------

def summarize_bars(bars):
    closes = [b["c"] for b in bars]
    if not closes:
        return None
    last = closes[-1]

    def change(n):
        return round((last / closes[-n - 1] - 1) * 100, 2) if len(closes) > n else None

    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets) if rets else 0
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(252) * 100 if rets else None
    return {
        "last_close": round(last, 2),
        "change_pct_1d": change(1), "change_pct_5d": change(5), "change_pct_20d": change(20),
        "change_pct_60d": change(len(closes) - 1),
        "sma_20": round(sum(closes[-20:]) / min(20, len(closes)), 2),
        "high_60d": round(max(b["h"] for b in bars), 2), "low_60d": round(min(b["l"] for b in bars), 2),
        "annualized_volatility_pct": round(vol, 1) if vol is not None else None,
    }


def summarize_intraday(bars):
    """Today's 5-minute bars -> a compact picture of the session so far."""
    if not bars:
        return None
    vol = sum(b["v"] for b in bars)
    vwap = sum((b.get("vw") or b["c"]) * b["v"] for b in bars) / vol if vol else bars[-1]["c"]
    first, last = bars[0]["o"], bars[-1]["c"]
    avg_bar_vol = vol / len(bars)
    return {
        "open": round(first, 2), "last": round(last, 2),
        "high": round(max(b["h"] for b in bars), 2), "low": round(min(b["l"] for b in bars), 2),
        "vwap": round(vwap, 2), "change_since_open_pct": round((last / first - 1) * 100, 2),
        "last_vs_vwap_pct": round((last / vwap - 1) * 100, 2),
        "last_12_closes_5min": [round(b["c"], 2) for b in bars[-12:]],
        "last_bar_volume_vs_avg": round(bars[-1]["v"] / avg_bar_vol, 2) if avg_bar_vol else None,
    }


def account_summary(acct):
    return {
        "equity": float(acct["equity"]), "cash": float(acct["cash"]),
        "equity_at_last_close": float(acct["last_equity"]),
    }


def snapshot(broker, settings):
    acct, positions = broker.account(), broker.stock_positions()
    held = {p["symbol"] for p in positions}
    symbols = sorted(set(settings["watchlist"]) | held)
    prices = broker.latest_prices(symbols)
    bars = broker.daily_bars(symbols)
    return {
        "as_of": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "account": account_summary(acct),
        "positions": [{
            "symbol": p["symbol"], "qty": float(p["qty"]), "avg_entry_price": float(p["avg_entry_price"]),
            "market_value": float(p["market_value"]), "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_pl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
        } for p in positions],
        "market": {s: {"latest_price": prices.get(s), **(summarize_bars(bars.get(s, [])) or {})}
                   for s in symbols},
    }


def day_qty(positions, state):
    """Shares bought by day trading today = held now minus held when the day started."""
    out = {}
    for p in positions:
        extra = float(p["qty"]) - state["baseline"].get(p["symbol"], 0)
        if extra >= 1:
            out[p["symbol"]] = int(extra)
    return out


def day_snapshot(broker, settings, state, minutes_left):
    acct, positions = broker.account(), broker.stock_positions()
    dq = day_qty(positions, state)
    symbols = sorted(set(settings["day"]["watchlist"]) | set(dq))
    prices = broker.latest_prices(symbols)
    intraday = broker.intraday_bars(symbols)
    daily = broker.daily_bars(symbols, days=20)
    by_sym = {p["symbol"]: p for p in positions}
    return {
        "as_of": dt.datetime.now(ET).isoformat(timespec="minutes"),
        "minutes_until_close": minutes_left,
        "account": account_summary(acct),
        "all_holdings": {p["symbol"]: float(p["qty"]) for p in positions},  # for limit checks only
        "day_positions": [{
            "symbol": s, "qty": q, "avg_entry_price": float(by_sym[s]["avg_entry_price"]),
            "current_price": float(by_sym[s]["current_price"]),
            "unrealized_pl_pct": round(float(by_sym[s]["unrealized_plpc"]) * 100, 2),
        } for s, q in dq.items()],
        "entries_today": len(state["entries"]),
        "market": {s: {"latest_price": prices.get(s),
                       "today": summarize_intraday(intraday.get(s, [])),
                       "daily_20d": summarize_bars(daily.get(s, []))} for s in symbols},
    }


# ---------- Claude ----------

SYSTEM = """You are the trade planner for a personal brokerage account. You receive the account,
current positions, and recent price statistics for a fixed list of symbols, plus the user's strategy.

Propose at most 3 trades, or none. Rules:
- Follow the user's strategy. Holding steady is usually right; propose trades only with a clear reason
  grounded in the data provided. Don't invent news, earnings, or facts that are not in the data.
- Only symbols listed in the market data. Buy only watchlist symbols. Sell only shares that are held.
- Whole shares only. Respect the limits given; orders that break them will be rejected.
- reason: 1-2 plain sentences a beginner can follow. risk: the main way this trade could lose money.
- outlook: 1-3 sentences on the account overall, honest about uncertainty. You can't predict prices."""

DAY_SYSTEM = """You are the intraday trade planner for a personal brokerage account. You are called
every few minutes during market hours. You receive the account, today's day-trade positions, and
intraday + 20-day statistics for a fixed list of liquid symbols, plus the user's strategy and limits.

Propose at most 2 actions, or none. Rules:
- Long only. "buy" opens a day trade; the broker will attach your stop_loss_pct and take_profit_pct.
  "sell" closes the WHOLE day position in that symbol (only symbols in day_positions).
- Only enter on a clear setup visible in the data; most checks should end with no trade. Never chase a
  move that has already happened. Size so that qty x price x stop_loss_pct/100 stays within
  max_risk_per_trade_usd. Take profit should be at least 1.5x the stop distance.
- Don't open new trades late in the session; everything is sold automatically before the close.
- Don't invent news or facts not in the data. Whole shares only.
- reason: 1-2 plain sentences a beginner can follow. risk: the main way this trade could lose money.
- outlook: 1-2 sentences on the session so far. You can't predict prices; say so when it's unclear.
- For sells, set stop_loss_pct and take_profit_pct to 0."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "outlook": {"type": "string"},
        "trades": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "qty": {"type": "integer"},
                "reason": {"type": "string"},
                "risk": {"type": "string"},
            },
            "required": ["symbol", "side", "qty", "reason", "risk"],
            "additionalProperties": False,
        }},
    },
    "required": ["outlook", "trades"],
    "additionalProperties": False,
}

DAY_SCHEMA = copy.deepcopy(PLAN_SCHEMA)
_day_item = DAY_SCHEMA["properties"]["trades"]["items"]
_day_item["properties"].update({"stop_loss_pct": {"type": "number"}, "take_profit_pct": {"type": "number"}})
_day_item["required"] += ["stop_loss_pct", "take_profit_pct"]


def find_claude_cli():
    for c in (shutil.which("claude"), str(Path.home() / ".local/bin/claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def claude_backend():
    load_config()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    exe = find_claude_cli()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=15)
        return "cli" if json.loads(out.stdout or "{}").get("loggedIn") else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def build_prompt(snap, settings):
    limits = {k: settings[k] for k in ("max_order_usd", "max_position_pct", "max_orders_per_day",
                                       "daily_loss_stop_pct")}
    return (f"<strategy>\n{settings['strategy']}\n</strategy>\n\n"
            f"<watchlist>{', '.join(settings['watchlist'])}</watchlist>\n\n"
            f"<limits>\n{json.dumps(limits)}\n</limits>\n\n"
            f"<snapshot>\n{json.dumps(snap, indent=1)}\n</snapshot>\n\n"
            "Propose today's trades (or none).")


def build_day_prompt(snap, settings):
    d = settings["day"]
    limits = {"max_order_usd": settings["max_order_usd"], "max_position_pct": settings["max_position_pct"],
              "daily_loss_stop_pct": settings["daily_loss_stop_pct"],
              **{k: d[k] for k in ("max_entries_per_day", "max_risk_per_trade_usd", "max_stop_loss_pct",
                                   "max_take_profit_pct", "no_new_entries_min_before_close")}}
    shown = {k: v for k, v in snap.items() if k != "all_holdings"}
    return (f"<strategy>\n{d['strategy']}\n</strategy>\n\n"
            f"<watchlist>{', '.join(d['watchlist'])}</watchlist>\n\n"
            f"<limits>\n{json.dumps(limits)}\n</limits>\n\n"
            f"<snapshot>\n{json.dumps(shown, indent=1)}\n</snapshot>\n\n"
            "Propose day-trade actions for right now (or none).")


# ---------- what the bots know ----------
KNOWLEDGE = Path(__file__).resolve().parent / "knowledge"
PLAYBOOKS = {"long_term": ["general", "long_term"], "day": ["general", "stocks_day"], "options": ["general", "options"],
             "crypto": ["general", "crypto"], "crypto_day": ["general", "crypto", "crypto_day"]}
PLAYBOOK_MAX_CHARS = 9000
knowledge_hook = None  # set by the desktop app: kind -> (extra system text, extra prompt text)


def playbook(kind):
    """The research playbook for one bot: short rules distilled from the research reports in knowledge/."""
    parts = []
    for name in PLAYBOOKS.get(kind, []):
        try:
            parts.append((KNOWLEDGE / f"playbook-{name}.md").read_text().strip())
        except OSError:
            continue
    text = "\n\n".join(p for p in parts if p)[:PLAYBOOK_MAX_CHARS]
    if not text:
        return ""
    return ("\n\n<playbook>\nResearch-based guidance the owner chose for you, distilled from studies of what has and "
            "hasn't worked. Follow it when it applies. It never overrides the rules and limits above.\n\n" + text + "\n</playbook>")


def with_knowledge(kind, prompt, system):
    """Add the bot's playbook to the system prompt and its own lessons to the prompt."""
    if not kind:
        return prompt, system
    if knowledge_hook:
        extra_system, extra_prompt = knowledge_hook(kind)
    else:
        extra_system, extra_prompt = (playbook(kind) if load_settings().get("use_playbook", True) else ""), ""
    return (extra_prompt + prompt if extra_prompt else prompt), system + extra_system


def ask_claude(prompt, system=SYSTEM, schema=PLAN_SCHEMA, effort="high", kind=None, raw=False):
    """Ask Claude for a structured answer. kind = which bot is asking (adds its playbook and lessons).
    raw = return the JSON as-is instead of checking it's a trade plan."""
    prompt, system = with_knowledge(kind, prompt, system)
    backend = claude_backend()
    if backend == "api":
        return _ask_api(prompt, system, schema, effort, raw)
    if backend == "cli":
        return _ask_cli(prompt, system, schema, raw)
    raise RuntimeError("Claude isn't set up: sign in to Claude Code (`claude`), or add "
                       f"ANTHROPIC_API_KEY=... to {CONFIG_FILE}")


def _ask_api(prompt, system, schema, effort, raw=False):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            betas=["server-side-fallback-2026-07-01"],
            # extra_body so older SDK versions still send these fields
            extra_body={"fallbacks": "default",
                        "output_config": {"effort": effort,
                                          "format": {"type": "json_schema", "schema": schema}}},
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("Your Anthropic API key wasn't accepted.")
    except anthropic.RateLimitError:
        raise RuntimeError("Claude is busy right now (rate limit). Try again in a minute.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude returned an error ({e.status_code}). Try again shortly.")
    except anthropic.APIConnectionError:
        raise RuntimeError("Couldn't reach Claude. Check your internet connection.")
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to make a plan this time.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off. Try again.")
    text = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _raw_json(text) if raw else parse_plan(text)


def _ask_cli(prompt, system, schema, raw=False):
    cmd = [find_claude_cli(), "-p", "--output-format", "json", "--tools", "", "--system-prompt", system,
           "--json-schema", json.dumps(schema), "--no-session-persistence", "--strict-mcp-config"]
    with tempfile.TemporaryDirectory() as empty_dir:  # nothing on disk for it to look at
        try:
            out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=400, cwd=empty_dir)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Claude took too long to answer. Try again.")
    try:
        res = json.loads(out.stdout)
    except ValueError:
        detail = (out.stderr or out.stdout or "no output").strip().splitlines()[-1][:200]
        raise RuntimeError(f"Claude Code didn't answer ({detail}).")
    if res.get("is_error"):
        raise RuntimeError(f"Claude Code: {str(res.get('result') or res.get('subtype'))[:300]}")
    plan = res.get("structured_output")
    if raw:
        return plan if isinstance(plan, dict) else _raw_json(res.get("result") or "")
    return validate_plan_shape(plan) if isinstance(plan, dict) else parse_plan(res.get("result") or "")


def _raw_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        out = json.loads(text)
    except ValueError:
        raise RuntimeError("Claude's answer wasn't readable. Try again.") from None
    if not isinstance(out, dict):
        raise RuntimeError("Claude's answer wasn't in the expected format. Try again.")
    return out


def ask_text(prompt, system, effort="medium"):
    """A plain-text answer from Claude (used to explain a stock; never to place trades)."""
    backend = claude_backend()
    if backend == "api":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        try:
            resp = client.beta.messages.create(
                model=CLAUDE_MODEL, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": prompt}],
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default", "output_config": {"effort": effort}},
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Your Anthropic API key wasn't accepted.")
        except anthropic.RateLimitError:
            raise RuntimeError("Claude is busy right now (rate limit). Try again in a minute.")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Claude returned an error ({e.status_code}). Try again shortly.")
        except anthropic.APIConnectionError:
            raise RuntimeError("Couldn't reach Claude. Check your internet connection.")
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude declined to answer this one.")
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if backend == "cli":
        cmd = [find_claude_cli(), "-p", "--output-format", "json", "--tools", "", "--system-prompt", system,
               "--no-session-persistence", "--strict-mcp-config"]
        with tempfile.TemporaryDirectory() as empty_dir:
            try:
                out = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300, cwd=empty_dir)
            except subprocess.TimeoutExpired:
                raise RuntimeError("Claude took too long to answer. Try again.")
        try:
            res = json.loads(out.stdout)
        except ValueError:
            raise RuntimeError("Claude Code didn't answer.")
        if res.get("is_error"):
            raise RuntimeError(f"Claude Code: {str(res.get('result') or res.get('subtype'))[:300]}")
        return (res.get("result") or "").strip()
    raise RuntimeError("Claude isn't set up: sign in to Claude Code (`claude`), or add an Anthropic API key in Settings.")


def parse_plan(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    try:
        return validate_plan_shape(json.loads(text))
    except ValueError:
        raise RuntimeError("Claude's plan wasn't readable. Try again.")


def validate_plan_shape(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("trades"), list):
        raise RuntimeError("Claude's plan wasn't in the expected format. Try again.")
    trades = []
    for t in plan["trades"]:
        try:
            trade = {"symbol": str(t["symbol"]).upper().strip(), "side": str(t["side"]).lower(),
                     "qty": int(t["qty"]), "reason": str(t.get("reason", "")), "risk": str(t.get("risk", ""))}
            for k in ("stop_loss_pct", "take_profit_pct"):
                if k in t:
                    trade[k] = float(t[k])
            trades.append(trade)
        except (KeyError, TypeError, ValueError):
            continue  # a malformed trade is dropped, never guessed at
    raw = [t for t in plan["trades"] if isinstance(t, dict)]
    return {"outlook": str(plan.get("outlook", "")), "trades": trades, "raw_trades": raw}


# ---------- hard limits (enforced here, not by Claude) ----------

def _buy_limits(sym, cost, price, held, acct, settings):
    """Checks shared by long-term and day-trade buys. Returns a reason string, or '' if OK."""
    if cost > settings["max_order_usd"]:
        return f"order is {money(cost)}, over your {money(settings['max_order_usd'])} per-order limit"
    if cost > acct["cash"]:
        return f"order is {money(cost)} but only {money(acct['cash'])} cash is available (no borrowing)"
    day_change = acct["equity"] / acct["equity_at_last_close"] - 1 if acct["equity_at_last_close"] else 0
    if day_change <= -settings["daily_loss_stop_pct"] / 100:
        return f"account is down {day_change * -100:.1f}% today; new buys are paused"
    position_value = held * price + cost
    if position_value > acct["equity"] * settings["max_position_pct"] / 100:
        return (f"{sym} would be {position_value / acct['equity'] * 100:.0f}% of the account, "
                f"over your {settings['max_position_pct']}% limit")
    return ""


def check_trade(trade, snap, settings, orders_placed_today, market_open):
    """Long-term trades. Return (ok, reason, limit_price)."""
    sym, side, qty = trade["symbol"], trade["side"], trade["qty"]
    price = (snap["market"].get(sym) or {}).get("latest_price")
    held = next((p["qty"] for p in snap["positions"] if p["symbol"] == sym), 0)
    slip = settings["limit_slippage_pct"] / 100

    if not market_open:
        return False, "the market is closed", None
    if side not in ("buy", "sell"):
        return False, f"unknown side '{side}'", None
    if qty <= 0:
        return False, "quantity must be at least 1 share", None
    if not price:
        return False, f"no current price for {sym}", None
    if orders_placed_today >= settings["max_orders_per_day"]:
        return False, f"daily limit of {settings['max_orders_per_day']} orders reached", None

    if side == "sell":
        if qty > held:
            return False, f"you hold {held:g} shares of {sym} (no short selling)", None
        return True, "", round(price * (1 - slip), 2)

    if sym not in settings["watchlist"]:
        return False, f"{sym} isn't on your watchlist", None
    limit_price = round(price * (1 + slip), 2)
    why = _buy_limits(sym, qty * limit_price, price, held, snap["account"], settings)
    return (False, why, None) if why else (True, "", limit_price)


def check_day_trade(trade, snap, settings):
    """Day trades. Return (ok, reason, order) where order holds the quantity and prices to send."""
    d = settings["day"]
    sym, side, qty = trade["symbol"], trade["side"], trade["qty"]
    acct = snap["account"]
    price = (snap["market"].get(sym) or {}).get("latest_price")
    slip = settings["limit_slippage_pct"] / 100
    day_pos = {p["symbol"]: p["qty"] for p in snap["day_positions"]}

    if side not in ("buy", "sell"):
        return False, f"unknown side '{side}'", None
    if not price:
        return False, f"no current price for {sym}", None

    if side == "sell":
        if sym not in day_pos:
            return False, f"there is no day position in {sym} (long-term holdings aren't day traded)", None
        return True, "", {"qty": day_pos[sym], "limit_price": round(price * (1 - slip), 2)}

    if qty <= 0:
        return False, "quantity must be at least 1 share", None
    if sym not in d["watchlist"]:
        return False, f"{sym} isn't on your day-trading watchlist", None
    if snap["entries_today"] >= d["max_entries_per_day"]:
        return False, f"daily limit of {d['max_entries_per_day']} day-trade entries reached", None
    if snap["minutes_until_close"] <= d["no_new_entries_min_before_close"]:
        return False, "too close to the market close for a new day trade", None
    stop, take = trade.get("stop_loss_pct", 0), trade.get("take_profit_pct", 0)
    if not 0.1 <= stop <= d["max_stop_loss_pct"]:
        return False, f"stop-loss must be between 0.1% and {d['max_stop_loss_pct']}% (got {stop:g}%)", None
    if not 0.1 <= take <= d["max_take_profit_pct"]:
        return False, f"take-profit must be between 0.1% and {d['max_take_profit_pct']}% (got {take:g}%)", None

    limit_price = round(price * (1 + slip), 2)
    stop_price = min(round(limit_price * (1 - stop / 100), 2), round(limit_price - 0.01, 2))
    take_profit = max(round(limit_price * (1 + take / 100), 2), round(limit_price + 0.01, 2))
    risk = qty * (limit_price - stop_price)
    if risk > d["max_risk_per_trade_usd"]:
        return False, (f"a stop-out would lose about {money(risk)}, over your "
                       f"{money(d['max_risk_per_trade_usd'])} per-trade risk limit"), None
    held = snap["all_holdings"].get(sym, 0)
    why = _buy_limits(sym, qty * limit_price, price, held, acct, settings)
    if why:
        return False, why, None
    return True, "", {"qty": qty, "limit_price": limit_price, "stop_price": stop_price,
                      "take_profit": take_profit, "risk": risk}


def apply_to_snapshot(snap, trade, limit_price):
    """Assume the order fills at its limit so later trades in the same run are checked against it."""
    sym, qty = trade["symbol"], trade["qty"]
    sign = 1 if trade["side"] == "buy" else -1
    snap["account"]["cash"] -= sign * qty * limit_price
    pos = next((p for p in snap["positions"] if p["symbol"] == sym), None)
    if pos is None:
        snap["positions"].append({"symbol": sym, "qty": qty})
    else:
        pos["qty"] += sign * qty


# ---------- day-trading state ----------

def day_state_file(mode):
    return DATA / f"day-{dt.datetime.now(ET):%Y-%m-%d}-{mode}.json"


def load_day_state(broker):
    """Today's state: what was held before day trading started (baseline) and the orders it placed."""
    path = day_state_file(broker.mode)
    if path.exists():
        return json.loads(path.read_text())
    state = {"baseline": {p["symbol"]: float(p["qty"]) for p in broker.stock_positions()}, "entries": [],
             "flattened": False}
    save_day_state(broker.mode, state)
    return state


def save_day_state(mode, state):
    save_private(day_state_file(mode), json.dumps(state, indent=1))


def add_to_baseline(mode, symbol, qty):
    """Long-term trades made on a day-trading day must not be undone by the end-of-day flatten."""
    path = day_state_file(mode)
    if path.exists():
        state = json.loads(path.read_text())
        state["baseline"][symbol] = state["baseline"].get(symbol, 0) + qty
        save_day_state(mode, state)


OPEN_STATUSES = ("new", "accepted", "pending_new", "held", "partially_filled", "pending_cancel")


def cancel_and_wait(broker, order_ids, timeout=10):
    """Cancel orders (ignoring ones already done) and wait until none are still open."""
    for oid in order_ids:
        try:
            broker.cancel_order(oid)
        except RuntimeError:
            pass  # already filled, cancelled or expired
    deadline = time.time() + timeout
    pending = list(order_ids)
    while pending and time.time() < deadline:
        still = []
        for oid in pending:
            try:
                if broker.get_order(oid).get("status") in OPEN_STATUSES:
                    still.append(oid)
            except RuntimeError:
                pass
        pending = still
        if pending:
            time.sleep(0.5)


def cancel_protection(broker, symbol):
    """Remove Trader HQ's protective stops (trailing stops, crypto stop-losses) on a symbol before selling it,
    so the shares aren't tied up."""
    key = symbol.replace("/", "")
    try:
        orders = broker.open_orders()
    except (RuntimeError, AttributeError):
        return []
    ids = [o["id"] for o in orders if o.get("symbol", "").replace("/", "") == key and o.get("side") == "sell"
           and (o.get("client_order_id") or "").startswith(("amht-", "amhc-", "amhy-"))]
    if ids:
        cancel_and_wait(broker, ids)
    return ids


def entry_order_ids(state, symbol=None):
    ids = []
    for e in state["entries"]:
        if symbol is None or e["symbol"] == symbol:
            ids += e.get("leg_ids", []) + [e["order_id"]]
    return ids


def flatten(broker, state, progress=print):
    """Cancel day-trade orders and sell every share bought by day trading today."""
    cancel_and_wait(broker, entry_order_ids(state))
    sold = {}
    for sym, qty in day_qty(broker.stock_positions(), state).items():
        try:
            broker.place_market_sell(sym, qty, f"amhx-{dt.datetime.now():%Y%m%d%H%M%S}-{sym}")
            sold[sym] = qty
            progress(f"  Sold {qty} {sym} at market.")
        except RuntimeError as e:
            progress(f"  Couldn't sell {sym}: {e}")
            journal("flatten_failed", mode=broker.mode, symbol=sym, error=str(e))
    state["flattened"] = True
    for f in state.get("fast", []):
        if f.get("status") in ("entering", "open"):
            f.update(status="closed", how="closed before the bell", result=f.get("result") or "closed")
    save_day_state(broker.mode, state)
    journal("flatten", mode=broker.mode, sold=sold)
    if not sold:
        progress("  No day positions to close.")
    return sold


# ---------- options (buying calls and puts only) ----------

def is_option(p):
    return p.get("asset_class") == "us_option"


def parse_occ(symbol):
    """Option symbols look like AAPL261218C00200000: AAPL, 2026-12-18, Call, strike $200.000."""
    core = symbol[-15:]
    if len(symbol) < 16 or core[6] not in "CP" or not (core[:6] + core[7:]).isdigit():
        raise ValueError(f"not an option symbol: {symbol}")
    return {"underlying": symbol[:-15], "expiration": f"20{core[:2]}-{core[2:4]}-{core[4:6]}",
            "type": "call" if core[6] == "C" else "put", "strike": int(core[7:]) / 1000}


def describe_option(symbol):
    try:
        o = parse_occ(symbol)
    except ValueError:
        return symbol
    return f"{o['underlying']} ${o['strike']:g} {o['type'].upper()} expiring {o['expiration']}"


def days_to_expiry(expiration, today=None):
    today = today or dt.datetime.now(ET).date()
    return (dt.date.fromisoformat(expiration) - today).days


def round_to_tick(price, up):
    """Options trade in $0.01 steps under $3 and $0.05 steps from $3 (for the popular, 'penny' symbols)."""
    tick = 0.05 if price >= 3 else 0.01
    steps = price / tick
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return round(steps * tick, 2)


def buy_limit(bid, ask):
    """Offer a bit above the middle of bid/ask (more likely to fill), never above the ask."""
    return min(round_to_tick(bid + (ask - bid) * 0.75, up=True), ask)


def sell_limit(bid, ask):
    return max(round_to_tick(bid + (ask - bid) * 0.25, up=False), bid)


def option_candidates(snapshots, price, settings, today=None):
    """Turn Alpaca's option chain into a short list of liquid, affordable contracts near the current price."""
    o = settings["options"]
    rows = []
    for sym, snap in snapshots.items():
        q = snap.get("latestQuote") or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        if bid <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        if (ask - bid) / mid * 100 > o["max_spread_pct"]:
            continue  # hard to trade fairly
        try:
            c = parse_occ(sym)
        except ValueError:
            continue
        dte = days_to_expiry(c["expiration"], today)
        if not o["min_days_to_expiry"] <= dte <= o["max_days_to_expiry"]:
            continue
        cost = buy_limit(bid, ask) * 100
        if cost > o["max_premium_per_trade_usd"]:
            continue
        greeks = snap.get("greeks") or {}
        rows.append({"contract": sym, "type": c["type"], "strike": c["strike"], "expiration": c["expiration"],
                     "days_to_expiry": dte, "bid": bid, "ask": ask, "cost_per_contract": round(cost, 2),
                     "delta": round(greeks["delta"], 2) if greeks.get("delta") is not None else None,
                     "implied_volatility": round(snap["impliedVolatility"], 3) if snap.get("impliedVolatility") else None})
    rows.sort(key=lambda r: abs(r["strike"] - price))
    keep, count = [], {}
    for r in rows:  # the 3 strikes nearest the price, per expiration and type
        k = (r["expiration"], r["type"])
        if count.get(k, 0) < 3:
            keep.append(r)
            count[k] = count.get(k, 0) + 1
    expirations = sorted({r["expiration"] for r in keep})
    picked = set(expirations[::max(1, len(expirations) // 3)][:3])  # up to 3 spread-out expiration dates
    return sorted((r for r in keep if r["expiration"] in picked),
                  key=lambda r: (r["expiration"], r["type"], r["strike"]))


def held_option_summary(p, quote, today=None):
    c = parse_occ(p["symbol"])
    q = (quote or {}).get("latestQuote") or {}
    return {"contract": p["symbol"], "type": c["type"], "strike": c["strike"], "expiration": c["expiration"],
            "days_to_expiry": days_to_expiry(c["expiration"], today), "qty": int(float(p["qty"])),
            "side": p.get("side", "long"), "paid_total": float(p["cost_basis"]),
            "market_value": float(p["market_value"]),
            "unrealized_pl_pct": round(float(p["unrealized_plpc"]) * 100, 1),
            "bid": float(q.get("bp") or 0), "ask": float(q.get("ap") or 0)}


def options_snapshot(broker, settings):
    acct = broker.account()
    held = broker.option_positions()
    underlyings = settings["options"]["watchlist"]
    prices = broker.latest_prices(underlyings)
    daily = broker.daily_bars(underlyings)
    quotes = broker.option_quotes([p["symbol"] for p in held]) if held else {}
    chains = {u: option_candidates(broker.option_snapshots(u, prices[u], settings), prices[u], settings)
              for u in underlyings if prices.get(u)}
    held_rows = [held_option_summary(p, quotes.get(p["symbol"])) for p in held]
    return {
        "as_of": dt.datetime.now(ET).isoformat(timespec="minutes"),
        "account": {**account_summary(acct),
                    "options_buying_power": float(acct.get("options_buying_power") or acct["cash"])},
        "options_held": held_rows,
        "options_paid_total": sum(h["paid_total"] for h in held_rows),
        "underlyings": {u: {"latest_price": prices.get(u), **(summarize_bars(daily.get(u, [])) or {})}
                        for u in underlyings},
        "contracts_you_can_buy": chains,
    }


OPTIONS_SYSTEM = """You are the options planner for a personal brokerage account whose owner is a beginner.
The account ONLY buys calls and puts and later sells those same contracts. It never sells options it
doesn't own, so the most any trade can lose is what was paid for it.

You receive the account, options already held, price statistics for a few stocks/ETFs, a short list
of contracts that may be bought, and the user's strategy and limits. Propose at most 3 actions, or none:
- side "buy": buy a call (profits if the stock rises enough before expiry) or a put (profits if it falls).
  symbol must be a "contract" from contracts_you_can_buy, exactly as written.
- side "sell": sell a contract that is held (from options_held), to take a profit, cut a loss, or avoid
  holding into the final days before expiry.
Rules:
- Only act on a clear reason in the data. Don't invent news, earnings dates, or facts not in the data.
- Respect the limits; orders that break them will be rejected. Whole contracts; 1 contract = 100 shares.
- reason: 1-2 plain sentences a beginner can follow, e.g. "This call gains if AAPL is above $X by DATE."
- risk: the main way this loses money. For buys, say the whole amount paid can be lost.
- outlook: 1-2 sentences on the account and the market data, honest about uncertainty."""


def build_options_prompt(snap, settings):
    o = settings["options"]
    limits = {k: o[k] for k in ("max_premium_per_trade_usd", "max_total_options_pct", "max_contracts_per_trade",
                                "max_trades_per_day", "min_days_to_expiry", "close_days_before_expiry")}
    return (f"<strategy>\n{o['strategy']}\n</strategy>\n\n"
            f"<limits>\n{json.dumps(limits)}\n</limits>\n\n"
            f"<snapshot>\n{json.dumps(snap, indent=1)}\n</snapshot>\n\n"
            "Propose options actions for today (or none).")


def check_option_trade(trade, snap, settings, buys_today, market_open):
    """Return (ok, reason, order). Buying to open listed contracts and selling held ones are the only actions."""
    o = settings["options"]
    sym, side, qty = trade["symbol"], trade["side"], trade["qty"]
    acct = snap["account"]

    if not market_open:
        return False, "the market is closed", None
    if side not in ("buy", "sell"):
        return False, f"unknown side '{side}'", None
    if qty <= 0:
        return False, "quantity must be at least 1 contract", None

    if side == "sell":
        held = next((h for h in snap["options_held"] if h["contract"] == sym and h["side"] == "long"), None)
        if not held:
            return False, "you don't own this contract (selling options you don't own is never allowed)", None
        if qty > held["qty"]:
            return False, f"you own only {held['qty']} of this contract", None
        if held["bid"] <= 0:
            return False, "nobody is bidding for this contract right now; try again later", None
        return True, "", {"qty": qty, "limit_price": sell_limit(held["bid"], max(held["ask"], held["bid"]))}

    cand = next((c for chain in snap["contracts_you_can_buy"].values() for c in chain if c["contract"] == sym), None)
    if not cand:
        return False, "that contract isn't on the list of allowed calls/puts (within your expiry and price limits)", None
    if buys_today >= o["max_trades_per_day"]:
        return False, f"daily limit of {o['max_trades_per_day']} options buys reached", None
    if qty > o["max_contracts_per_trade"]:
        return False, f"{qty} contracts is over your limit of {o['max_contracts_per_trade']} per trade", None
    if cand["days_to_expiry"] < o["min_days_to_expiry"]:
        return False, f"expires in {cand['days_to_expiry']} days (minimum {o['min_days_to_expiry']})", None
    limit_price = buy_limit(cand["bid"], cand["ask"])
    cost = limit_price * 100 * qty
    if cost > o["max_premium_per_trade_usd"]:
        return False, f"costs up to {money(cost)}, over your {money(o['max_premium_per_trade_usd'])} per-trade limit", None
    if cost > acct["options_buying_power"]:
        return False, f"costs up to {money(cost)} but only {money(acct['options_buying_power'])} is available", None
    cap = acct["equity"] * o["max_total_options_pct"] / 100
    if snap["options_paid_total"] + cost > cap:
        return False, (f"all your options together would cost {money(snap['options_paid_total'] + cost)}, over "
                       f"{o['max_total_options_pct']}% of the account ({money(cap)})"), None
    day_change = acct["equity"] / acct["equity_at_last_close"] - 1 if acct["equity_at_last_close"] else 0
    if day_change <= -settings["daily_loss_stop_pct"] / 100:
        return False, f"account is down {day_change * -100:.1f}% today; new buys are paused", None
    return True, "", {"qty": qty, "limit_price": limit_price, "cost": cost}


def expiring_sells(snap, settings):
    """Held options close to expiry: always offered for sale, so nothing is left to be exercised."""
    days = settings["options"]["close_days_before_expiry"]
    return [{"symbol": h["contract"], "side": "sell", "qty": h["qty"],
             "reason": f"Expires in {h['days_to_expiry']} day(s). Selling now keeps whatever value is left and "
                       "avoids it being exercised into 100 shares per contract.",
             "risk": "If the stock moves your way in the last days, you miss that gain."}
            for h in snap["options_held"] if h["side"] == "long" and h["days_to_expiry"] <= days]


# ---------- trading rounds (shared by the terminal and the desktop app) ----------

def money(x):
    x = float(x)
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def journal(event, **data):
    DATA.mkdir(mode=0o700, exist_ok=True)
    with JOURNAL_FILE.open("a") as f:
        f.write(json.dumps({"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                            "event": event, **data}) + "\n")


def minutes_until_close(clock):
    now = dt.datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00"))
    close = dt.datetime.fromisoformat(clock["next_close"].replace("Z", "+00:00"))
    return int((close - now).total_seconds() // 60)


class Notice(Exception):
    """Nothing to do, with a plain-English reason (not an error)."""


CLOSED_NOTE = ("The market is closed, so these are shown for reference only. Run again during market hours "
               "(9:30am-4pm ET, weekdays) to place orders.")


class Plan:
    """One round of Claude's suggestions. Each trade is checked against the hard limits right before it is
    placed, taking into account what this round has already placed."""
    kind = ""
    label = ""
    empty_message = "No trades proposed."

    size_cap = None  # set when auto-approve applies: buys are sized to fit under the slider amount

    def __init__(self, broker, settings):
        self.b, self.settings, self.mode = broker, settings, broker.mode
        self.outlook, self.trades, self.note = "", [], ""

    def fit(self, t):
        """The trade resized to fit size_cap, if this plan sizes by shares or dollars. Default: unchanged."""
        return t

    def headline(self, t, o):
        return f"{t['side'].upper()} {t['qty']} {t['symbol']}"

    def check(self, t):
        raise NotImplementedError

    def question(self, t, o):
        raise NotImplementedError

    def place(self, t, o, i):
        raise NotImplementedError


class LongTermPlan(Plan):
    kind, label, empty_message = "run", "Long-term", "No trades proposed today."

    def __init__(self, broker, settings, progress=print):
        super().__init__(broker, settings)
        self.market_open = bool(broker.clock()["is_open"])
        progress("Getting prices and your account...")
        self.snap = snapshot(broker, settings)
        self.placed_today = sum(1 for o in broker.orders_today() if o.get("client_order_id", "").startswith("amh-"))
        progress("Asking Claude for a plan (this can take a minute)...")
        plan = ask_claude(build_prompt(self.snap, settings), kind="long_term")
        journal("plan", mode=self.mode, outlook=plan["outlook"], trades=plan["trades"])
        self.outlook, self.trades = plan["outlook"], plan["trades"]
        self.note = "" if self.market_open else CLOSED_NOTE

    def fit(self, t):
        price = (self.snap["market"].get(t.get("symbol")) or {}).get("latest_price")
        return shrink_qty(t, price, self.size_cap, self.settings["limit_slippage_pct"])

    def check(self, t):
        ok, why, limit_price = check_trade(t, self.snap, self.settings, self.placed_today, self.market_open)
        return ok, why, ({"qty": t["qty"], "limit_price": limit_price, "cost": t["qty"] * limit_price} if ok else None)

    def question(self, t, o):
        return f"Place a limit order at {money(o['limit_price'])} (up to {money(o['cost'])})?"

    def place(self, t, o, i):
        if t["side"] == "sell":
            cancel_protection(self.b, t["symbol"])
        order = self.b.place_limit_order(t["symbol"], t["side"], t["qty"], o["limit_price"],
                                         f"amh-{dt.datetime.now():%Y%m%d%H%M%S}-{i}")
        self.placed_today += 1
        apply_to_snapshot(self.snap, t, o["limit_price"])
        add_to_baseline(self.mode, t["symbol"], t["qty"] if t["side"] == "buy" else -t["qty"])
        return order


class DayPlan(Plan):
    kind, label, empty_message = "day", "Day trade", "No action."

    def __init__(self, broker, settings, state, minutes_left, progress=print):
        super().__init__(broker, settings)
        self.state = state
        progress(f"[{dt.datetime.now(ET):%H:%M} ET] Checking ({minutes_left} min until close)...")
        self.snap = day_snapshot(broker, settings, state, minutes_left)
        plan = ask_claude(build_day_prompt(self.snap, settings), DAY_SYSTEM, DAY_SCHEMA, effort="medium", kind="day")
        journal("day_plan", mode=self.mode, outlook=plan["outlook"], trades=plan["trades"])
        self.outlook, self.trades = plan["outlook"], plan["trades"]

    def headline(self, t, o):
        if t["side"] == "buy":
            extra = f" (stop -{t.get('stop_loss_pct', 0):g}%, target +{t.get('take_profit_pct', 0):g}%)"
        else:
            extra = " (close the day position)"
        return f"{t['side'].upper()} {o['qty'] if o else t['qty']} {t['symbol']}{extra}"

    def fit(self, t):
        price = (self.snap["market"].get(t.get("symbol")) or {}).get("latest_price")
        return shrink_qty(t, price, self.size_cap, self.settings["limit_slippage_pct"])

    def check(self, t):
        return check_day_trade(t, self.snap, self.settings)

    def question(self, t, o):
        if t["side"] == "buy":
            return (f"Buy {o['qty']} {t['symbol']} up to {money(o['limit_price'])}, stop {money(o['stop_price'])}, "
                    f"target {money(o['take_profit'])} (max loss about {money(o['risk'])})?")
        return f"Sell all {o['qty']} {t['symbol']} day shares (at least {money(o['limit_price'])})?"

    def place(self, t, o, i):
        b, state, snap = self.b, self.state, self.snap
        cid = f"amhd-{dt.datetime.now():%Y%m%d%H%M%S}-{i}"
        if t["side"] == "buy":
            order = b.place_bracket_buy(t["symbol"], o["qty"], o["limit_price"], o["take_profit"],
                                        o["stop_price"], cid)
            legs = order.get("legs") or b.get_order(order["id"]).get("legs") or []
            state["entries"].append({"symbol": t["symbol"], "qty": o["qty"], "order_id": order["id"],
                                     "leg_ids": [leg["id"] for leg in legs]})
            save_day_state(self.mode, state)
            snap["entries_today"] += 1
            snap["account"]["cash"] -= o["qty"] * o["limit_price"]
            snap["all_holdings"][t["symbol"]] = snap["all_holdings"].get(t["symbol"], 0) + o["qty"]
        else:
            cancel_and_wait(b, entry_order_ids(state, t["symbol"]))  # free shares held by stop/target
            order = b.place_limit_order(t["symbol"], "sell", o["qty"], o["limit_price"], cid)
            snap["day_positions"] = [p for p in snap["day_positions"] if p["symbol"] != t["symbol"]]
        return order


class OptionsPlan(Plan):
    kind, label, empty_message = "options", "Options", "No options trades proposed today."

    def __init__(self, broker, settings, progress=print):
        super().__init__(broker, settings)
        acct = broker.account()
        level = acct.get("options_trading_level", acct.get("options_approved_level"))
        if level is not None and int(level) < 2:
            raise Notice("This account isn't approved to buy calls and puts yet (options level 2). "
                         "In the Alpaca dashboard, open your account settings and apply for options trading.")
        self.market_open = bool(broker.clock()["is_open"])
        progress("Getting stock prices and option quotes...")
        self.snap = options_snapshot(broker, settings)
        if not any(self.snap["contracts_you_can_buy"].values()) and not self.snap["options_held"]:
            raise Notice("No contracts fit your limits right now (price, expiry, bid/ask gap). Nothing to do.")
        self.buys_today = sum(1 for o in broker.orders_today()
                              if o.get("client_order_id", "").startswith("amho-") and o.get("side") == "buy")
        forced = expiring_sells(self.snap, settings)
        progress("Asking Claude for an options plan (this can take a minute)...")
        plan = ask_claude(build_options_prompt(self.snap, settings), OPTIONS_SYSTEM, PLAN_SCHEMA, kind="options")
        journal("options_plan", mode=self.mode, outlook=plan["outlook"], trades=plan["trades"])
        forced_syms = {t["symbol"] for t in forced}
        self.outlook = plan["outlook"]
        self.trades = forced + [t for t in plan["trades"] if not (t["side"] == "sell" and t["symbol"] in forced_syms)]
        self.note = "" if self.market_open else CLOSED_NOTE

    def headline(self, t, o):
        return f"{'BUY' if t['side'] == 'buy' else 'SELL'} {t['qty']} x {describe_option(t['symbol'])}"

    def check(self, t):
        return check_option_trade(t, self.snap, self.settings, self.buys_today, self.market_open)

    def question(self, t, o):
        if t["side"] == "buy":
            return f"Pay up to {money(o['limit_price'])} per share = {money(o['cost'])} in total (the most you can lose)?"
        return f"Sell for at least {money(o['limit_price'])} per share ({money(o['limit_price'] * 100 * o['qty'])} total)?"

    def place(self, t, o, i):
        order = self.b.place_option_order(t["symbol"], t["side"], o["qty"], o["limit_price"],
                                          f"amho-{dt.datetime.now():%Y%m%d%H%M%S}-{i}")
        if t["side"] == "buy":
            self.buys_today += 1
            self.snap["options_paid_total"] += o["cost"]
            self.snap["account"]["options_buying_power"] -= o["cost"]
        else:
            held = next(h for h in self.snap["options_held"] if h["contract"] == t["symbol"])
            held["qty"] -= o["qty"]
        return order


def place_trade(plan, t, o, i):
    """Place one checked trade and record it. Returns the broker's order; raises RuntimeError on failure."""
    try:
        order = plan.place(t, o, i)
    except RuntimeError as e:
        journal("order_failed", mode=plan.mode, trade=t, error=str(e))
        raise
    journal("ordered", mode=plan.mode, trade=t, limit_price=o.get("limit_price"), order=o, order_id=order.get("id"))
    return order


def day_tick(b, settings, state, progress=print, make_plan=None, every_min=None):
    """One step of day trading. Returns (plan or None, seconds until the next step, finished for today).
    make_plan/every_min let fast mode swap in its own planner and pace."""
    d = settings["day"]
    clock = b.clock()
    left = minutes_until_close(clock)
    if not clock["is_open"] or left <= d["flatten_min_before_close"]:
        progress(f"[{dt.datetime.now(ET):%H:%M} ET] Closing all day positions before the bell...")
        flatten(b, state, progress)
        return None, 0, True
    plan = (make_plan or DayPlan)(b, settings, state, left, progress)
    wait = max(min(every_min or d["check_every_min"], left - d["flatten_min_before_close"]), 0.5) * 60
    return plan, wait, False


# ---------- terminal commands ----------

def approve(settings, question, kind=None, value=None):
    if can_auto_approve(settings, kind, value):
        print(f"   {question} Auto-approved (paper).")
        return True
    return input(f"   {question} [y/N] ").strip().lower() in ("y", "yes")


def run_plan_cli(plan):
    print(f"\nClaude's view: {plan.outlook}\n")
    if not plan.trades:
        print(plan.empty_message + "\n")
        return
    if plan.note:
        print(plan.note + "\n")
    for i, t in enumerate(plan.trades, 1):
        ok, why, o = plan.check(t)
        print(f"{i}. {plan.headline(t, o)}")
        print(f"   Why:  {t['reason']}")
        print(f"   Risk: {t['risk']}")
        if not ok:
            print(f"   Blocked: {why}.\n")
            journal("blocked", mode=plan.mode, trade=t, reason=why)
            continue
        if not approve(plan.settings, plan.question(t, o), plan.kind, trade_value(plan.kind, t, o)):
            print("   Skipped.\n")
            journal("skipped", mode=plan.mode, trade=t)
            continue
        try:
            order = place_trade(plan, t, o, i)
        except RuntimeError as e:
            print(f"   Order failed: {e}\n")
            continue
        print(f"   Order sent ({order.get('status', 'submitted')}).\n")


def cli_broker():
    settings = load_settings()
    confirm_mode(settings)
    return Broker(settings["mode"]), settings


def status():
    settings = load_settings()
    b = Broker(settings["mode"])
    acct, positions, clock = b.account(), b.positions(), b.clock()
    equity, last = float(acct["equity"]), float(acct["last_equity"])
    label = "LIVE account (real money)" if b.mode == "live" else "PAPER account (simulated money)"
    print(f"{label} - market {'open' if clock['is_open'] else 'closed'}")
    print(f"  Value  {money(equity)}   today {money(equity - last)} ({(equity / last - 1) * 100 if last else 0:+.2f}%)")
    print(f"  Cash   {money(acct['cash'])}")
    if not positions:
        print("  No positions yet.")
    for p in positions:
        if is_option(p):
            o = parse_occ(p["symbol"])
            name = f"{o['underlying']} {o['strike']:g} {o['type'].upper()} {o['expiration']}"
            unit = "ct"
        else:
            name, unit = p["symbol"], "sh"
        print(f"  {name:<24} {float(p['qty']):>6g} {unit}  {money(p['market_value']):>12}  "
              f"P/L {money(p['unrealized_pl'])} ({float(p['unrealized_plpc']) * 100:+.1f}%)")


def run():
    b, settings = cli_broker()
    run_plan_cli(LongTermPlan(b, settings))


def options_cmd():
    b, settings = cli_broker()
    try:
        plan = OptionsPlan(b, settings)
    except Notice as e:
        print(e)
        return
    run_plan_cli(plan)


def day(once=False):
    b, settings = cli_broker()
    d = settings["day"]
    if not b.clock()["is_open"]:
        print("The market is closed. Day trading runs 9:30am-4pm ET on weekdays.")
        return
    state = load_day_state(b)
    if state["flattened"]:
        print("Day trading already finished for today (positions were closed). See you tomorrow.")
        return
    if not can_auto_approve(settings) and not once:
        print("Stay at this window: every trade waits for your y/n.\n")
    try:
        while True:
            try:
                plan, wait, done = day_tick(b, settings, state)
            except RuntimeError as e:  # one bad check shouldn't end the session
                print(f"Check skipped: {e}\n")
                journal("day_error", mode=b.mode, error=str(e))
                plan, wait, done = None, d["check_every_min"] * 60, False
            if done:
                print("Done for today.")
                return
            if plan:
                run_plan_cli(plan)
            if once:
                return
            print(f"Next check in {wait / 60:.0f} min. Press Ctrl+C to stop.\n")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\nStopped. Open day trades keep their stop-loss and take-profit orders, but those expire at "
              "the close.\nRun `python trader.py flatten` to close them now, or `python trader.py day` "
              "to resume (it closes them before the bell).")


def flatten_cmd():
    b, _ = cli_broker()
    if not day_state_file(b.mode).exists():
        print("No day trading today, so there's nothing to close.")
        return
    print("Closing all day positions...")
    flatten(b, load_day_state(b))


def history_lines():
    if not JOURNAL_FILE.exists():
        return []
    lines = []
    for line in JOURNAL_FILE.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        when = e["time"][:16].replace("T", " ")
        tag = "LIVE " if e.get("mode") == "live" else ""
        t = e.get("trade") or {}
        label = f"{t.get('side', '').upper()} {t.get('qty', '')} {t.get('symbol', '')}".strip()
        ev = e["event"]
        if ev in ("plan", "day_plan", "options_plan"):
            kind = {"plan": "plan", "day_plan": "day check", "options_plan": "options plan"}[ev]
            lines.append(f"{when}  {tag}{kind}: {len(e['trades'])} trade(s). {e['outlook']}")
        elif ev == "ordered":
            price = e.get("limit_price") or (e.get("order") or {}).get("limit_price")
            lines.append(f"{when}    {tag}ORDERED {label}" + (f" @ {money(price)}" if price else ""))
        elif ev == "blocked":
            lines.append(f"{when}    {tag}blocked {label}: {e['reason']}")
        elif ev == "flatten":
            sold = ", ".join(f"{q} {s}" for s, q in e["sold"].items()) or "nothing to sell"
            lines.append(f"{when}  {tag}end of day: {sold}")
        else:
            lines.append(f"{when}    {tag}{ev} {label} {e.get('error', '')}".rstrip())
    return lines


def history():
    lines = history_lines()
    if not lines:
        print("Nothing yet. Run `python trader.py run` or `python trader.py day`.")
    for line in lines:
        print(line)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    def app():
        import desktop
        desktop.serve(open_win="--no-open" not in args)

    def install_app():
        import desktop
        desktop.install_app()

    def update():
        import updater
        updater.update(quiet="--quiet" in args)

    actions = {
        "setup": lambda: setup(args[1] if len(args) > 1 else "paper"),
        "status": status, "run": run, "history": history, "flatten": flatten_cmd, "options": options_cmd,
        "day": lambda: day(once=len(args) > 1 and args[1] == "once"),
        "app": app, "install-app": install_app, "update": update,
    }
    if cmd not in actions:
        print(__doc__)
        return
    try:
        actions[cmd]()
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("\nStopped.")


if __name__ == "__main__":
    main()
