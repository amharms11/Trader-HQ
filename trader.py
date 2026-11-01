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
  python trader.py flatten          # sell everything day trading bought today, right now
  python trader.py history          # every plan, approval, skip, block and order

Safety by design:
- Paper unless you set "mode": "live" in settings.json AND type LIVE each time you start.
  Paper keys (PK...) only ever go to the paper endpoint; live keys (AK...) only to the live one.
- Claude never places orders. It returns proposals; this script checks them against hard limits
  (settings.json) and asks you before each order. Auto-approve exists for paper only.
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
PDT_EQUITY = 25_000  # US pattern-day-trader rule: below this, max 3 day trades per 5 business days

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
        day = saved.pop("day", {})
        s.update(saved)
        s["day"].update(day)
    for section in (s, s["day"]):
        section["watchlist"] = [str(x).upper().strip() for x in section["watchlist"] if str(x).strip()]
    if s["mode"] not in ENDPOINTS:
        raise RuntimeError(f'"mode" in {SETTINGS_FILE} must be "paper" or "live".')
    return s


def save_private(path, text):
    DATA.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def key_names(mode):
    return f"ALPACA_{mode.upper()}_KEY_ID", f"ALPACA_{mode.upper()}_SECRET_KEY"


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
    if not key.startswith(KEY_PREFIX[mode]):
        sys.exit(f"{mode.title()} keys start with {KEY_PREFIX[mode]}. Nothing saved.")
    kid, ksec = key_names(mode)
    keep = []
    if CONFIG_FILE.exists():  # keep everything else, e.g. the other account's keys or ANTHROPIC_API_KEY
        keep = [l for l in CONFIG_FILE.read_text().splitlines() if l.split("=", 1)[0].strip() not in (kid, ksec)]
    save_private(CONFIG_FILE, "\n".join([f"{kid}={key}", f"{ksec}={secret}"] + keep) + "\n")
    if not SETTINGS_FILE.exists():
        save_private(SETTINGS_FILE, json.dumps(DEFAULT_SETTINGS, indent=2) + "\n")
    os.environ[kid], os.environ[ksec] = key, secret
    acct = Broker(mode).account()
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


def can_auto_approve(settings):
    return bool(settings.get("auto_approve")) and settings["mode"] == "paper"


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

    def clock(self):
        return self._api("/v2/clock")

    def orders_today(self):
        start = dt.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return self._api("/v2/orders", params={"status": "all", "after": start.isoformat(), "limit": 500})

    def get_order(self, order_id):
        return self._api(f"/v2/orders/{order_id}", params={"nested": "true"})

    def cancel_order(self, order_id):
        return self._api(f"/v2/orders/{order_id}", method="DELETE")

    def latest_prices(self, symbols):
        res = self._req(DATA_URL, "/v2/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": "iex"})
        return {s: float(t["p"]) for s, t in (res.get("trades") or {}).items()}

    def bars(self, symbols, timeframe, start):
        out, token = {}, None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start,
                      "feed": "iex", "adjustment": "all", "limit": 10000}
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
    vwap = sum(b.get("vw", b["c"]) * b["v"] for b in bars) / vol if vol else bars[-1]["c"]
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
        "day_trades_last_5_days": int(acct.get("daytrade_count") or 0),
    }


def snapshot(broker, settings):
    acct, positions = broker.account(), broker.positions()
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
    acct, positions = broker.account(), broker.positions()
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


def ask_claude(prompt, system=SYSTEM, schema=PLAN_SCHEMA, effort="high"):
    backend = claude_backend()
    if backend == "api":
        return _ask_api(prompt, system, schema, effort)
    if backend == "cli":
        return _ask_cli(prompt, system, schema)
    raise RuntimeError("Claude isn't set up: sign in to Claude Code (`claude`), or add "
                       f"ANTHROPIC_API_KEY=... to {CONFIG_FILE}")


def _ask_api(prompt, system, schema, effort):
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
    return parse_plan(next(b.text for b in resp.content if getattr(b, "type", "") == "text"))


def _ask_cli(prompt, system, schema):
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
    return validate_plan_shape(plan) if isinstance(plan, dict) else parse_plan(res.get("result") or "")


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
    return {"outlook": str(plan.get("outlook", "")), "trades": trades}


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
    if acct["equity"] < PDT_EQUITY and acct["day_trades_last_5_days"] >= 3:
        return False, ("you've made 3 day trades in the last 5 business days and the account is under "
                       "$25,000 (pattern-day-trader rule)"), None
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
    state = {"baseline": {p["symbol"]: float(p["qty"]) for p in broker.positions()}, "entries": [],
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


def entry_order_ids(state, symbol=None):
    ids = []
    for e in state["entries"]:
        if symbol is None or e["symbol"] == symbol:
            ids += e.get("leg_ids", []) + [e["order_id"]]
    return ids


def flatten(broker, state):
    """Cancel day-trade orders and sell every share bought by day trading today."""
    cancel_and_wait(broker, entry_order_ids(state))
    sold = {}
    for sym, qty in day_qty(broker.positions(), state).items():
        try:
            broker.place_market_sell(sym, qty, f"amhx-{dt.datetime.now():%Y%m%d%H%M%S}-{sym}")
            sold[sym] = qty
            print(f"  Sold {qty} {sym} at market.")
        except RuntimeError as e:
            print(f"  Couldn't sell {sym}: {e}")
            journal("flatten_failed", mode=broker.mode, symbol=sym, error=str(e))
    state["flattened"] = True
    save_day_state(broker.mode, state)
    journal("flatten", mode=broker.mode, sold=sold)
    if not sold:
        print("  No day positions to close.")
    return sold


# ---------- commands ----------

def money(x):
    x = float(x)
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def journal(event, **data):
    DATA.mkdir(mode=0o700, exist_ok=True)
    with JOURNAL_FILE.open("a") as f:
        f.write(json.dumps({"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                            "event": event, **data}) + "\n")


def approve(settings, question):
    if can_auto_approve(settings):
        print(f"   {question} Auto-approved (paper).")
        return True
    return input(f"   {question} [y/N] ").strip().lower() in ("y", "yes")


def minutes_until_close(clock):
    now = dt.datetime.fromisoformat(clock["timestamp"].replace("Z", "+00:00"))
    close = dt.datetime.fromisoformat(clock["next_close"].replace("Z", "+00:00"))
    return int((close - now).total_seconds() // 60)


def status():
    settings = load_settings()
    b = Broker(settings["mode"])
    acct, positions, clock = b.account(), b.positions(), b.clock()
    equity, last = float(acct["equity"]), float(acct["last_equity"])
    label = "LIVE account (real money)" if b.mode == "live" else "PAPER account (simulated money)"
    print(f"{label} - market {'open' if clock['is_open'] else 'closed'}")
    print(f"  Value  {money(equity)}   today {money(equity - last)} ({(equity / last - 1) * 100 if last else 0:+.2f}%)")
    print(f"  Cash   {money(acct['cash'])}")
    pdt = " (account under $25,000: the limit is 3)" if equity < PDT_EQUITY else ""
    print(f"  Day trades in the last 5 business days: {int(acct.get('daytrade_count') or 0)}{pdt}")
    if not positions:
        print("  No positions yet.")
    for p in positions:
        print(f"  {p['symbol']:<6} {float(p['qty']):>8g} sh  {money(p['market_value']):>12}  "
              f"P/L {money(p['unrealized_pl'])} ({float(p['unrealized_plpc']) * 100:+.1f}%)")


def run():
    settings = load_settings()
    confirm_mode(settings)
    b = Broker(settings["mode"])
    market_open = bool(b.clock()["is_open"])
    print("Getting prices and your account...")
    snap = snapshot(b, settings)
    placed_today = sum(1 for o in b.orders_today() if o.get("client_order_id", "").startswith("amh-"))
    print("Asking Claude for a plan (this can take a minute)...\n")
    plan = ask_claude(build_prompt(snap, settings))
    journal("plan", mode=b.mode, outlook=plan["outlook"], trades=plan["trades"])

    print(f"Claude's view: {plan['outlook']}\n")
    if not plan["trades"]:
        print("No trades proposed today.")
        return
    if not market_open:
        print("The market is closed, so these are shown for reference only. Run again during market hours "
              "(9:30am-4pm ET, weekdays) to place orders.\n")

    for i, t in enumerate(plan["trades"], 1):
        print(f"{i}. {t['side'].upper()} {t['qty']} {t['symbol']}")
        print(f"   Why:  {t['reason']}")
        print(f"   Risk: {t['risk']}")
        ok, why, limit_price = check_trade(t, snap, settings, placed_today, market_open)
        if not ok:
            print(f"   Blocked: {why}.\n")
            journal("blocked", mode=b.mode, trade=t, reason=why)
            continue
        est = t["qty"] * limit_price
        if not approve(settings, f"Place a limit order at {money(limit_price)} (up to {money(est)})?"):
            print("   Skipped.\n")
            journal("skipped", mode=b.mode, trade=t)
            continue
        try:
            order = b.place_limit_order(t["symbol"], t["side"], t["qty"], limit_price,
                                        f"amh-{dt.datetime.now():%Y%m%d%H%M%S}-{i}")
        except RuntimeError as e:
            print(f"   Order failed: {e}\n")
            journal("order_failed", mode=b.mode, trade=t, error=str(e))
            continue
        placed_today += 1
        apply_to_snapshot(snap, t, limit_price)
        add_to_baseline(b.mode, t["symbol"], t["qty"] if t["side"] == "buy" else -t["qty"])
        print(f"   Order sent ({order.get('status', 'submitted')}).\n")
        journal("ordered", mode=b.mode, trade=t, limit_price=limit_price, order_id=order.get("id"))


def day_check(b, settings, state, minutes_left):
    print(f"[{dt.datetime.now(ET):%H:%M} ET] Checking ({minutes_left} min until close)...")
    snap = day_snapshot(b, settings, state, minutes_left)
    plan = ask_claude(build_day_prompt(snap, settings), DAY_SYSTEM, DAY_SCHEMA, effort="medium")
    journal("day_plan", mode=b.mode, outlook=plan["outlook"], trades=plan["trades"])
    print(f"Claude's view: {plan['outlook']}")
    if not plan["trades"]:
        print("No action.\n")
        return
    for i, t in enumerate(plan["trades"], 1):
        ok, why, o = check_day_trade(t, snap, settings)
        if t["side"] == "buy":
            extra = f" (stop -{t.get('stop_loss_pct', 0):g}%, target +{t.get('take_profit_pct', 0):g}%)"
        else:
            extra = " (close the day position)"
        print(f"{i}. {t['side'].upper()} {o['qty'] if o else t['qty']} {t['symbol']}{extra}")
        print(f"   Why:  {t['reason']}")
        print(f"   Risk: {t['risk']}")
        if not ok:
            print(f"   Blocked: {why}.")
            journal("blocked", mode=b.mode, trade=t, reason=why)
            continue
        if t["side"] == "buy":
            q = (f"Buy {o['qty']} {t['symbol']} up to {money(o['limit_price'])}, stop {money(o['stop_price'])}, "
                 f"target {money(o['take_profit'])} (max loss about {money(o['risk'])})?")
        else:
            q = f"Sell all {o['qty']} {t['symbol']} day shares (at least {money(o['limit_price'])})?"
        if not approve(settings, q):
            print("   Skipped.")
            journal("skipped", mode=b.mode, trade=t)
            continue
        cid = f"amhd-{dt.datetime.now():%Y%m%d%H%M%S}-{i}"
        try:
            if t["side"] == "buy":
                order = b.place_bracket_buy(t["symbol"], o["qty"], o["limit_price"], o["take_profit"],
                                            o["stop_price"], cid)
                legs = order.get("legs") or b.get_order(order["id"]).get("legs") or []
                state["entries"].append({"symbol": t["symbol"], "qty": o["qty"], "order_id": order["id"],
                                         "leg_ids": [leg["id"] for leg in legs]})
                save_day_state(b.mode, state)
                snap["entries_today"] += 1
                snap["account"]["cash"] -= o["qty"] * o["limit_price"]
                snap["all_holdings"][t["symbol"]] = snap["all_holdings"].get(t["symbol"], 0) + o["qty"]
            else:
                cancel_and_wait(b, entry_order_ids(state, t["symbol"]))  # free shares held by stop/target
                order = b.place_limit_order(t["symbol"], "sell", o["qty"], o["limit_price"], cid)
        except RuntimeError as e:
            print(f"   Order failed: {e}")
            journal("order_failed", mode=b.mode, trade=t, error=str(e))
            continue
        print(f"   Order sent ({order.get('status', 'submitted')}).")
        journal("ordered", mode=b.mode, trade=t, order=o, order_id=order.get("id"))
    print()


def day(once=False):
    settings = load_settings()
    confirm_mode(settings)
    b = Broker(settings["mode"])
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
            clock = b.clock()
            left = minutes_until_close(clock)
            if not clock["is_open"] or left <= d["flatten_min_before_close"]:
                print(f"[{dt.datetime.now(ET):%H:%M} ET] Closing all day positions before the bell...")
                flatten(b, state)
                print("Done for today.")
                return
            try:
                day_check(b, settings, state, left)
            except RuntimeError as e:  # one bad check shouldn't end the session
                print(f"Check skipped: {e}\n")
                journal("day_error", mode=b.mode, error=str(e))
            if once:
                return
            wait = max(min(d["check_every_min"], left - d["flatten_min_before_close"]), 0.5) * 60
            print(f"Next check in {wait / 60:.0f} min. Press Ctrl+C to stop.\n")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\nStopped. Open day trades keep their stop-loss and take-profit orders, but those expire at "
              "the close.\nRun `python trader.py flatten` to close them now, or `python trader.py day` "
              "to resume (it closes them before the bell).")


def flatten_cmd():
    settings = load_settings()
    confirm_mode(settings)
    b = Broker(settings["mode"])
    if not day_state_file(b.mode).exists():
        print("No day trading today, so there's nothing to close.")
        return
    print("Closing all day positions...")
    flatten(b, load_day_state(b))


def history():
    if not JOURNAL_FILE.exists():
        print("Nothing yet. Run `python trader.py run` or `python trader.py day`.")
        return
    for line in JOURNAL_FILE.read_text().splitlines():
        e = json.loads(line)
        when = e["time"][:16].replace("T", " ")
        tag = "LIVE " if e.get("mode") == "live" else ""
        t = e.get("trade") or {}
        label = f"{t.get('side', '').upper()} {t.get('qty', '')} {t.get('symbol', '')}".strip()
        ev = e["event"]
        if ev in ("plan", "day_plan"):
            kind = "day check" if ev == "day_plan" else "plan"
            print(f"{when}  {tag}{kind}: {len(e['trades'])} trade(s). {e['outlook']}")
        elif ev == "ordered":
            price = e.get("limit_price") or (e.get("order") or {}).get("limit_price")
            print(f"{when}    {tag}ORDERED {label}" + (f" @ {money(price)}" if price else ""))
        elif ev == "blocked":
            print(f"{when}    {tag}blocked {label}: {e['reason']}")
        elif ev == "flatten":
            sold = ", ".join(f"{q} {s}" for s, q in e["sold"].items()) or "nothing to sell"
            print(f"{when}  {tag}end of day: {sold}")
        else:
            print(f"{when}    {tag}{ev} {label} {e.get('error', '')}".rstrip())


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    actions = {
        "setup": lambda: setup(args[1] if len(args) > 1 else "paper"),
        "status": status, "run": run, "history": history, "flatten": flatten_cmd,
        "day": lambda: day(once=len(args) > 1 and args[1] == "once"),
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
