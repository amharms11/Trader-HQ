"""
Fast mode for stock day trading: Claude plans, code pulls the trigger.

Claude is too slow to watch prices second by second, so the work is split:

- Every few minutes Claude looks at 1-minute data and "arms" setups: buy SYMBOL if it trades above a trigger
  price, with a stop-loss and a target (an opening range breakout or a VWAP pullback). The app checks each
  setup against your limits and works out the number of shares from your risk per trade.
- Once a setup is approved (by you, or by auto-approve on paper), the engine watches the price every couple of
  seconds. When the price crosses the trigger it buys straight away with a bracket order, so the stop-loss and
  target sit at Alpaca from the moment it fills.
- Safety rules run in code before every entry: no chasing a price that already ran, no wide spreads, no stale
  prices, a cool-down after a loss, a daily cap on losing trades, plus all the usual day-trading limits. A time
  stop sells a trade that hasn't reached its target or stop after a set number of minutes.
"""
import datetime as dt
import json
import math
import threading
import time
import uuid

import market as M
import trader as T

SETUPS = {"orb": "Opening range breakout", "vwap_pullback": "VWAP pullback"}
OR_MINUTES = 5            # the opening range is the first 5 minutes (9:30-9:35 ET)
STALE_SEC = 90            # don't enter if the last trade is older than this
FILL_WAIT_SEC = 30        # cancel an entry that hasn't filled after this long (price moved away)
MIN_REWARD_RISK = 1.5

FAST_SYSTEM = """You are the planner for a fast intraday stock trading bot. You can't place orders yourself and
you're far too slow to react to every price tick, so you ARM setups and code pulls the trigger: when the price
trades at or above your trigger_price, the app buys within seconds with a bracket order (your stop and target).

You receive the account, today's open day trades, and for each watchlist stock: 1-minute based levels (opening
range = first 5 minutes, VWAP, high/low of day, recent 1-minute and 5-minute closes, relative volume) and the
last 20 days of daily data, plus the user's strategy, the setups allowed and the limits.

Arm at most 3 setups, or none. Setups:
- "orb" (opening range breakout, long only): trigger just above the opening range high (e.g. a few cents over),
  stop below the range (the range low, or the midpoint for a wide range). Best on stocks with high relative volume
  that opened strongly and hold above VWAP. Only valid after the first 5 minutes.
- "vwap_pullback" (long only): in an uptrend above VWAP, after a pullback toward VWAP, trigger just above the
  recent 1-minute high as price turns back up; stop just below VWAP or the pullback low.
Rules:
- The trigger must be at or a little above the current price (it's a buy-stop). Never arm a setup whose trigger is
  already far below the price: that move is gone.
- Target at least 1.5x the distance from trigger to stop, at a sensible level (prior high, round number, measured
  move). Keep stops beyond real levels, not arbitrary.
- valid_minutes: how long the setup stays valid if it doesn't trigger (5-30).
- Skip choppy, low-volume or wide-range stocks. Arming nothing is a fine answer.
- reason: 1-2 plain sentences a beginner can follow. risk: the main way it could fail.
- outlook: 1-2 sentences on the session so far, honest about uncertainty."""

FAST_SCHEMA = {
    "type": "object",
    "properties": {
        "outlook": {"type": "string"},
        "setups": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"}, "setup": {"type": "string", "enum": list(SETUPS)},
                "trigger_price": {"type": "number"}, "stop_price": {"type": "number"}, "target_price": {"type": "number"},
                "valid_minutes": {"type": "number"}, "reason": {"type": "string"}, "risk": {"type": "string"}},
            "required": ["symbol", "setup", "trigger_price", "stop_price", "target_price", "valid_minutes", "reason", "risk"],
            "additionalProperties": False}},
    },
    "required": ["outlook", "setups"], "additionalProperties": False,
}


# ---------- levels from 1-minute bars ----------

def _et(t):
    return dt.datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(T.ET)


def levels(bars):
    """Today's 1-minute bars (regular session) -> the levels the setups use."""
    if not bars:
        return None
    vol = sum(b["v"] for b in bars)
    vwap = sum((b.get("vw") or b["c"]) * b["v"] for b in bars) / vol if vol else bars[-1]["c"]
    opening = [b for b in bars if _et(b["t"]).hour == 9 and _et(b["t"]).minute < 30 + OR_MINUTES]
    or_ready = len(bars) > len(opening) or len(opening) >= OR_MINUTES
    ranges = [b["h"] - b["l"] for b in bars[-14:]]
    five = [bars[i:i + 5] for i in range(0, len(bars), 5)]
    recent_vol = sum(b["v"] for b in bars[-5:]) / min(5, len(bars))
    return {
        "last": round(bars[-1]["c"], 4), "vwap": round(vwap, 4),
        "high_of_day": round(max(b["h"] for b in bars), 4), "low_of_day": round(min(b["l"] for b in bars), 4),
        "opening_range_high": round(max(b["h"] for b in opening), 4) if opening and or_ready else None,
        "opening_range_low": round(min(b["l"] for b in opening), 4) if opening and or_ready else None,
        "avg_1min_range": round(sum(ranges) / len(ranges), 4),
        "last_15_closes_1min": [round(b["c"], 2) for b in bars[-15:]],
        "last_12_closes_5min": [round(g[-1]["c"], 2) for g in five[-12:]],
        "last_5min_volume_vs_session_avg": round(recent_vol / (vol / len(bars)), 2) if vol else None,
        "minutes_of_data": len(bars),
    }


def spread_pct(q):
    bid, ask = M.num((q or {}).get("bp"), 0), M.num((q or {}).get("ap"), 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100


def snapshots(broker, symbols, feed="iex"):
    """Latest trade and quote for each symbol: {sym: {"price", "time", "bid", "ask", "spread_pct"}}."""
    if not symbols:
        return {}
    res = broker._req(T.DATA_URL, "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed}) or {}
    out = {}
    for sym, s in res.items():
        trade, quote = (s or {}).get("latestTrade") or {}, (s or {}).get("latestQuote") or {}
        if not trade.get("p"):
            continue
        out[sym] = {"price": float(trade["p"]), "time": trade.get("t"), "bid": M.num(quote.get("bp"), 0),
                    "ask": M.num(quote.get("ap"), 0), "spread_pct": spread_pct(quote)}
    return out


def age_sec(iso, now=None):
    if not iso:
        return 1e9
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()


# ---------- Claude arms setups ----------

def fast_snapshot(broker, settings, state, minutes_left, feed="iex"):
    acct, positions = broker.account(), broker.stock_positions()
    dq = T.day_qty(positions, state)
    symbols = sorted(set(settings["day"]["watchlist"]) | set(dq))
    session_open = dt.datetime.now(T.ET).replace(hour=9, minute=30, second=0, microsecond=0)
    one_min = broker.bars(symbols, "1Min", session_open.isoformat(), feed=feed)
    daily = broker.daily_bars(symbols, days=20)
    live = snapshots(broker, symbols, feed)
    by_sym = {p["symbol"]: p for p in positions}
    return {
        "as_of": dt.datetime.now(T.ET).isoformat(timespec="seconds"),
        "minutes_until_close": minutes_left,
        "account": T.account_summary(acct),
        "all_holdings": {p["symbol"]: float(p["qty"]) for p in positions},
        "day_positions": [{"symbol": s, "qty": q, "avg_entry_price": float(by_sym[s]["avg_entry_price"]),
                           "current_price": float(by_sym[s]["current_price"])} for s, q in dq.items()],
        "entries_today": len(state["entries"]),
        "market": {s: {"latest_price": (live.get(s) or {}).get("price"),
                       "spread_pct": round((live.get(s) or {}).get("spread_pct") or 0, 3) or None,
                       "today_1min": levels(one_min.get(s, [])),
                       "daily_20d": T.summarize_bars(daily.get(s, []))} for s in symbols},
    }


def parse_setup(x):
    try:
        return {"symbol": str(x["symbol"]).upper().strip(), "setup": str(x["setup"]), "side": "buy",
                "trigger_price": float(x["trigger_price"]), "stop_price": float(x["stop_price"]),
                "target_price": float(x["target_price"]), "valid_minutes": float(x.get("valid_minutes") or 15),
                "reason": str(x.get("reason", "")), "risk": str(x.get("risk", "")), "qty": 0}
    except (KeyError, TypeError, ValueError):
        return None


def check_setup(t, snap, settings):
    """Hard limits for arming a setup. Returns (ok, reason, order) where order holds the shares and prices."""
    d = settings["day"]
    sym, setup = t["symbol"], t["setup"]
    m = snap["market"].get(sym) or {}
    lv, price = m.get("today_1min") or {}, m.get("latest_price")
    trig, stop, target = t["trigger_price"], t["stop_price"], t["target_price"]
    if setup not in SETUPS or not d.get(f"fast_{setup}", True):
        return False, f"the {SETUPS.get(setup, setup)} setup is turned off", None
    if sym not in d["watchlist"]:
        return False, f"{sym} isn't on your day-trading watchlist", None
    if not price or not lv:
        return False, f"no live price data for {sym}", None
    if snap["entries_today"] >= d["max_entries_per_day"]:
        return False, f"daily limit of {d['max_entries_per_day']} day-trade entries reached", None
    if snap["minutes_until_close"] <= d["no_new_entries_min_before_close"]:
        return False, "too close to the market close for a new day trade", None
    if not stop < trig < target:
        return False, "the stop must be below the trigger and the target above it", None
    stop_pct, take_pct = (trig - stop) / trig * 100, (target - trig) / trig * 100
    if not 0.1 <= stop_pct <= d["max_stop_loss_pct"]:
        return False, f"stop-loss must be between 0.1% and {d['max_stop_loss_pct']}% below the trigger (got {stop_pct:.2f}%)", None
    if take_pct > d["max_take_profit_pct"]:
        return False, f"target is more than {d['max_take_profit_pct']}% above the trigger", None
    if (target - trig) < MIN_REWARD_RISK * (trig - stop):
        return False, f"the target should be at least {MIN_REWARD_RISK:g}x the stop distance", None
    if trig < price * (1 - d["max_chase_pct"] / 100):
        return False, f"{sym} is already above the trigger ({T.money(price)} vs {T.money(trig)})", None
    if trig > price * 1.03:
        return False, f"the trigger is more than 3% above the price ({T.money(price)}), too far for a quick setup", None
    if setup == "orb":
        orh, orl = lv.get("opening_range_high"), lv.get("opening_range_low")
        if not orh:
            return False, "the opening range isn't complete yet (first 5 minutes)", None
        if trig < orh * 0.999 or stop < orl * 0.995:
            return False, "an opening range breakout needs the trigger at or above the range high and the stop no lower than the range low", None
    if setup == "vwap_pullback" and trig < lv["vwap"] * 0.999:
        return False, f"a VWAP pullback needs the trigger above VWAP ({T.money(lv['vwap'])})", None
    qty = int(min(d["max_risk_per_trade_usd"] / (trig - stop), settings["max_order_usd"] / trig))
    if qty < 1:
        return False, f"even 1 share risks more than {T.money(d['max_risk_per_trade_usd'])} or costs more than {T.money(settings['max_order_usd'])}", None
    why = T._buy_limits(sym, qty * trig, price, snap["all_holdings"].get(sym, 0), snap["account"], settings)
    if why:
        return False, why, None
    minutes = max(3, min(t["valid_minutes"], d["fast_arm_minutes"]))
    return True, "", {"qty": qty, "trigger": trig, "stop_price": stop, "take_profit": target, "minutes": minutes,
                      "risk": qty * (trig - stop), "limit_price": trig}


class FastPlan(T.Plan):
    kind, label, empty_message = "day", "Fast day setup", "No setups armed. Waiting for a clean one."

    def __init__(self, broker, settings, state, minutes_left, engine, progress=print, feed="iex"):
        super().__init__(broker, settings)
        self.state, self.engine = state, engine
        progress(f"[{dt.datetime.now(T.ET):%H:%M} ET] Looking for fast setups ({minutes_left} min until close)...")
        self.snap = fast_snapshot(broker, settings, state, minutes_left, feed)
        d = settings["day"]
        allowed = [k for k in SETUPS if d.get(f"fast_{k}", True)]
        limits = {k: d[k] for k in ("max_entries_per_day", "max_risk_per_trade_usd", "max_stop_loss_pct", "max_take_profit_pct",
                                    "max_chase_pct", "fast_arm_minutes", "no_new_entries_min_before_close")}
        limits["max_order_usd"] = settings["max_order_usd"]
        armed = [{"symbol": a["symbol"], "setup": a["setup"], "trigger": a["trigger"]} for a in engine.armed_list()]
        prompt = (f"<strategy>\n{d['strategy']}\n</strategy>\n\n<setups_allowed>{', '.join(allowed)}</setups_allowed>\n\n"
                  f"<watchlist>{', '.join(d['watchlist'])}</watchlist>\n\n<limits>\n{json.dumps(limits)}\n</limits>\n\n"
                  f"<already_armed>\n{json.dumps(armed)}\n</already_armed>\n\n"
                  f"<snapshot>\n{json.dumps({k: v for k, v in self.snap.items() if k != 'all_holdings'}, indent=1)}\n</snapshot>\n\n"
                  "Arm setups for the next few minutes (or none). Don't repeat ones already armed.")
        plan = T.ask_claude(prompt, FAST_SYSTEM, FAST_SCHEMA, effort="medium", kind="day", raw=True)
        self.outlook = str(plan.get("outlook", ""))
        armed_syms = {a["symbol"] for a in engine.armed_list()}
        self.trades = [x for x in (parse_setup(s) for s in (plan.get("setups") or [])[:3]) if x and x["symbol"] not in armed_syms]
        T.journal("fast_plan", mode=self.mode, outlook=self.outlook, setups=self.trades)

    def headline(self, t, o):
        q = o["qty"] if o else "?"
        return (f"ARM {SETUPS.get(t['setup'], t['setup'])}: buy {q} {t['symbol']} if it trades above {T.money(t['trigger_price'])} "
                f"(stop {T.money(t['stop_price'])}, target {T.money(t['target_price'])})")

    def check(self, t):
        return check_setup(t, self.snap, self.settings)

    def question(self, t, o):
        return (f"Arm it? For the next {o['minutes']:g} min, the app buys {o['qty']} {t['symbol']} within seconds if it trades "
                f"above {T.money(o['trigger'])} (max loss about {T.money(o['risk'])}).")

    def place(self, t, o, i):
        return self.engine.arm(t, o)


# ---------- the engine: watches prices and pulls the trigger ----------

class Engine:
    def __init__(self, log=print, notify=None):
        self.lock = threading.RLock()
        self.armed = []           # setups waiting for their trigger
        self.log = log
        self.notify = notify or (lambda *a, **k: None)
        self.last_loss_at = 0.0
        self.last_manage = 0.0
        self.status = ""

    # --- arming ---
    def arm(self, t, o, now=None):
        now = now or time.time()
        a = {"id": uuid.uuid4().hex[:8], "symbol": t["symbol"], "setup": t["setup"], "qty": o["qty"], "trigger": o["trigger"],
             "stop": o["stop_price"], "target": o["take_profit"], "armed_at": now, "expires": now + o["minutes"] * 60,
             "reason": t.get("reason", ""), "status": "armed"}
        with self.lock:
            self.armed = [x for x in self.armed if x["symbol"] != a["symbol"]] + [a]
        self.log(f"Fast: armed {a['symbol']} {SETUPS[a['setup']]}: buy {a['qty']} above {T.money(a['trigger'])} "
                 f"(stop {T.money(a['stop'])}, target {T.money(a['target'])}) for {o['minutes']:g} min.")
        return {"id": a["id"], "status": "armed"}

    def armed_list(self):
        with self.lock:
            return [dict(a) for a in self.armed]

    def disarm(self, aid=None, why="cancelled"):
        with self.lock:
            gone = [a for a in self.armed if aid is None or a["id"] == aid]
            self.armed = [a for a in self.armed if not (aid is None or a["id"] == aid)]
        for a in gone:
            self.log(f"Fast: {a['symbol']} setup {why}.")
        return len(gone)

    # --- the fast loop ---
    def tick(self, broker, settings, state, gate, now=None, feed="iex"):
        """One pass: check armed triggers, then manage open fast trades. gate() returns a reason not to enter, or ''."""
        now = now or time.time()
        d = settings["day"]
        with self.lock:
            for a in [a for a in self.armed if now >= a["expires"]]:
                self.disarm(a["id"], "expired without triggering")
            armed = list(self.armed)
        open_trades = [f for f in state.get("fast", []) if f["status"] in ("entering", "open")]
        syms = sorted({a["symbol"] for a in armed})
        if syms:
            try:
                live = snapshots(broker, syms, feed)
            except RuntimeError as e:
                self.status = f"Price check failed: {e}"
                live = {}
            for a in armed:
                self._check_trigger(broker, settings, state, gate, a, live.get(a["symbol"]), now)
        if open_trades and now - self.last_manage >= d.get("fast_manage_sec", 5):
            self.last_manage = now
            self.manage(broker, settings, state, now)

    def _check_trigger(self, broker, settings, state, gate, a, q, now):
        d = settings["day"]
        if not q:
            self.status = f"No live price for {a['symbol']}."
            return
        price = q["price"]
        if price <= a["stop"]:
            self.disarm(a["id"], f"dropped to its stop ({T.money(price)}) before triggering")
            return
        if price < a["trigger"]:
            return
        if price > a["trigger"] * (1 + d["max_chase_pct"] / 100):
            self.status = f"{a['symbol']} jumped past the trigger to {T.money(price)}: not chasing it."
            return  # stays armed in case it comes back into range
        if age_sec(q["time"]) > STALE_SEC:
            self.status = f"{a['symbol']} prices are stale; waiting for fresh trades."
            return
        if q["spread_pct"] is None or q["spread_pct"] > d["max_spread_pct"]:
            self.status = f"{a['symbol']} spread is too wide ({q['spread_pct'] or 0:.2f}%); waiting."
            return
        why = self.entry_block(settings, state, now) or gate()
        if why:
            self.disarm(a["id"], f"skipped: {why}")
            return
        self.enter(broker, settings, state, a, q, now)

    def entry_block(self, settings, state, now):
        d = settings["day"]
        if len(state["entries"]) >= d["max_entries_per_day"]:
            return f"daily limit of {d['max_entries_per_day']} entries reached"
        losses = sum(1 for f in state.get("fast", []) if f.get("result") == "loss")
        if d["max_losses_per_day"] and losses >= d["max_losses_per_day"]:
            return f"{losses} losing trades today (your limit is {d['max_losses_per_day']})"
        if d["cooldown_after_loss_min"] and now - self.last_loss_at < d["cooldown_after_loss_min"] * 60:
            return f"cooling down for {d['cooldown_after_loss_min']} min after a loss"
        if sum(1 for f in state.get("fast", []) if f["status"] in ("entering", "open")) >= d["max_open_fast_trades"]:
            return f"already {d['max_open_fast_trades']} fast trade(s) open"
        return ""

    def enter(self, broker, settings, state, a, q, now):
        d = settings["day"]
        cap = a["trigger"] * (1 + d["max_chase_pct"] / 100)
        limit = round(min(max(q["ask"], q["price"]) * 1.0005, cap), 2)
        qty = a["qty"]
        if limit - a["stop"] > 0 and qty * (limit - a["stop"]) > d["max_risk_per_trade_usd"]:
            qty = int(d["max_risk_per_trade_usd"] / (limit - a["stop"]))
        if qty < 1 or limit >= a["target"]:
            self.disarm(a["id"], "skipped: the entry price left too little room")
            return
        self.disarm(a["id"], "triggered")
        cid = f"amhd-f-{dt.datetime.now():%H%M%S}-{a['id']}"
        try:
            order = broker.place_bracket_buy(a["symbol"], qty, limit, a["target"], a["stop"], cid)
        except RuntimeError as e:
            self.notify("Fast entry failed", f"{a['symbol']}: {e}", "error")
            T.journal("fast_entry_failed", mode=broker.mode, symbol=a["symbol"], error=str(e))
            return
        legs = order.get("legs") or []
        state["entries"].append({"symbol": a["symbol"], "qty": qty, "order_id": order["id"], "leg_ids": [x["id"] for x in legs]})
        state.setdefault("fast", []).append({
            "id": a["id"], "symbol": a["symbol"], "setup": a["setup"], "qty": qty, "limit": limit, "stop": a["stop"],
            "target": a["target"], "order_id": order["id"], "leg_ids": [x["id"] for x in legs], "status": "entering",
            "placed_at": now, "filled_at": None, "entry": None, "exit": None, "result": None})
        T.save_day_state(broker.mode, state)
        T.journal("fast_entry", mode=broker.mode, symbol=a["symbol"], setup=a["setup"], qty=qty, limit=limit,
                  stop=a["stop"], target=a["target"], order_id=order["id"])
        self.notify("Fast entry", f"Buying {qty} {a['symbol']} up to {T.money(limit)} ({SETUPS[a['setup']]}); "
                    f"stop {T.money(a['stop'])}, target {T.money(a['target'])}.", "trade")

    def manage(self, broker, settings, state, now):
        """Entries that didn't fill, time stops, and recording wins and losses."""
        d = settings["day"]
        changed = False
        for f in state.get("fast", []):
            if f["status"] not in ("entering", "open"):
                continue
            try:
                o = broker.get_order(f["order_id"])
            except RuntimeError:
                continue
            legs = o.get("legs") or []
            if legs and not f["leg_ids"]:
                f["leg_ids"] = [x["id"] for x in legs]
                for e in state["entries"]:
                    if e["order_id"] == f["order_id"]:
                        e["leg_ids"] = f["leg_ids"]
                changed = True
            if f["status"] == "entering":
                if o.get("status") == "filled":
                    f.update(status="open", filled_at=now, entry=M.num(o.get("filled_avg_price"), f["limit"]))
                    changed = True
                elif o.get("status") in ("canceled", "expired", "rejected"):
                    f.update(status="closed", result="not filled")
                    changed = True
                    continue
                elif now - f["placed_at"] > FILL_WAIT_SEC and M.num(o.get("filled_qty"), 0) == 0:
                    T.cancel_and_wait(broker, [f["order_id"]])
                    f.update(status="closed", result="not filled")
                    self.log(f"Fast: {f['symbol']} entry didn't fill within {FILL_WAIT_SEC}s, cancelled.")
                    changed = True
                    continue
                else:
                    continue
            done = next((x for x in legs if x.get("status") == "filled"), None)
            if done:
                exit_price = M.num(done.get("filled_avg_price"), 0)
                self._closed(f, exit_price, "target" if done.get("type") == "limit" else "stop-loss", now)
                changed = True
                continue
            hold = d.get("time_stop_min", 0)
            if hold and f["filled_at"] and now - f["filled_at"] >= hold * 60:
                T.cancel_and_wait(broker, [x["id"] for x in legs if x.get("status") in T.OPEN_STATUSES] or f["leg_ids"])
                try:
                    sell = broker.place_market_sell(f["symbol"], f["qty"], f"amhx-t-{f['id']}")
                    price = M.num(sell.get("filled_avg_price"), 0) or (snapshots(broker, [f["symbol"]]).get(f["symbol"]) or {}).get("price", 0)
                except RuntimeError as e:
                    self.notify(f"Couldn't sell {f['symbol']}", f"Time stop: {e}", "error")
                    continue
                self._closed(f, price, f"time stop ({hold:g} min)", now)
                changed = True
        if changed:
            T.save_day_state(broker.mode, state)

    def _closed(self, f, exit_price, how, now):
        pnl = (exit_price - (f["entry"] or f["limit"])) * f["qty"] if exit_price else 0.0
        f.update(status="closed", exit=exit_price, closed_at=now, how=how, pnl=round(pnl, 2),
                 result="win" if pnl > 0 else "loss" if pnl < 0 else "flat")
        if pnl < 0:
            self.last_loss_at = now
        T.journal("fast_exit", symbol=f["symbol"], how=how, pnl=pnl)
        self.notify("Fast trade closed", f"{f['symbol']} {how}: {T.money(pnl)}.", "trade")

    def view(self, state, now=None):
        now = now or time.time()
        fast = (state or {}).get("fast", [])
        return {"armed": [{**a, "seconds_left": max(0, int(a["expires"] - now))} for a in self.armed_list()],
                "trades": [dict(f, minutes_in=round((now - f["filled_at"]) / 60, 1) if f.get("filled_at") else None)
                           for f in fast[-20:]],
                "status": self.status}
