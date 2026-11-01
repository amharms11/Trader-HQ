#!/usr/bin/env python3
"""
AMH Paper Trader - Claude suggests stock trades, you approve each one, and they are
placed in an Alpaca *paper* (fake money) account.

Commands:
  python paper_trader.py setup      # one-time: save your Alpaca paper keys and default settings
  python paper_trader.py status     # account value, cash, positions, today's change
  python paper_trader.py run        # Claude proposes trades; you approve or skip each one
  python paper_trader.py history    # every proposal, decision and order so far

Safety by design:
- Paper only. The broker address is hard-coded to Alpaca's paper endpoint and live keys are refused.
- Claude never places orders. It returns proposals; this script checks them against hard limits
  (settings.json) and asks you before each order.
- Claude only sees prices and your account numbers, never web pages or news, so nothing it
  reads can steer it into placing trades.
Files live in ~/.amh-trader/ (keys in config.env, limits in settings.json, log in journal.jsonl).
"""
import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path.home() / ".amh-trader"
CONFIG_FILE = DATA / "config.env"
SETTINGS_FILE = DATA / "settings.json"
JOURNAL_FILE = DATA / "journal.jsonl"
SOFI_CONFIG = Path.home() / ".sofi-tracker" / "config.env"  # reuse an Anthropic key saved for AMH Finance

PAPER_URL = "https://paper-api.alpaca.markets"  # never the live endpoint
DATA_URL = "https://data.alpaca.markets"
CLAUDE_MODEL = "claude-opus-5-5"

DEFAULT_SETTINGS = {
    "watchlist": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"],
    "strategy": ("Long-term, low-turnover investing. Prefer broad index funds (SPY, QQQ) as the core. "
                 "Only add individual stocks in small amounts. Do nothing unless there is a clear reason; "
                 "'no trades' is a good answer most days."),
    "max_order_usd": 500,          # largest single order
    "max_position_pct": 20,        # no symbol may grow above this % of account value
    "max_orders_per_day": 5,       # orders placed by this tool per day
    "daily_loss_stop_pct": 3,      # no new buys once the account is down this much today
    "limit_slippage_pct": 0.5,     # limit price = last price +/- this %, so a fast move can't fill badly
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
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        s.update(json.loads(SETTINGS_FILE.read_text()))
    s["watchlist"] = [str(x).upper().strip() for x in s["watchlist"] if str(x).strip()]
    return s


def save_private(path, text):
    DATA.mkdir(mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)


def setup():
    print("AMH Paper Trader setup\n")
    print("1. Make a free account at https://alpaca.markets (no deposit needed for paper trading).")
    print("2. In the dashboard, switch to your Paper account and click 'Generate API keys'.")
    print("   Paper keys start with PK.\n")
    key = input("Paper API Key ID: ").strip()
    secret = input("Paper API Secret: ").strip()
    if not key.startswith("PK"):
        sys.exit("That doesn't look like a paper key (they start with PK). Nothing saved.")
    lines = [f"ALPACA_API_KEY_ID={key}", f"ALPACA_API_SECRET_KEY={secret}"]
    if CONFIG_FILE.exists():  # keep anything else already saved, e.g. ANTHROPIC_API_KEY
        lines += [l for l in CONFIG_FILE.read_text().splitlines() if not l.startswith("ALPACA_")]
    save_private(CONFIG_FILE, "\n".join(lines) + "\n")
    if not SETTINGS_FILE.exists():
        save_private(SETTINGS_FILE, json.dumps(DEFAULT_SETTINGS, indent=2) + "\n")
    os.environ["ALPACA_API_KEY_ID"], os.environ["ALPACA_API_SECRET_KEY"] = key, secret
    acct = Broker().account()
    print(f"\nConnected. Paper account value: {money(acct['equity'])}")
    print(f"Edit your watchlist, strategy and limits in {SETTINGS_FILE}")
    if not claude_backend():
        print("\nClaude isn't set up yet: sign in to Claude Code (`claude`), or add "
              f"ANTHROPIC_API_KEY=... to {CONFIG_FILE}")


# ---------- Alpaca (paper) ----------

class Broker:
    def __init__(self):
        load_config()
        self.key = os.environ.get("ALPACA_API_KEY_ID", "")
        self.secret = os.environ.get("ALPACA_API_SECRET_KEY", "")
        if not self.key:
            sys.exit("Run `python paper_trader.py setup` first.")
        if not self.key.startswith("PK"):
            sys.exit("Only Alpaca paper keys (starting with PK) are allowed. Live trading is disabled.")

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
            if e.code in (401, 403):
                raise RuntimeError("Alpaca rejected your keys. Run setup again with your paper keys.") from None
            raise RuntimeError(f"Alpaca error {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Couldn't reach Alpaca ({e.reason}). Check your internet connection.") from None

    def account(self):
        return self._req(PAPER_URL, "/v2/account")

    def positions(self):
        return self._req(PAPER_URL, "/v2/positions")

    def clock(self):
        return self._req(PAPER_URL, "/v2/clock")

    def orders_today(self):
        start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return self._req(PAPER_URL, "/v2/orders", {"status": "all", "after": start.isoformat(), "limit": 500})

    def latest_prices(self, symbols):
        res = self._req(DATA_URL, "/v2/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": "iex"})
        return {s: float(t["p"]) for s, t in (res.get("trades") or {}).items()}

    def daily_bars(self, symbols, days=60):
        start = (dt.date.today() - dt.timedelta(days=days * 1.6)).isoformat()
        out, token = {}, None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                      "feed": "iex", "adjustment": "all", "limit": 10000}
            if token:
                params["page_token"] = token
            res = self._req(DATA_URL, "/v2/stocks/bars", params)
            for s, bars in (res.get("bars") or {}).items():
                out.setdefault(s, []).extend(bars)
            token = res.get("next_page_token")
            if not token:
                return {s: b[-days:] for s, b in out.items()}

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id):
        return self._req(PAPER_URL, "/v2/orders", method="POST", body={
            "symbol": symbol, "side": side, "qty": str(qty), "type": "limit",
            "limit_price": f"{limit_price:.2f}", "time_in_force": "day",
            "client_order_id": client_order_id})


# ---------- market snapshot sent to Claude ----------

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


def snapshot(broker, settings):
    acct, positions = broker.account(), broker.positions()
    held = {p["symbol"] for p in positions}
    symbols = sorted(set(settings["watchlist"]) | held)
    prices = broker.latest_prices(symbols)
    bars = broker.daily_bars(symbols)
    return {
        "as_of": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "account": {
            "equity": float(acct["equity"]), "cash": float(acct["cash"]),
            "equity_at_last_close": float(acct["last_equity"]),
        },
        "positions": [{
            "symbol": p["symbol"], "qty": float(p["qty"]), "avg_entry_price": float(p["avg_entry_price"]),
            "market_value": float(p["market_value"]), "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_pl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
        } for p in positions],
        "market": {s: {"latest_price": prices.get(s), **(summarize_bars(bars.get(s, [])) or {})}
                   for s in symbols},
    }


# ---------- Claude ----------

SYSTEM = """You are the trade planner for a personal PAPER-trading account (simulated money) used to
learn whether a strategy works before risking real money. You receive the account, current
positions, and recent price statistics for a fixed list of symbols, plus the user's strategy.

Propose at most 3 trades, or none. Rules:
- Follow the user's strategy. Holding steady is usually right; propose trades only with a clear reason
  grounded in the data provided. Don't invent news, earnings, or facts that are not in the data.
- Only symbols listed in the market data. Buy only watchlist symbols. Sell only shares that are held.
- Whole shares only. Respect the limits given; orders that break them will be rejected.
- reason: 1-2 plain sentences a beginner can follow. risk: the main way this trade could lose money.
- outlook: 1-3 sentences on the account overall, honest about uncertainty. You can't predict prices."""

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


def ask_claude(prompt):
    backend = claude_backend()
    if backend == "api":
        return _ask_api(prompt)
    if backend == "cli":
        return _ask_cli(prompt)
    raise RuntimeError("Claude isn't set up: sign in to Claude Code (`claude`), or add "
                       f"ANTHROPIC_API_KEY=... to {CONFIG_FILE}")


def _ask_api(prompt):
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            betas=["server-side-fallback-2026-07-01"],
            # extra_body so older SDK versions still send these fields
            extra_body={"fallbacks": "default",
                        "output_config": {"effort": "high",
                                          "format": {"type": "json_schema", "schema": PLAN_SCHEMA}}},
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


def _ask_cli(prompt):
    cmd = [find_claude_cli(), "-p", "--output-format", "json", "--tools", "", "--system-prompt", SYSTEM,
           "--json-schema", json.dumps(PLAN_SCHEMA), "--no-session-persistence", "--strict-mcp-config"]
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
            trades.append({"symbol": str(t["symbol"]).upper().strip(), "side": str(t["side"]).lower(),
                           "qty": int(t["qty"]), "reason": str(t.get("reason", "")),
                           "risk": str(t.get("risk", ""))})
        except (KeyError, TypeError, ValueError):
            continue  # a malformed trade is dropped, never guessed at
    return {"outlook": str(plan.get("outlook", "")), "trades": trades}


# ---------- hard limits (enforced here, not by Claude) ----------

def check_trade(trade, snap, settings, orders_placed_today, market_open):
    """Return (ok, reason, limit_price). `snap` is updated by the caller as trades are approved."""
    sym, side, qty = trade["symbol"], trade["side"], trade["qty"]
    acct = snap["account"]
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

    limit_price = round(price * (1 + slip), 2)
    cost = qty * limit_price
    if sym not in settings["watchlist"]:
        return False, f"{sym} isn't on your watchlist", None
    if cost > settings["max_order_usd"]:
        return False, f"order is {money(cost)}, over your {money(settings['max_order_usd'])} per-order limit", None
    if cost > acct["cash"]:
        return False, f"order is {money(cost)} but only {money(acct['cash'])} cash is available (no borrowing)", None
    day_change = acct["equity"] / acct["equity_at_last_close"] - 1 if acct["equity_at_last_close"] else 0
    if day_change <= -settings["daily_loss_stop_pct"] / 100:
        return False, f"account is down {day_change * -100:.1f}% today; new buys are paused", None
    position_value = held * price + cost
    if position_value > acct["equity"] * settings["max_position_pct"] / 100:
        return False, (f"{sym} would be {position_value / acct['equity'] * 100:.0f}% of the account, "
                       f"over your {settings['max_position_pct']}% limit"), None
    return True, "", limit_price


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


# ---------- commands ----------

def money(x):
    x = float(x)
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def journal(event, **data):
    DATA.mkdir(mode=0o700, exist_ok=True)
    with JOURNAL_FILE.open("a") as f:
        f.write(json.dumps({"time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                            "event": event, **data}) + "\n")


def status():
    b = Broker()
    acct, positions, clock = b.account(), b.positions(), b.clock()
    equity, last = float(acct["equity"]), float(acct["last_equity"])
    print(f"PAPER account (simulated money) - market {'open' if clock['is_open'] else 'closed'}")
    print(f"  Value  {money(equity)}   today {money(equity - last)} ({(equity / last - 1) * 100 if last else 0:+.2f}%)")
    print(f"  Cash   {money(acct['cash'])}")
    if not positions:
        print("  No positions yet.")
    for p in positions:
        print(f"  {p['symbol']:<6} {float(p['qty']):>8g} sh  {money(p['market_value']):>12}  "
              f"P/L {money(p['unrealized_pl'])} ({float(p['unrealized_plpc']) * 100:+.1f}%)")


def run():
    b, settings = Broker(), load_settings()
    market_open = bool(b.clock()["is_open"])
    print("Getting prices and your paper account...")
    snap = snapshot(b, settings)
    placed_today = sum(1 for o in b.orders_today() if o.get("client_order_id", "").startswith("amh-"))
    print("Asking Claude for a plan (this can take a minute)...\n")
    plan = ask_claude(build_prompt(snap, settings))
    journal("plan", outlook=plan["outlook"], trades=plan["trades"])

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
            journal("blocked", trade=t, reason=why)
            continue
        est = t["qty"] * limit_price
        answer = input(f"   Place a limit order at {money(limit_price)} (up to {money(est)})? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("   Skipped.\n")
            journal("skipped", trade=t)
            continue
        try:
            order = b.place_limit_order(t["symbol"], t["side"], t["qty"], limit_price,
                                        f"amh-{dt.datetime.now():%Y%m%d%H%M%S}-{i}")
        except RuntimeError as e:
            print(f"   Order failed: {e}\n")
            journal("order_failed", trade=t, error=str(e))
            continue
        placed_today += 1
        apply_to_snapshot(snap, t, limit_price)
        print(f"   Order sent ({order.get('status', 'submitted')}).\n")
        journal("ordered", trade=t, limit_price=limit_price, order_id=order.get("id"))


def history():
    if not JOURNAL_FILE.exists():
        print("Nothing yet. Run `python paper_trader.py run`.")
        return
    for line in JOURNAL_FILE.read_text().splitlines():
        e = json.loads(line)
        when = e["time"][:16].replace("T", " ")
        t = e.get("trade") or {}
        label = f"{t.get('side', '').upper()} {t.get('qty', '')} {t.get('symbol', '')}".strip()
        if e["event"] == "plan":
            print(f"{when}  plan: {len(e['trades'])} trade(s). {e['outlook']}")
        elif e["event"] == "ordered":
            print(f"{when}    ORDERED {label} @ {money(e['limit_price'])}")
        elif e["event"] == "blocked":
            print(f"{when}    blocked {label}: {e['reason']}")
        else:
            print(f"{when}    {e['event']} {label} {e.get('error', '')}".rstrip())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    actions = {"setup": setup, "status": status, "run": run, "history": history}
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
