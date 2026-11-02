"""
Trader HQ desktop app: a dashboard that runs the bot in a window instead of the terminal.

  python trader.py app            # start it and open the window
  python trader.py install-app    # put a "Trader HQ" icon on your Desktop

It is a small web server that only your own Mac can reach (http://127.0.0.1:8780). The window shows
the dashboard; trading keeps running in the background until you press Quit in the dashboard.
"""
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import market as M
import trader as T

PORT = 8780
HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
STATIC = {"/market.js": ("market.js", "text/javascript; charset=utf-8"),
          "/vendor/lightweight-charts.js": ("vendor/lightweight-charts.js", "text/javascript; charset=utf-8")}
DAY_TTL = 180          # seconds a day-trade suggestion can be approved before it goes stale
PLAN_TTL = 30 * 60     # seconds a long-term or options suggestion can be approved
PERIODS = {"1W": ("1W", "1H"), "1M": ("1M", "1D"), "3M": ("3M", "1D"), "1Y": ("1A", "1D")}


class NeedLive(RuntimeError):
    pass


def now():
    return time.time()


# ---------- settings from the dashboard form ----------

def clean_settings(posted):
    """Coerce the form's values to the types in DEFAULT_SETTINGS. Raises ValueError with a friendly message."""
    def coerce(path, default, value):
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{path} must be on or off")
            return value
        if isinstance(default, (int, float)):
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{path} must be a number") from None
            if num < 0 or num != num:
                raise ValueError(f"{path} can't be negative")
            return int(num) if isinstance(default, int) and num.is_integer() else num
        if isinstance(default, list):
            items = value if isinstance(value, list) else str(value).split(",")
            items = [str(x).upper().strip() for x in items if str(x).strip()]
            if not items:
                raise ValueError(f"{path} needs at least one symbol")
            if any(not x.replace(".", "").isalnum() or len(x) > 10 for x in items):
                raise ValueError(f"{path} should be stock symbols separated by commas, like SPY, AAPL")
            return items
        text = str(value).strip()
        if path == "market.data_feed" and text not in ("iex", "sip"):
            raise ValueError("market.data_feed must be iex or sip")
        if path == "market.sec_contact_email":
            if text and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                raise ValueError("market.sec_contact_email doesn't look like an email address")
            return text
        if not text:
            raise ValueError(f"{path} can't be empty")
        return text[:2000]

    if not isinstance(posted, dict):
        raise ValueError("settings must be an object")
    out = {}
    for key, default in T.DEFAULT_SETTINGS.items():
        if key not in posted:
            continue
        if isinstance(default, dict):
            section = posted[key] if isinstance(posted[key], dict) else {}
            out[key] = {k: coerce(f"{key}.{k}", d, section[k]) for k, d in default.items() if k in section}
        elif key == "mode":
            if posted[key] not in T.ENDPOINTS:
                raise ValueError('mode must be "paper" or "live"')
            out[key] = posted[key]
        else:
            out[key] = coerce(key, default, posted[key])
    for key in ("max_order_usd",):
        if key in out and out[key] <= 0:
            raise ValueError(f"{key} must be more than 0")
    return out


def save_settings(posted):
    clean = clean_settings(posted)
    if clean.get("mode") == "live" and not T.has_keys("live"):
        raise ValueError("Add your live Alpaca keys first (Settings > Alpaca keys).")
    current = T.load_settings()
    for key, value in clean.items():
        if isinstance(value, dict):
            current[key].update(value)
        else:
            current[key] = value
    T.save_private(T.SETTINGS_FILE, json.dumps(current, indent=2) + "\n")
    return T.load_settings()


# ---------- the app's state ----------

class App:
    def __init__(self, broker_factory=None):
        self.broker_factory = broker_factory or T.Broker
        self.lock = threading.RLock()           # one order at a time
        self.live_ok = False                     # LIVE typed since the app started
        self.busy = ""                           # what the bot is doing, shown in the header
        self.plans = []                          # newest first: {id, kind, label, outlook, note, created}
        self.proposals = []                      # newest first
        self._plan_objects = {}
        self.activity = []                       # newest first: {time, text}
        self.day_thread = None
        self.day_stop = threading.Event()
        self.day_next = None
        self.day_state = None
        self.cache = {}
        self.stopping = threading.Event()
        self.market = M.Market(self.data_broker)

    # --- helpers ---
    def log(self, text):
        text = str(text).strip()
        if text:
            self.activity.insert(0, {"time": dt.datetime.now().strftime("%H:%M:%S"), "text": text})
            del self.activity[200:]

    def settings(self):
        return T.load_settings()

    def broker(self):
        s = self.settings()
        if s["mode"] == "live" and not self.live_ok:
            raise NeedLive("Type LIVE to confirm real-money trading first.")
        try:
            return self.broker_factory(s["mode"])
        except SystemExit as e:  # Broker exits with a message when keys are missing
            raise RuntimeError(str(e.code)) from None

    def data_broker(self):
        """For reading prices and company data. Reading moves no money, so live mode doesn't need LIVE typed."""
        s = self.settings()
        try:
            return self.broker_factory(s["mode"])
        except SystemExit as e:
            raise RuntimeError(str(e.code)) from None

    # --- orders you place yourself ---
    def manual_order(self, spec, place):
        s = self.settings()
        sym = str((spec or {}).get("symbol", "")).upper().strip()
        self.market.ensure_assets()
        asset = self.market.db.asset(sym)
        if not asset:
            raise ValueError(f"{sym or 'That symbol'} isn't a listed stock, ETF or crypto that Alpaca can trade.")
        b = self.broker() if place else self.data_broker()
        quote = self.market.quotes([sym]).get(sym)
        positions = b.positions()
        held = next((float(p["qty"]) for p in positions if p["symbol"] in (sym, sym.replace("/", ""))), 0.0)
        cash = float(b.account()["cash"])
        body, info = M.build_order(spec, asset, quote, held, s, cash)
        if not place:
            return {"ok": True, **info, "confirm": s["market"]["confirm_manual_orders"] or s["mode"] == "live"}
        if s["mode"] == "live" and not spec.get("confirmed"):
            raise ValueError("Live orders need the review step. Press Review first.")
        with self.lock:
            started = time.perf_counter()
            order = b._api("/v2/orders", method="POST", body=body)
            took = (time.perf_counter() - started) * 1000
        T.journal("manual_order", mode=b.mode, trade={"symbol": sym, "side": body["side"],
                                                     "qty": body.get("qty") or body.get("notional")},
                  order=body, order_id=order.get("id"), status=order.get("status"))
        self.log(f"{info['summary']}: sent in {took:.0f} ms ({order.get('status', 'submitted')}).")
        self.forget()
        return {"ok": True, **info, "status": order.get("status"), "took_ms": round(took), "id": order.get("id")}

    def recent_orders(self):
        b = self.data_broker()
        rows = b._api("/v2/orders", params={"status": "all", "limit": 60, "nested": "true"}) or []
        out = []
        for o in rows:
            legs = o.get("legs") or []
            out.append({"id": o["id"], "symbol": o["symbol"], "side": o["side"], "qty": o.get("qty"),
                        "notional": o.get("notional"), "type": o.get("type"), "order_class": o.get("order_class"),
                        "limit_price": o.get("limit_price"), "stop_price": o.get("stop_price"),
                        "trail_percent": o.get("trail_percent"), "status": o.get("status"),
                        "filled_qty": o.get("filled_qty"), "filled_avg_price": o.get("filled_avg_price"),
                        "submitted_at": o.get("submitted_at"), "time_in_force": o.get("time_in_force"),
                        "legs": [{"id": l["id"], "type": l.get("type"), "side": l.get("side"),
                                  "limit_price": l.get("limit_price"), "stop_price": l.get("stop_price"),
                                  "status": l.get("status")} for l in legs]})
        return out

    def close_position(self, symbol):
        b = self.broker()
        self.market.ensure_assets()
        if not self.market.db.asset(str(symbol).upper()):
            raise ValueError("unknown symbol")
        sym = str(symbol).upper().replace("/", "")
        with self.lock:
            b._api(f"/v2/positions/{sym}", method="DELETE")
        T.journal("close_position", mode=b.mode, trade={"symbol": symbol, "side": "sell", "qty": "all"})
        self.log(f"Selling all {symbol} at market.")
        self.forget()

    def save_finnhub_key(self, key):
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{10,64}", key):
            raise ValueError("That doesn't look like a Finnhub API key.")
        lines = []
        if T.CONFIG_FILE.exists():
            lines = [l for l in T.CONFIG_FILE.read_text().splitlines() if not l.startswith("FINNHUB_API_KEY=")]
        T.save_private(T.CONFIG_FILE, "\n".join(lines + [f"FINNHUB_API_KEY={key}"]) + "\n")
        os.environ["FINNHUB_API_KEY"] = key
        self.log("Finnhub API key saved.")

    def cached(self, key, seconds, fn):
        hit = self.cache.get(key)
        if hit and now() - hit[0] < seconds:
            return hit[1]
        value = fn()
        self.cache[key] = (now(), value)
        return value

    def forget(self):
        self.cache.clear()

    # --- dashboard data ---
    def state(self):
        s = self.settings()
        out = {
            "mode": s["mode"], "live_ok": self.live_ok, "busy": self.busy,
            "has_keys": {"paper": T.has_keys("paper"), "live": T.has_keys("live")},
            "claude": self.cached("claude", 120, lambda: bool(T.claude_backend())),
            "auto_approve": T.can_auto_approve(s),
            "plans": self.plans[:8], "proposals": [self.public(p) for p in self.proposals[:40]],
            "activity": self.activity[:60], "day": self.day_info(), "error": None,
        }
        if not out["has_keys"][s["mode"]] or (s["mode"] == "live" and not self.live_ok):
            return out
        try:
            b = self.broker()
            acct = self.cached("account", 4, b.account)
            clock = self.cached("clock", 15, b.clock)
            positions = self.cached("positions", 4, b.positions)
            orders = self.cached("orders", 4, b.open_orders)
        except RuntimeError as e:
            out["error"] = str(e)
            return out
        equity, last = float(acct["equity"]), float(acct["last_equity"])
        held = [p for p in positions if T.is_option(p)]
        out.update({
            "account": {"equity": equity, "last_equity": last, "cash": float(acct["cash"]),
                        "options_paid": sum(float(p["cost_basis"]) for p in held),
                        "options_cap": equity * s["options"]["max_total_options_pct"] / 100},
            "clock": {"is_open": clock["is_open"], "next_open": clock.get("next_open"),
                      "next_close": clock.get("next_close"),
                      "now_et": dt.datetime.now(T.ET).strftime("%-I:%M %p ET") if sys.platform != "win32"
                      else dt.datetime.now(T.ET).strftime("%I:%M %p ET")},
            "positions": [self.position_row(p) for p in positions],
            "orders": [{"id": o["id"], "symbol": o["symbol"],
                        "name": T.describe_option(o["symbol"]) if o.get("asset_class") == "us_option" else o["symbol"],
                        "side": o["side"], "qty": o.get("qty"), "type": o.get("type"),
                        "limit_price": o.get("limit_price"), "stop_price": o.get("stop_price"),
                        "status": o.get("status"), "submitted_at": o.get("submitted_at")} for o in orders],
        })
        return out

    @staticmethod
    def position_row(p):
        option = T.is_option(p)
        return {"symbol": p["symbol"], "name": T.describe_option(p["symbol"]) if option else p["symbol"],
                "kind": "option" if option else "stock", "qty": float(p["qty"]),
                "avg_entry_price": float(p["avg_entry_price"]), "current_price": float(p.get("current_price") or 0),
                "market_value": float(p["market_value"]), "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": float(p["unrealized_plpc"]) * 100}

    def equity(self, period):
        span, frame = PERIODS.get(period, PERIODS["1M"])
        b = self.broker()
        h = self.cached(f"equity-{period}", 60, lambda: b.portfolio_history(span, frame))
        points = [{"t": t, "v": v} for t, v in zip(h.get("timestamp") or [], h.get("equity") or [])
                  if v is not None]
        return {"period": period, "points": points}

    def day_info(self):
        running = bool(self.day_thread and self.day_thread.is_alive())
        state = self.day_state or {}
        return {"running": running, "next_check": self.day_next if running else None,
                "entries_today": len(state.get("entries", [])), "flattened": state.get("flattened", False)}

    @staticmethod
    def public(p):
        return {k: v for k, v in p.items() if k not in ("trade", "order")} | {
            "trade": {k: p["trade"].get(k) for k in ("symbol", "side", "qty")}}

    # --- plans and approvals ---
    def add_plan(self, plan, ttl):
        s = plan.settings
        entry = {"id": uuid.uuid4().hex[:10], "kind": plan.kind, "label": plan.label, "outlook": plan.outlook,
                 "note": plan.note, "created": now(), "mode": plan.mode,
                 "empty": plan.empty_message if not plan.trades else ""}
        self.plans.insert(0, entry)
        del self.plans[20:]
        self._plan_objects[entry["id"]] = plan
        for i, t in enumerate(plan.trades, 1):
            with self.lock:
                ok, why, o = plan.check(t)
                p = {"id": uuid.uuid4().hex[:10], "plan_id": entry["id"], "index": i, "kind": plan.kind,
                     "label": plan.label, "headline": plan.headline(t, o), "reason": t.get("reason", ""),
                     "risk": t.get("risk", ""), "question": plan.question(t, o) if ok else "",
                     "status": "pending" if ok else "blocked", "why": "" if ok else why,
                     "created": now(), "expires": now() + ttl, "trade": t, "order": o, "result": ""}
                self.proposals.insert(0, p)
                if not ok:
                    T.journal("blocked", mode=plan.mode, trade=t, reason=why)
                elif T.can_auto_approve(s):
                    self._place(p, auto=True)
        del self.proposals[100:]
        if not plan.trades:
            self.log(f"{plan.label}: {plan.empty_message}")
        return entry

    def decide(self, proposal_id, approve):
        with self.lock:
            p = next((x for x in self.proposals if x["id"] == proposal_id), None)
            if not p or p["status"] != "pending":
                raise ValueError("That suggestion is no longer waiting for a decision.")
            plan = self._plan_objects.get(p["plan_id"])
            if plan is None:
                raise ValueError("That suggestion is no longer available.")
            if plan.mode == "live" and not self.live_ok:
                raise NeedLive("Type LIVE to confirm real-money trading first.")
            if now() > p["expires"]:
                p["status"], p["result"] = "expired", "Too old: prices have moved. Ask Claude again."
                return self.public(p)
            if not approve:
                p["status"], p["result"] = "skipped", "Skipped."
                T.journal("skipped", mode=plan.mode, trade=p["trade"])
                return self.public(p)
            if not plan.b.clock()["is_open"]:
                p["status"], p["why"] = "blocked", "the market is closed"
                return self.public(p)
            ok, why, o = plan.check(p["trade"])  # check again: earlier approvals may have used the room
            if not ok:
                p["status"], p["why"] = "blocked", why
                T.journal("blocked", mode=plan.mode, trade=p["trade"], reason=why)
                return self.public(p)
            p["order"] = o
            self._place(p)
            return self.public(p)

    def _place(self, p, auto=False):
        plan = self._plan_objects[p["plan_id"]]
        try:
            order = T.place_trade(plan, p["trade"], p["order"], p["index"])
        except RuntimeError as e:
            p["status"], p["result"] = "failed", f"Order failed: {e}"
            self.log(f"{p['headline']}: order failed: {e}")
            return
        p["status"] = "placed"
        p["result"] = f"Order sent ({order.get('status', 'submitted')})" + (", auto-approved (paper)" if auto else "")
        self.log(f"{p['headline']}: {p['result']}")
        self.forget()

    def expire_old(self):
        for p in self.proposals:
            if p["status"] == "pending" and now() > p["expires"]:
                p["status"], p["result"] = "expired", "Too old: prices have moved. Ask Claude again."

    def start_plan(self, kind):
        if kind not in ("run", "options"):
            raise ValueError("unknown plan")
        if self.busy:
            raise ValueError(f"Busy: {self.busy}")
        b = self.broker()
        s = self.settings()
        if not T.claude_backend():
            raise ValueError("Claude isn't set up yet: sign in to Claude Code on this Mac (run `claude` once), "
                             "or add an Anthropic API key in Settings.")
        self.cache.pop("claude", None)
        label = "Long-term" if kind == "run" else "Options"
        self.busy = f"{label}: asking Claude for a plan..."

        def work():
            try:
                plan = (T.LongTermPlan if kind == "run" else T.OptionsPlan)(b, s, progress=self.set_busy(label))
                self.add_plan(plan, PLAN_TTL)
            except T.Notice as e:
                self.log(f"{label}: {e}")
            except Exception as e:  # shown to the user rather than crashing the app
                self.log(f"{label}: {e}")
            finally:
                self.busy = ""
                self.forget()

        threading.Thread(target=work, daemon=True).start()

    def set_busy(self, label):
        def progress(text):
            text = str(text).strip()
            if text:
                self.busy = f"{label}: {text}"
                self.log(f"{label}: {text}")
        return progress

    # --- day trading ---
    def start_day(self):
        if self.day_thread and self.day_thread.is_alive():
            raise ValueError("Day trading is already running.")
        b = self.broker()
        s = self.settings()
        if not T.claude_backend():
            raise ValueError("Claude isn't set up yet: sign in to Claude Code on this Mac (run `claude` once).")
        if not b.clock()["is_open"]:
            raise ValueError("The market is closed. Day trading runs 9:30am-4pm ET on weekdays.")
        state = T.load_day_state(b)
        if state["flattened"]:
            raise ValueError("Day trading already finished for today (positions were closed).")
        self.day_state = state
        self.day_stop.clear()
        self.day_thread = threading.Thread(target=self._day_loop, args=(b, s, state), daemon=True)
        self.day_thread.start()
        self.log("Day trading started.")

    def _day_loop(self, b, s, state):
        progress = self.set_busy("Day trading")
        try:
            while not self.day_stop.is_set() and not self.stopping.is_set():
                try:
                    plan, wait, done = T.day_tick(b, s, state, progress=progress)
                except Exception as e:
                    self.log(f"Day trading: check skipped: {e}")
                    T.journal("day_error", mode=b.mode, error=str(e))
                    plan, wait, done = None, s["day"]["check_every_min"] * 60, False
                finally:
                    self.busy = ""
                if done:
                    self.log("Day trading: done for today.")
                    break
                if plan:
                    self.add_plan(plan, DAY_TTL)
                self.forget()
                self.day_next = now() + wait
                self.day_stop.wait(wait)
        finally:
            self.day_next = None
            self.forget()

    def stop_day(self):
        self.day_stop.set()
        self.log("Day trading stopped. Open day trades keep their stop-loss and take-profit orders until "
                 "the close; use 'Close day trades now' to sell them.")

    def flatten_now(self):
        b = self.broker()
        if not T.day_state_file(b.mode).exists():
            raise ValueError("No day trading today, so there's nothing to close.")
        with self.lock:
            self.day_stop.set()
            state = T.load_day_state(b)
            self.log("Closing all day positions...")
            T.flatten(b, state, progress=self.log)
            self.day_state = state
        self.forget()

    # --- other actions ---
    def confirm_live(self, text):
        if text != "LIVE":
            raise ValueError("Type LIVE in capital letters to confirm.")
        self.live_ok = True
        self.log("Live trading confirmed for this session. Every order still needs your approval.")

    def cancel_order(self, order_id):
        b = self.broker()
        with self.lock:
            b.cancel_order(order_id)
        self.forget()
        self.log("Order cancel requested.")

    def save_keys(self, mode, key, secret):
        if mode not in T.ENDPOINTS:
            raise ValueError("unknown account type")
        acct = T.save_keys(mode, key, secret)
        self.forget()
        self.log(f"{mode.title()} keys saved. Account value {T.money(acct['equity'])}.")

    def save_anthropic_key(self, key):
        key = key.strip()
        if not key.startswith("sk-ant-"):
            raise ValueError("Anthropic API keys start with sk-ant-.")
        lines = []
        if T.CONFIG_FILE.exists():
            lines = [l for l in T.CONFIG_FILE.read_text().splitlines() if not l.startswith("ANTHROPIC_API_KEY=")]
        T.save_private(T.CONFIG_FILE, "\n".join(lines + [f"ANTHROPIC_API_KEY={key}"]) + "\n")
        os.environ["ANTHROPIC_API_KEY"] = key
        self.cache.pop("claude", None)
        self.log("Anthropic API key saved.")


# ---------- web server (only reachable from this Mac) ----------

def make_handler(app, shutdown):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self):  # blocks other websites reaching the bot through a look-alike address
            port = self.server.server_address[1]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            path, _, query = self.path.partition("?")
            params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if path in STATIC:
                    file, ctype = STATIC[path]
                    return self._send(200, (HERE / file).read_bytes(), ctype)
                mk = app.market
                if path == "/api/market/assets":
                    return self._send(200, mk.list_assets(params.get("kind", "stock"), params.get("q", ""),
                                                          max(0, int(params.get("offset", 0))),
                                                          min(100, max(1, int(params.get("limit", 50))))))
                if path == "/api/market/search":
                    return self._send(200, {"rows": mk.search(params.get("q", ""))})
                if path == "/api/market/quote":
                    syms = [x for x in params.get("symbols", "").upper().split(",") if x][:50]
                    return self._send(200, {"quotes": mk.quotes(syms) if syms else {}})
                if path == "/api/market/bars":
                    return self._send(200, mk.bars(params.get("symbol", "").upper(), params.get("range", "1M")))
                if path == "/api/market/info":
                    info = mk.info(params.get("symbol", "").upper())
                    sym = info["asset"]["symbol"]
                    held = next((app.position_row(p) for p in app.data_broker().positions()
                                 if p["symbol"] in (sym, sym.replace("/", ""))), None)
                    return self._send(200, {**info, "position": held, "mode": app.settings()["mode"]})
                if path == "/api/market/news":
                    return self._send(200, {"rows": mk.news(params.get("symbol", "").upper())})
                if path == "/api/market/movers":
                    return self._send(200, mk.movers("crypto" if params.get("market") == "crypto" else "stocks"))
                if path == "/api/market/actives":
                    return self._send(200, mk.most_actives("trades" if params.get("by") == "trades" else "volume"))
                if path == "/api/orders/recent":
                    return self._send(200, {"rows": app.recent_orders()})
                if path == "/api/ping":
                    return self._send(200, {"ok": True})
                if path == "/api/state":
                    app.expire_old()
                    return self._send(200, app.state())
                if path == "/api/equity":
                    return self._send(200, app.equity(params.get("period", "1M")))
                if path == "/api/settings":
                    return self._send(200, {"settings": app.settings(), "defaults": T.DEFAULT_SETTINGS})
                if path == "/api/history":
                    return self._send(200, {"lines": list(reversed(T.history_lines()))[:300]})
            except NeedLive as e:
                return self._send(409, {"error": str(e), "need_live": True})
            except (RuntimeError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            # A custom header can't be sent by other websites without permission, so they can't trade for you.
            if not self._host_ok() or self.headers.get("X-Trader-HQ") != "1":
                return self._send(403, {"error": "forbidden"})
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 100_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path
                if path == "/api/plan":
                    app.start_plan(body.get("kind"))
                elif path == "/api/decide":
                    return self._send(200, app.decide(body.get("id"), bool(body.get("approve"))))
                elif path == "/api/day/start":
                    app.start_day()
                elif path == "/api/day/stop":
                    app.stop_day()
                elif path == "/api/flatten":
                    app.flatten_now()
                elif path == "/api/confirm-live":
                    app.confirm_live(body.get("text", ""))
                elif path == "/api/settings":
                    was = app.settings()["mode"]
                    s = save_settings(body.get("settings"))
                    if s["mode"] != was:
                        app.live_ok = False
                        app.stop_day()
                    app.forget()
                elif path == "/api/keys":
                    app.save_keys(body.get("mode"), str(body.get("key", "")), str(body.get("secret", "")))
                elif path == "/api/anthropic-key":
                    app.save_anthropic_key(str(body.get("key", "")))
                elif path == "/api/market/scan":
                    return self._send(200, app.market.scan(body.get("kind", "stock"), body.get("filters") or {},
                                                           str(body.get("sort", "change_pct"))))
                elif path == "/api/orders/preview":
                    return self._send(200, app.manual_order(body.get("order") or {}, place=False))
                elif path == "/api/orders":
                    return self._send(200, app.manual_order(body.get("order") or {}, place=True))
                elif path == "/api/positions/close":
                    app.close_position(body.get("symbol", ""))
                elif path == "/api/finnhub-key":
                    app.save_finnhub_key(str(body.get("key", "")))
                elif path == "/api/cancel-order":
                    app.cancel_order(str(body.get("id", "")))
                elif path == "/api/quit":
                    app.stopping.set()
                    app.day_stop.set()
                    self._send(200, {"ok": True})
                    threading.Thread(target=shutdown, daemon=True).start()
                    return
                else:
                    return self._send(404, {"error": "not found"})
            except NeedLive as e:
                return self._send(409, {"error": str(e), "need_live": True})
            except (ValueError, RuntimeError, T.Notice) as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True})

    return Handler


def make_server(app, port=PORT):
    holder = {}
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, lambda: holder["s"].shutdown()))
    holder["s"] = server
    return server


def open_window(url):
    chrome = Path("/Applications/Google Chrome.app")
    if sys.platform == "darwin" and chrome.exists():  # an app-style window with no tabs or address bar
        subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}", "--window-size=1400,950"])
    else:
        webbrowser.open(url)


def serve(open_win=True, port=PORT):
    url = f"http://127.0.0.1:{port}"
    try:
        server = make_server(App(), port)
    except OSError:
        print(f"Trader HQ is already running. Opening {url}")
        if open_win:
            open_window(url)
        return
    print(f"Trader HQ dashboard: {url}  (press Quit in the dashboard, or Ctrl+C here, to stop)")
    if open_win:
        threading.Timer(0.5, open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------- Desktop icon ----------

LAUNCHER = """#!/bin/bash
# Trader HQ launcher: starts the dashboard in the background (if it isn't running) and opens its window.
DIR={dir}
PY={python}
LOG="$HOME/.amh-trader/app.log"
URL="http://127.0.0.1:{port}"
mkdir -p "$HOME/.amh-trader"
if [ ! -f "$DIR/trader.py" ]; then
  osascript -e 'display alert "Trader HQ" message "The Trader HQ folder was moved or deleted. Open Terminal in the new folder and run: python trader.py install-app"'
  exit 1
fi
if ! curl -s -o /dev/null "$URL/api/ping"; then
  cd "$DIR" && nohup "$PY" trader.py app --no-open >> "$LOG" 2>&1 &
  for i in $(seq 1 75); do curl -s -o /dev/null "$URL/api/ping" && break; sleep 0.2; done
  if ! curl -s -o /dev/null "$URL/api/ping"; then
    osascript -e 'display alert "Trader HQ couldn'"'"'t start" message "The details are in the file .amh-trader/app.log in your home folder. Open Terminal and run: cat ~/.amh-trader/app.log"'
    exit 1
  fi
fi
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args --app="$URL" --window-size=1400,950
else
  open "$URL"
fi
"""

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Trader HQ</string>
  <key>CFBundleDisplayName</key><string>Trader HQ</string>
  <key>CFBundleIdentifier</key><string>local.traderhq.launcher</string>
  <key>CFBundleExecutable</key><string>TraderHQ</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
"""


def shell_quote(path):
    return "'" + str(path).replace("'", "'\\''") + "'"


PROTECTED = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")


def protected_folder(folder, home=None):
    """macOS won't let a double-clicked app read these folders, so the bot can't live in them."""
    home = Path(home or Path.home()).resolve()
    folder = Path(folder).resolve()
    for name in PROTECTED:
        root = home / name
        if folder == root or root in folder.parents:
            return name
    return None


def install_app(desktop=None, python=None, here=None, home=None):
    """Create 'Trader HQ.app' on the Desktop. It runs this folder's copy of the bot with this Python."""
    here = Path(here or HERE)
    blocked = protected_folder(here, home)
    if blocked:
        raise RuntimeError(
            f"This folder is inside your {blocked} folder, which macOS doesn't let a double-clicked app read.\n"
            "Move it to your home folder and set it up again by pasting these lines one at a time:\n\n"
            f"  deactivate\n  mv {shell_quote(here)} ~/Trader-HQ\n  cd ~/Trader-HQ\n  rm -rf .venv\n"
            "  python3 -m venv .venv\n  source .venv/bin/activate\n  pip install -r requirements.txt\n"
            "  python trader.py install-app")
    desktop = Path(desktop) if desktop else Path.home() / "Desktop"
    app = desktop / "Trader HQ.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text(PLIST)
    launcher = macos / "TraderHQ"
    launcher.write_text(LAUNCHER.format(dir=shell_quote(here), python=shell_quote(python or sys.executable), port=PORT))
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f'Created "{app}". Double-click it to open the dashboard.')
    print("If you move this folder later, run `python trader.py install-app` again from its new place.")
    return app
