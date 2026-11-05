"""
Fast mode for crypto day trading: catch small swings with limit orders.

Market orders cost about 0.25% each way on Alpaca crypto, so a small swing is eaten by fees. Limit orders that
wait on the order book cost about 0.15% each way (about 0.3% for a round trip), so a 0.6-1% swing can pay:

- Every few minutes Claude looks at 1-minute data and arms dip-buy setups: "buy BTC at $X (a support level or
  VWAP band below the price), sell at $Y, stop at $Z". The app checks each against your limits and sizes it.
- Once approved (by you, or auto-approve on paper), a limit buy waits at the entry price at Alpaca.
- When it fills, a limit sell waits at the target at Alpaca. The app watches the price every couple of seconds
  and sells at market if the stop is hit (Alpaca has no bracket orders for crypto). Unfilled buys are cancelled
  when they expire or if the price falls to the stop first.

The app-watched stop only works while Trader HQ is open, which is why "Keep this Mac awake" matters.
"""
import datetime as dt
import json
import threading
import time
import uuid

import features as F
import market as M
import trader as T

STALE_SEC = 120
MIN_REWARD_RISK = 1.5

SYSTEM = """You are the planner for a fast crypto swing bot on a spot-only account (it can only buy, then sell;
no shorting). Alpaca charges about 0.15% for limit orders that wait on the order book, so the bot enters AND takes
profit with resting limit orders (about 0.3% round trip). Your job: arm dip-buy setups that catch small swings.

You receive the account, open fast trades, and for each coin: levels from the last few hours of 1-minute bars
(VWAP, recent highs and lows, typical 1-minute range, recent closes, spread), plus the user's strategy and limits.

Arm at most 3 setups, or none. Each setup:
- entry_price: a limit buy AT OR BELOW the current price, at a level where buyers have stepped in (recent swing
  low, VWAP or a band below it, a round number). Not more than about 1.5% below the price.
- target_price: where to take profit, typically the recent swing high or VWAP above. At least the minimum target
  % above entry, and at least 1.5x the stop distance.
- stop_price: below the level the entry relies on, so a clean break means the idea was wrong.
- valid_minutes: how long the buy may wait (10-60).
Rules:
- Only in range-bound or up-trending conditions. In a steady selloff below VWAP with lower lows, arm nothing:
  dip-buying a falling market is how this strategy loses.
- In thin hours (late US evening into the Asian morning, weekends) prefer BTC and ETH, smaller size, wider levels.
- Meme coins swing harder: only on clear, well-tested ranges.
- Volume in this data comes only from Alpaca's own crypto venue, a small slice of the market, so it is often
  tiny or zero (especially at night). Don't read low or zero volume as a signal; judge from price structure.
- reason: 1-2 plain sentences a beginner can follow. risk: the main way it could fail.
- outlook: 1-2 sentences on the market right now, honest about uncertainty."""

SCHEMA = {
    "type": "object",
    "properties": {
        "outlook": {"type": "string"},
        "setups": {"type": "array", "items": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}, "entry_price": {"type": "number"}, "stop_price": {"type": "number"},
                           "target_price": {"type": "number"}, "valid_minutes": {"type": "number"},
                           "reason": {"type": "string"}, "risk": {"type": "string"}},
            "required": ["symbol", "entry_price", "stop_price", "target_price", "valid_minutes", "reason", "risk"],
            "additionalProperties": False}},
    },
    "required": ["outlook", "setups"], "additionalProperties": False,
}

ACTIVE = ("buying", "open")


class Store:
    """Fast crypto trades, kept in the database so open trades survive a restart."""
    COLS = ("id", "symbol", "amount", "entry", "stop", "target", "qty", "buy_order_id", "target_order_id", "status",
            "created", "expires", "filled_at", "exit", "pnl", "result", "how", "reason", "mode", "day", "closed_at")

    def __init__(self, store):
        self.s = store
        with store.db.lock:
            store.db.conn.execute("""CREATE TABLE IF NOT EXISTS crypto_fast (id TEXT PRIMARY KEY, symbol TEXT, amount REAL,
                entry REAL, stop REAL, target REAL, qty REAL, buy_order_id TEXT, target_order_id TEXT, status TEXT,
                created REAL, expires REAL, filled_at REAL, exit REAL, pnl REAL, result TEXT, how TEXT, reason TEXT,
                mode TEXT, day TEXT, closed_at REAL)""")
            store.db.conn.commit()

    def rows(self, mode, statuses=None, day=None):
        sql, args = "SELECT " + ",".join(self.COLS) + " FROM crypto_fast WHERE mode=?", [mode]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if day:
            sql += " AND day=?"
            args.append(day)
        return [dict(zip(self.COLS, r)) for r in self.s.q(sql + " ORDER BY created", tuple(args))]

    def add(self, row):
        self.s.x(f"INSERT INTO crypto_fast ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def update(self, rid, **kw):
        self.s.x(f"UPDATE crypto_fast SET {','.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), rid))


# ---------- levels for Claude ----------

def levels(bars):
    """The last few hours of 1-minute bars -> levels for swing setups."""
    if not bars:
        return None
    vol = sum(b.get("v", 0) for b in bars)
    vwap = sum((b.get("vw") or b["c"]) * b.get("v", 0) for b in bars) / vol if vol else bars[-1]["c"]
    last60 = bars[-60:]
    ranges = [b["h"] - b["l"] for b in bars[-30:]]
    five = [bars[i:i + 5] for i in range(0, len(bars), 5)]
    return {"last": bars[-1]["c"], "vwap": round(vwap, 6),
            "high_last_hour": max(b["h"] for b in last60), "low_last_hour": min(b["l"] for b in last60),
            "high_period": max(b["h"] for b in bars), "low_period": min(b["l"] for b in bars),
            "change_period_pct": round((bars[-1]["c"] / bars[0]["o"] - 1) * 100, 2),
            "avg_1min_range_pct": round(sum(ranges) / len(ranges) / bars[-1]["c"] * 100, 3),
            "last_20_closes_1min": [b["c"] for b in bars[-20:]],
            "last_12_closes_5min": [g[-1]["c"] for g in five[-12:]],
            "minutes_of_data": len(bars)}


def live_quotes(market, symbols):
    """{sym: {"price", "time", "bid", "ask", "spread_pct"}} from crypto snapshots."""
    out = {}
    if not symbols:
        return out
    res = market.data("/v1beta3/crypto/us/snapshots", {"symbols": ",".join(symbols)}) or {}
    for sym, s in (res.get("snapshots") or {}).items():
        trade, quote = (s or {}).get("latestTrade") or {}, (s or {}).get("latestQuote") or {}
        if not trade.get("p"):
            continue
        bid, ask = M.num(quote.get("bp"), 0), M.num(quote.get("ap"), 0)
        mid = (bid + ask) / 2
        out[sym] = {"price": float(trade["p"]), "time": trade.get("t"), "bid": bid, "ask": ask,
                    "spread_pct": (ask - bid) / mid * 100 if bid > 0 and ask >= bid else None}
    return out


def age_sec(iso):
    if not iso:
        return 1e9
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()


def minute_bars(market, symbols, hours=4):
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for sym in symbols:
        try:
            res = market.data("/v1beta3/crypto/us/bars", {"symbols": sym, "timeframe": "1Min", "start": start, "limit": 10000}) or {}
            out[sym] = (res.get("bars") or {}).get(sym) or []
        except RuntimeError:
            out[sym] = []
    return out


# ---------- checking a setup ----------

def parse(x):
    try:
        return {"symbol": str(x["symbol"]).upper().strip(), "side": "buy", "entry_price": float(x["entry_price"]),
                "stop_price": float(x["stop_price"]), "target_price": float(x["target_price"]),
                "valid_minutes": float(x.get("valid_minutes") or 30), "reason": str(x.get("reason", "")),
                "risk": str(x.get("risk", "")), "qty": 0}
    except (KeyError, TypeError, ValueError):
        return None


def check(t, settings, acct, quotes, active, entries_today, losses_today, last_loss_at, held_value, to_close_all, now=None):
    """Hard limits for arming a crypto swing setup. Returns (ok, reason, order)."""
    c = settings["crypto_day"]
    now = now or time.time()
    sym, entry, stop, target = t["symbol"], t["entry_price"], t["stop_price"], t["target_price"]
    q = quotes.get(sym) or {}
    price = q.get("price")
    if not price:
        return False, f"no current price for {sym}", None
    if not stop < entry < target:
        return False, "the stop must be below the entry and the target above it", None
    if entry > price * 1.001:
        return False, f"the entry is above the price ({T.money(price)}): fast mode only buys dips with limit orders", None
    if entry < price * 0.985:
        return False, "the entry is more than 1.5% below the price, too far for a quick swing", None
    stop_pct, take_pct = (entry - stop) / entry * 100, (target - entry) / entry * 100
    if take_pct < c["fast_min_target_pct"]:
        return False, (f"target is only {take_pct:.2f}% above the entry; the minimum is {c['fast_min_target_pct']:g}% "
                       "so fees don't eat it"), None
    if target - entry < MIN_REWARD_RISK * (entry - stop):
        return False, f"the target should be at least {MIN_REWARD_RISK:g}x the stop distance", None
    if sum(1 for r in active if r["status"] in ACTIVE) >= c["max_open_fast_trades"]:
        return False, f"already {c['max_open_fast_trades']} fast crypto trade(s) waiting or open", None
    if any(r["symbol"] == sym for r in active if r["status"] in ACTIVE):
        return False, f"there's already a fast {sym} trade", None
    if c["max_losses_per_day"] and losses_today >= c["max_losses_per_day"]:
        return False, f"{losses_today} losing fast trades today (your limit is {c['max_losses_per_day']})", None
    if c["cooldown_after_loss_min"] and now - last_loss_at < c["cooldown_after_loss_min"] * 60:
        return False, f"cooling down for {c['cooldown_after_loss_min']} min after a loss", None
    max_amount, max_stop, _ = F.crypto_day_limits(c, sym)
    amount = min(max_amount, c["max_risk_per_trade_usd"] / (stop_pct / 100))
    rows = [{"symbol": r["symbol"], "status": "open"} for r in active if r["status"] in ACTIVE]
    ok, why, o = F.check_crypto_day({"symbol": sym, "side": "buy", "amount_usd": round(amount, 2), "stop_loss_pct": stop_pct,
                                     "take_profit_pct": take_pct}, settings, acct, quotes, rows, entries_today, to_close_all,
                                    held_value, min_target=c["fast_min_target_pct"])
    if not ok:
        return False, why, None
    minutes = max(5, min(t["valid_minutes"], c["fast_arm_minutes"]))
    return True, "", {"amount": round(amount, 2), "qty": round(amount / entry, 8), "entry": entry, "stop_price": stop,
                      "take_profit": target, "risk": amount * stop_pct / 100, "minutes": minutes, "limit_price": entry}


class Plan(T.Plan):
    kind, label, empty_message = "crypto_day", "Fast crypto setup", "No crypto swing setups armed right now."

    def __init__(self, broker, settings, market, engine, progress=print, now=None):
        super().__init__(broker, settings)
        self.market, self.engine = market, engine
        c = settings["crypto_day"]
        now_et = now or dt.datetime.now(T.ET)
        progress(f"[{now_et:%H:%M} ET] Looking for crypto swing setups...")
        self.acct = T.account_summary(broker.account())
        self.held = {F.CryptoPlan.slash(p["symbol"]): float(p["market_value"]) for p in broker.positions() if p.get("asset_class") == "crypto"}
        self.active = engine.store.rows(broker.mode, ACTIVE)
        syms = sorted(set(c["watchlist"]) | {r["symbol"] for r in self.active})
        self.quotes = live_quotes(market, syms)
        bars = minute_bars(market, syms)
        day = F.today_et().isoformat()
        self.entries_today = engine.entries_today(broker.mode, day)
        self.losses_today = sum(1 for r in engine.store.rows(broker.mode, day=day) if r["result"] == "loss")
        self.to_close_all = None if c.get("around_the_clock") else F.minutes_to(c["close_all_hhmm"], now_et)
        utc = now_et.astimezone(dt.timezone.utc)
        snap = {"account": self.acct, "clock": {"new_york": now_et.strftime("%a %H:%M"), "utc": utc.strftime("%a %H:%M")},
                "fast_trades": [{k: r[k] for k in ("symbol", "status", "entry", "stop", "target")} for r in self.active],
                "market": {s: {"price": (self.quotes.get(s) or {}).get("price"),
                               "spread_pct": round((self.quotes.get(s) or {}).get("spread_pct") or 0, 4),
                               "meme_coin": T.is_meme(s), "last_hours_1min": levels(bars.get(s, []))} for s in syms}}
        limits = {k: c[k] for k in ("max_order_usd", "max_risk_per_trade_usd", "max_stop_loss_pct", "fast_min_target_pct",
                                    "meme_max_order_usd", "meme_max_stop_loss_pct", "fast_arm_minutes")}
        prompt = (f"<strategy>\n{c['strategy']}\n</strategy>\n\n<watchlist>{', '.join(c['watchlist'])}</watchlist>\n\n"
                  f"<limits>\n{json.dumps(limits)}\n</limits>\n\n<snapshot>\n{json.dumps(snap, indent=1, default=str)}\n</snapshot>\n\n"
                  "Arm crypto swing setups for right now (or none). Don't repeat coins that already have a fast trade.")
        out = T.ask_claude(prompt, SYSTEM, SCHEMA, effort="medium", kind="crypto_day", raw=True)
        self.outlook = str(out.get("outlook", ""))
        taken = {r["symbol"] for r in self.active}
        self.trades = [x for x in (parse(s) for s in (out.get("setups") or [])[:3]) if x and x["symbol"] not in taken]
        T.journal("crypto_fast_plan", mode=self.mode, outlook=self.outlook, setups=self.trades)

    def headline(self, t, o):
        amt = T.money(o["amount"]) if o else "?"
        return (f"ARM swing: buy {amt} of {t['symbol']} at {T.money(t['entry_price'])}, sell at {T.money(t['target_price'])} "
                f"(stop {T.money(t['stop_price'])})")

    def check(self, t):
        return check(t, self.settings, self.acct, self.quotes, self.active, self.entries_today, self.losses_today,
                     self.engine.last_loss_at, self.held, self.to_close_all)

    def question(self, t, o):
        return (f"Place a limit buy for {T.money(o['amount'])} of {t['symbol']} at {T.money(o['entry'])} for the next "
                f"{o['minutes']:g} min? If it fills, it sells at {T.money(o['take_profit'])} (max loss about {T.money(o['risk'])}).")

    def place(self, t, o, i):
        r = self.engine.arm(self.b, t, o)
        self.active.append(r)
        self.entries_today += 1
        self.acct["cash"] -= o["amount"]
        return {"id": r["buy_order_id"], "status": "armed"}


# ---------- the engine ----------

class Engine:
    def __init__(self, store, log=print, notify=None):
        self.store = Store(store)
        self.log = log
        self.notify = notify or (lambda *a, **k: None)
        self.lock = threading.RLock()
        self.last_loss_at = 0.0
        self.status = ""

    def entries_today(self, mode, day):
        n = self.store.s.q("SELECT COUNT(*) FROM crypto_day_trades WHERE mode=? AND day=?", (mode, day))[0][0]
        return n + sum(1 for r in self.store.rows(mode, day=day) if r["result"] != "not filled")

    def arm(self, broker, t, o, now=None):
        now = now or time.time()
        rid = uuid.uuid4().hex[:8]
        order = broker._api("/v2/orders", method="POST", body={
            "symbol": t["symbol"], "side": "buy", "type": "limit", "qty": f"{o['qty']:.8g}",
            "limit_price": M.fmt_price(o["entry"], True), "time_in_force": "gtc", "client_order_id": f"amhy-f-{rid}"})
        row = {"id": rid, "symbol": t["symbol"], "amount": o["amount"], "entry": o["entry"], "stop": o["stop_price"],
               "target": o["take_profit"], "qty": o["qty"], "buy_order_id": order["id"], "target_order_id": None,
               "status": "buying", "created": now, "expires": now + o["minutes"] * 60, "filled_at": None, "exit": None,
               "pnl": None, "result": None, "how": None, "reason": t.get("reason", ""), "mode": broker.mode,
               "day": F.today_et().isoformat(), "closed_at": None}
        self.store.add(row)
        T.journal("crypto_fast_armed", mode=broker.mode, symbol=t["symbol"], entry=o["entry"], stop=o["stop_price"], target=o["take_profit"])
        self.log(f"Fast crypto: limit buy {T.money(o['amount'])} of {t['symbol']} at {T.money(o['entry'])} "
                 f"(sell at {T.money(o['take_profit'])}, stop {T.money(o['stop_price'])}) for {o['minutes']:g} min.")
        return row

    def cancel(self, broker, rid, why="cancelled by you"):
        """Cancel a waiting buy, or sell an open trade now."""
        with self.lock:
            row = next((r for r in self.store.rows(broker.mode, ACTIVE) if r["id"] == rid), None)
            if not row:
                raise ValueError("That fast crypto trade isn't active any more.")
            if row["status"] == "buying":
                self._cancel_buy(broker, row, why)
            else:
                self._sell(broker, row, why, None)

    def tick(self, broker, settings, market, now=None):
        """Watch waiting buys and open trades. Called every couple of seconds while any are active."""
        now = now or time.time()
        with self.lock:
            rows = self.store.rows(broker.mode, ACTIVE)
            if not rows:
                return
            try:
                quotes = live_quotes(market, sorted({r["symbol"] for r in rows}))
            except RuntimeError as e:
                self.status = f"Price check failed: {e}"
                quotes = {}
            c = settings["crypto_day"]
            past_close_all = not c.get("around_the_clock") and F.minutes_to(c["close_all_hhmm"]) <= 0
            for r in rows:
                q = quotes.get(r["symbol"])
                fresh = q and age_sec(q["time"]) <= STALE_SEC
                if not fresh:
                    self.status = f"{r['symbol']}: no fresh price; the stop can't be checked right now."
                try:
                    if r["status"] == "buying":
                        self._watch_buy(broker, r, q if fresh else None, now)
                    else:
                        self._watch_open(broker, settings, r, q if fresh else None, now, past_close_all)
                except RuntimeError as e:
                    self.status = f"{r['symbol']}: {e}"

    def _watch_buy(self, broker, r, q, now):
        o = broker.get_order(r["buy_order_id"])
        status, filled = o.get("status"), M.num(o.get("filled_qty"), 0)
        if status == "filled":
            return self._filled(broker, r, o, now)
        if status in ("canceled", "expired", "rejected"):
            if filled > 0:
                return self._filled(broker, r, o, now)
            self.store.update(r["id"], status="closed", result="not filled", how=f"buy {status}", closed_at=now)
            return
        if now >= r["expires"]:
            return self._cancel_buy(broker, r, "the buy didn't fill in time")
        if q and q["price"] <= r["stop"]:
            return self._cancel_buy(broker, r, f"price fell to the stop ({T.money(q['price'])}) before the buy filled")

    def _cancel_buy(self, broker, r, why):
        T.cancel_and_wait(broker, [r["buy_order_id"]])
        o = broker.get_order(r["buy_order_id"])
        if M.num(o.get("filled_qty"), 0) > 0:  # partly filled before the cancel: manage what we got
            return self._filled(broker, r, o, time.time())
        self.store.update(r["id"], status="closed", result="not filled", how=why, closed_at=time.time())
        self.log(f"Fast crypto: {r['symbol']} buy cancelled ({why}).")

    def _filled(self, broker, r, o, now):
        qty, entry = M.num(o.get("filled_qty"), 0), M.num(o.get("filled_avg_price"), r["entry"])
        target_id = None
        try:
            so = broker._api("/v2/orders", method="POST", body={
                "symbol": r["symbol"], "side": "sell", "type": "limit", "qty": f"{qty:.9g}",
                "limit_price": M.fmt_price(r["target"], True), "time_in_force": "gtc", "client_order_id": f"amhy-t-{r['id']}"})
            target_id = so.get("id")
        except RuntimeError as e:
            self.notify(f"No target order on {r['symbol']}", f"{e}. The app will still watch the stop.", "error")
        self.store.update(r["id"], status="open", qty=qty, entry=entry, filled_at=now, target_order_id=target_id)
        self.notify("Fast crypto entry", f"Bought {qty:.6g} {r['symbol']} at {T.money(entry)}; selling at "
                    f"{T.money(r['target'])}, stop {T.money(r['stop'])}.", "trade")

    def _watch_open(self, broker, settings, r, q, now, past_close_all):
        if r["target_order_id"]:
            o = broker.get_order(r["target_order_id"])
            if o.get("status") == "filled":
                return self._closed(r, M.num(o.get("filled_avg_price"), r["target"]), "target", now)
        else:  # no resting target (it failed to place): sell at market when reached
            if q and q["price"] >= r["target"]:
                return self._sell(broker, r, "target reached", q["price"])
        if q and q["price"] <= r["stop"]:
            return self._sell(broker, r, "stop-loss", q["price"])
        hold = M.num(settings["crypto_day"].get("max_hold_hours"), 0) * 3600
        if hold and r["filled_at"] and now - r["filled_at"] >= hold:
            return self._sell(broker, r, f"held {hold / 3600:g} hours", q and q["price"])
        if past_close_all:
            return self._sell(broker, r, "daily close-all time", q and q["price"])

    def _sell(self, broker, r, how, price):
        if r["target_order_id"]:
            T.cancel_and_wait(broker, [r["target_order_id"]])
            o = broker.get_order(r["target_order_id"])
            if o.get("status") == "filled":  # filled while we were cancelling
                return self._closed(r, M.num(o.get("filled_avg_price"), r["target"]), "target", time.time())
        broker._api("/v2/orders", method="POST", body={
            "symbol": r["symbol"], "side": "sell", "type": "market", "qty": f"{r['qty']:.9g}", "time_in_force": "gtc",
            "client_order_id": f"amhy-x-f{r['id']}"})
        self._closed(r, price or r["entry"], how, time.time())

    def _closed(self, r, exit_price, how, now):
        pnl = (exit_price - r["entry"]) * r["qty"]
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        self.store.update(r["id"], status="closed", exit=exit_price, pnl=round(pnl, 2), result=result, how=how, closed_at=now)
        if result == "loss":
            self.last_loss_at = now
        T.journal("crypto_fast_exit", symbol=r["symbol"], how=how, pnl=pnl)
        self.notify("Fast crypto trade closed", f"{r['symbol']} {how}: {T.money(pnl)} before fees.", "trade")

    def view(self, mode, now=None):
        now = now or time.time()
        rows = self.store.rows(mode, day=F.today_et().isoformat())
        active = [dict(r, seconds_left=max(0, int(r["expires"] - now)) if r["status"] == "buying" else None)
                  for r in self.store.rows(mode, ACTIVE)]
        done = [r for r in rows if r["status"] == "closed" and r["result"] != "not filled"]
        return {"active": active, "recent": done[-6:][::-1], "status": self.status,
                "today_pnl": round(sum(r["pnl"] or 0 for r in done), 2), "wins": sum(1 for r in done if r["result"] == "win"),
                "losses": sum(1 for r in done if r["result"] == "loss")}
