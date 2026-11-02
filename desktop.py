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

import features as F
import market as M
import phone as P
import trader as T
import updater as U

PORT = 8780
HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
STATIC = {"/assets/favicon.png": ("assets/favicon.png", "image/png"),
          "/assets/icon-180.png": ("assets/icon-180.png", "image/png"),
          "/assets/icon-192.png": ("assets/icon-192.png", "image/png"),
          "/assets/icon-512.png": ("assets/icon-512.png", "image/png"),
          "/vendor/qrcode.js": ("vendor/qrcode.js", "text/javascript; charset=utf-8"),
          "/market.js": ("market.js", "text/javascript; charset=utf-8"),
          "/features.js": ("features.js", "text/javascript; charset=utf-8"),
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
            if path.endswith("_hhmm") and (num > 2359 or num % 100 >= 60 or num != int(num)):
                raise ValueError(f"{path} must be a time like 930 (9:30 AM) or 2330 (11:30 PM)")
            return int(num) if isinstance(default, int) and num.is_integer() else num
        if isinstance(default, list):
            items = value if isinstance(value, list) else str(value).split(",")
            items = [str(x).upper().strip() for x in items if str(x).strip()]
            if not items:
                raise ValueError(f"{path} needs at least one symbol")
            if any(not x.replace(".", "").replace("/", "").isalnum() or len(x) > 12 for x in items):
                raise ValueError(f"{path} should be stock symbols separated by commas, like SPY, AAPL")
            return items
        text = str(value).strip()
        if path == "market.data_feed" and text not in ("iex", "sip"):
            raise ValueError("market.data_feed must be iex or sip")
        if path == "phone.push_topic":
            if text and not P.TOPIC_RE.fullmatch(text):
                raise ValueError("The notification topic should be 12-64 letters, numbers, - or _")
            return text
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
        self.update_info = {"available": False}
        self._store = None
        self._last_run = {}                      # scheduler: when each job last ran
        self.crypto_thread = None
        self.crypto_stop = threading.Event()
        self.crypto_next = None
        self.cday_thread = None
        self.cday_stop = threading.Event()
        self.cday_next = None
        self.bot_status = {}                     # why a bot isn't searching right now (run hours, daily goal)
        self.auth = P.Auth()
        self.phone_server = None                 # the second, passcode-protected address for the phone
        self.phone_info = {"installed": False, "ip": None, "dns": None}
        self._caffeinate = None
        self._goal_note = set()
        self.updated_files = []                  # files changed by the update installed at startup

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

    @property
    def store(self):
        if self._store is None:
            self._store = F.Store(self.market.db)
        return self._store

    def notify(self, title, body, kind="info"):
        s = self.settings()
        self.store.notify(title, body, kind, mac=s["market"].get("mac_notifications", True))
        self.log(f"{title}: {body}")
        self.push(title, body, kind, s)

    def push(self, title, body, kind="info", settings=None, click="#live"):
        """A notification on the phone (if set up). Tapping it opens Trader HQ when phone access is on."""
        P.push_async(settings or self.settings(), title, body, kind, click=self.phone_url(click))

    # --- phone access ---
    def phone_url(self, fragment=""):
        if not self.phone_server:
            return None
        host = self.phone_info.get("dns") or self.phone_info.get("ip")
        return f"http://{host}:{self.phone_server.server_address[1]}/{fragment}"

    def phone_sync(self, info=None):
        """Start or stop the phone address to match Settings, Tailscale and whether a passcode is set."""
        s = self.settings()
        want = s["phone"]["enabled"] and self.auth.has_passcode()
        self.phone_info = info or (P.tailscale_info() if want or s["phone"]["enabled"] else self.phone_info)
        ip = self.phone_info.get("ip") if want else None
        current = self.phone_server.server_address[0] if self.phone_server else None
        if current and current != ip:
            srv, self.phone_server = self.phone_server, None
            srv.shutdown()
            srv.server_close()
            self.log("Phone access is off.")
        if ip and not self.phone_server:
            try:
                srv = make_server(self, PORT, host=ip, remote=True, dns=self.phone_info.get("dns"))
            except OSError as e:
                self.log(f"Couldn't turn on phone access at {ip}: {e}")
                return
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.phone_server = srv
            self.log(f"Phone access is on: {self.phone_url()}")

    def keep_awake(self):
        """While phone access or a bot is on, stop the Mac from sleeping (macOS `caffeinate`)."""
        if sys.platform != "darwin":
            return
        s = self.settings()
        busy = self.phone_server or any(t and t.is_alive() for t in (self.day_thread, self.crypto_thread, self.cday_thread))
        want = s["phone"]["keep_mac_awake"] and busy
        running = self._caffeinate and self._caffeinate.poll() is None
        if want and not running:
            self._caffeinate = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        elif not want and running:
            self._caffeinate.terminate()
            self._caffeinate = None

    def phone_status(self):
        s = self.settings()
        return {"enabled": s["phone"]["enabled"], "has_passcode": self.auth.has_passcode(), "tailscale": self.phone_info,
                "url": self.phone_url(), "running": bool(self.phone_server), "sessions": self.auth.sessions(),
                "settings": s["phone"]}

    def can_trade_unattended(self):
        """Background jobs (recurring buys, protection) trade on paper, or on live only after LIVE was typed."""
        return self.settings()["mode"] == "paper" or self.live_ok

    def due(self, job, seconds):
        if time.time() - self._last_run.get(job, 0) >= seconds:
            self._last_run[job] = time.time()
            return True
        return False

    def tick(self):
        """One pass of the background jobs. Each job is independent: one failing doesn't stop the others."""
        s = self.settings()
        if not T.has_keys(s["mode"]):
            return
        jobs = []
        if self.due("alerts", 20):
            jobs.append(("alerts", lambda: self.store.check_alerts(self.market.quotes)))
        if self.can_trade_unattended():
            b = None
            if self.due("recurring", 60):
                jobs.append(("recurring", lambda: F.run_recurring(self.store, self.data_broker(), self.market, s, self.live_ok)))
            if s["market"]["auto_trailing_stop_pct"] > 0 and self.due("protect", 120):
                jobs.append(("protection", lambda: F.protect_positions(self.data_broker(), s["market"]["auto_trailing_stop_pct"], self.notify)))
            if self.due("crypto_protect", 10):
                jobs.append(("crypto stop-losses", lambda: F.protect_crypto_fills(self.data_broker(), self.store, self.notify)))
                jobs.append(("crypto bot trades", lambda: [self.log("Crypto bot: " + e) for e in
                                                         F.manage_crypto_bot(self.data_broker(), self.store, self.market, s, self.notify)]))
            if self.due("crypto_day", 10):
                jobs.append(("crypto day trades", lambda: [self.log("Crypto day: " + e) for e in
                                                         F.manage_crypto_day(self.data_broker(), self.store, self.market, s, self.notify)]))
        if self.due("phone", 60):
            jobs.append(("phone access", lambda: (self.phone_sync(), self.keep_awake())))
        if self.due("summary", 300):
            jobs.append(("daily summary", self.maybe_daily_summary))
        for name, job in jobs:
            try:
                job()
                self.forget()
            except Exception as e:  # noqa: BLE001 - shown in Activity, retried next pass
                if self.due(f"err-{name}", 1800):
                    self.log(f"Background {name} problem: {e}")

    def maybe_daily_summary(self, force=False):
        now_et = dt.datetime.now(T.ET)
        if not force and (now_et.weekday() >= 5 or (now_et.hour, now_et.minute) < (16, 5)):
            return None
        mode = self.settings()["mode"]
        day = now_et.date().isoformat()
        if not force and self.store.q("SELECT 1 FROM summaries WHERE day=? AND mode=?", (day, mode)):
            return None
        summary = F.daily_summary(self.data_broker())
        self.store.x("INSERT OR REPLACE INTO summaries VALUES (?,?,?)", (day, mode, json.dumps(summary)))
        self.notify("Daily summary", summary["text"], "summary")
        return summary

    def scheduler_loop(self):
        while not self.stopping.wait(10):
            self.tick()

    # --- run hours and daily goals (each automatic bot) ---
    BOT_NAMES = {"day": "Day trading", "crypto": "Crypto bot", "crypto_day": "Crypto day trading"}

    def bot_pl(self, key):
        b = self.data_broker()
        fills = self.cached("fills", 10, lambda: F.fetch_fills(b, max_pages=2))
        prices = {p["symbol"].replace("/", ""): float(p.get("current_price") or 0) for p in self.cached("positions", 4, b.positions)}
        return F.bot_pl_today(fills, self.BOT_NAMES[key], prices)

    def portfolio_value(self):
        """The account's value at the start of today (yesterday's close), used for the % goals."""
        acct = self.cached("account", 30, self.data_broker().account)
        return float(acct.get("last_equity") or acct.get("equity") or 0)

    def bot_gate(self, key, sec):
        """'ok' if a bot may search for trades now, 'wait' outside its run hours, or 'goal' once its daily
        profit goal or loss limit is reached (then it shuts off for the day). Records the reason."""
        if M.num(sec.get("daily_profit_target_pct"), 0) > 0 or M.num(sec.get("daily_loss_limit_pct"), 0) > 0:
            try:
                pl, value = self.bot_pl(key)["total"], self.portfolio_value()
            except RuntimeError:
                pl, value = 0.0, 0.0
            hit = F.goal_hit(sec, pl, value) if value else None
            if hit:
                pct_now = pl / value * 100
                what = (f"daily profit goal of +{sec['daily_profit_target_pct']:g}% reached" if hit == "profit"
                        else f"daily loss limit of -{sec['daily_loss_limit_pct']:g}% reached")
                text = f"Shut off for today: {what} ({T.money(pl)}, {pct_now:+.2f}% of the portfolio)"
                mark = (key, F.today_et().isoformat())
                if mark not in self._goal_note:
                    self._goal_note.add(mark)
                    self.notify(self.BOT_NAMES[key], text + ". No more searches today, so no more Claude usage. "
                                "Open trades keep their stop-losses and targets.", "goal")
                self.bot_status[key] = text
                return "goal"
        if not T.in_run_window(sec):
            self.bot_status[key] = (f"Waiting for run hours ({T.fmt_hhmm(sec['run_from_hhmm'])} to "
                                    f"{T.fmt_hhmm(sec['run_to_hhmm'])} ET)")
            return "wait"
        self.bot_status[key] = ""
        return "ok"

    # --- crypto day trading loop ---
    def start_crypto_day(self):
        if self.cday_thread and self.cday_thread.is_alive():
            raise ValueError("Crypto day trading is already running.")
        b = self.broker()
        if not T.claude_backend():
            raise ValueError("Claude isn't set up yet: sign in to Claude Code on this Mac (run `claude` once).")
        self.bot_status["crypto_day"] = ""
        self.cday_stop.clear()
        self.cday_thread = threading.Thread(target=self._crypto_day_loop, args=(b,), daemon=True)
        self.cday_thread.start()
        self.log("Crypto day trading started.")

    def _crypto_day_loop(self, b):
        progress = self.set_busy("Crypto day trading")
        try:
            while not self.cday_stop.is_set() and not self.stopping.is_set():
                s = self.settings()
                sec = s["crypto_day"]
                if F.minutes_to(sec["close_all_hhmm"]) <= 30:
                    self.bot_status["crypto_day"] = (f"Done for today: trades close at {T.fmt_hhmm(sec['close_all_hhmm'])} ET. "
                                                     "Resumes after midnight ET.")
                    self.cday_next = None
                    self.cday_stop.wait(60)
                    continue
                gate = self.bot_gate("crypto_day", sec)
                if gate == "goal":
                    self.log("Crypto day trading: " + self.bot_status["crypto_day"] + ".")
                    break  # shut off; the trade watcher still manages open trades
                if gate == "wait":
                    self.cday_next = None
                    self.cday_stop.wait(60)
                    continue
                try:
                    self.add_plan(F.CryptoDayPlan(b, s, self.market, self.store, progress=progress), DAY_TTL)
                except Exception as e:  # noqa: BLE001
                    self.log(f"Crypto day trading: check skipped: {e}")
                finally:
                    self.busy = ""
                self.forget()
                wait = max(5, sec["check_every_min"]) * 60
                self.cday_next = time.time() + wait
                self.cday_stop.wait(wait)
        finally:
            self.cday_next = None

    def stop_crypto_day(self):
        self.cday_stop.set()
        self.log("Crypto day trading stopped searching. Open crypto day trades keep their stop-losses and targets "
                 "and still close at the daily close-all time while Trader HQ is open.")

    # --- crypto bot loop (24/7 while the app is open) ---
    def start_crypto(self):
        if self.crypto_thread and self.crypto_thread.is_alive():
            raise ValueError("The crypto bot is already running.")
        b, s = self.broker(), self.settings()
        if not T.claude_backend():
            raise ValueError("Claude isn't set up yet: sign in to Claude Code on this Mac (run `claude` once).")
        self.bot_status["crypto"] = ""
        self.crypto_stop.clear()
        self.crypto_thread = threading.Thread(target=self._crypto_loop, args=(b, s), daemon=True)
        self.crypto_thread.start()
        self.log("Crypto bot started. It checks the market every %d minutes while Trader HQ is open." % s["crypto"]["check_every_min"])

    def _crypto_loop(self, b, s):
        progress = self.set_busy("Crypto")
        try:
            while not self.crypto_stop.is_set() and not self.stopping.is_set():
                gate = self.bot_gate("crypto", self.settings()["crypto"])
                if gate == "goal":
                    F.finish_crypto_bot_today(self.store, b.mode)
                    self.log("Crypto bot: " + self.bot_status["crypto"] + ". Its open trades finish at their target or "
                             f"stop-loss, or are sold at {T.fmt_hhmm(self.settings()['crypto_day']['close_all_hhmm'])} ET.")
                    break  # shut off: no new trades
                if gate == "wait":
                    self.crypto_next = None
                    self.crypto_stop.wait(60)
                    continue
                try:
                    self.add_plan(F.CryptoPlan(b, s, self.market, self.store, progress=progress), DAY_TTL)
                except Exception as e:  # noqa: BLE001
                    self.log(f"Crypto: check skipped: {e}")
                finally:
                    self.busy = ""
                wait = max(5, self.settings()["crypto"]["check_every_min"]) * 60
                self.crypto_next = time.time() + wait
                self.crypto_stop.wait(wait)
        finally:
            self.crypto_next = None

    def stop_crypto(self):
        self.crypto_stop.set()
        self.log("Crypto bot stopped. Stop-losses already placed stay with Alpaca.")

    # --- watchlists ---
    def watchlists(self):
        lists = self.store.watchlists()
        syms = sorted({x for l in lists for x in l["symbols"]})
        quotes = self.market.quotes(syms) if syms else {}
        return {"lists": lists, "quotes": quotes}

    def watchlist_action(self, body):
        action = body.get("action")
        if action == "create":
            return {"id": self.store.create_list(body.get("name", ""))}
        lid = int(M.num(body.get("id"), 0))
        if action == "delete":
            self.store.delete_list(lid)
        elif action in ("add", "remove"):
            sym = self.valid_symbol(body.get("symbol"))
            self.store.set_watch(lid, sym, action == "add")
        elif action == "use":
            target = body.get("target")
            if target not in ("long_term", "day", "options", "crypto"):
                raise ValueError("unknown bot")
            lst = next((l for l in self.store.watchlists() if l["id"] == lid), None)
            if not lst or not lst["symbols"]:
                raise ValueError("That watchlist is empty.")
            crypto = [x for x in lst["symbols"] if M.is_crypto(x)]
            stocks = [x for x in lst["symbols"] if not M.is_crypto(x)]
            syms = crypto if target == "crypto" else stocks
            if not syms:
                raise ValueError("That list has no " + ("crypto" if target == "crypto" else "stocks or ETFs") + " for this bot.")
            save_settings({"watchlist": syms} if target == "long_term" else {target: {"watchlist": syms}})
            self.log(f"The {target.replace('_', '-')} bot now watches: {', '.join(syms)}.")
        else:
            raise ValueError("unknown action")
        return {"ok": True}

    def valid_symbol(self, symbol):
        sym = str(symbol or "").upper().strip()
        self.market.ensure_assets()
        if not self.market.db.asset(sym):
            raise ValueError(f"{sym or 'That'} isn't a listed stock, ETF or crypto that Alpaca can trade.")
        return sym

    def live_view(self, symbol=""):
        """Everything the Live tab shows: today's bot fills and exits for one symbol, open trades with their
        stops and targets, and today's scoreboard."""
        b = self.data_broker()
        norm = lambda x: str(x or "").upper().replace("/", "")
        fills = self.cached("fills", 5, lambda: F.fetch_fills(b, max_pages=2))
        positions = self.cached("positions", 4, b.positions)
        orders = self.cached("orders", 4, b.open_orders)
        today = F.today_et()
        on_day = lambda iso: dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(T.ET).date() == today
        today_fills = [f for f in fills if on_day(f["time"])]
        trades, _ = F.match_trades(fills)
        closed = [t for t in trades if on_day(t["closed"])]
        rows = []
        for p in positions:
            if T.is_option(p):
                continue
            row = self.position_row(p)
            exits = [o for o in orders if norm(o.get("symbol")) == norm(p["symbol"]) and o.get("side") == "sell"]
            stops = [M.num(o.get("stop_price")) for o in exits if o.get("type") in ("stop", "stop_limit", "trailing_stop")]
            targets = [M.num(o.get("limit_price")) for o in exits if o.get("type") == "limit"]
            price = row["current_price"]
            cday = [r for r in F.crypto_day_rows(self.store, b.mode, ("open",)) if norm(r["symbol"]) == norm(p["symbol"])]
            targets += [r["target"] for r in cday]  # crypto targets are watched by the app, not sent as orders
            targets += [tg for sy, tg in self.store.q("SELECT symbol, target FROM crypto_entries WHERE mode=? AND "
                                                       "status IN ('protected','unprotected') AND target IS NOT NULL", (b.mode,))
                        if norm(sy) == norm(p["symbol"])]
            row["stop"] = max([x for x in stops if x], default=None)
            row["target"] = min([x for x in targets if x], default=None)
            row["to_stop_pct"] = (row["stop"] / price - 1) * 100 if row["stop"] and price else None
            row["to_target_pct"] = (row["target"] / price - 1) * 100 if row["target"] and price else None
            rows.append(row)
        symbols = []
        for x in [r["link"] for r in rows] + [F.CryptoPlan.slash(f["symbol"]) if f["asset_class"] == "crypto" else f["symbol"]
                                            for f in reversed(today_fills) if f["asset_class"] != "us_option"] + self.settings()["day"]["watchlist"]:
            if x and x not in symbols:
                symbols.append(x)
        sym = (symbol or "").upper() or (symbols[0] if symbols else "SPY")
        return {
            "symbol": sym, "symbols": symbols[:12], "positions": rows,
            "fills": [f for f in today_fills if norm(f["symbol"]) == norm(sym)],
            "orders": [{"type": o.get("type"), "side": o.get("side"), "qty": o.get("qty"), "stop_price": M.num(o.get("stop_price")),
                        "limit_price": M.num(o.get("limit_price")), "status": o.get("status")}
                       for o in orders if norm(o.get("symbol")) == norm(sym)]
                      + [{"type": "limit", "side": "sell", "qty": r["qty"], "stop_price": None, "limit_price": r["target"], "status": "watched"}
                         for r in F.crypto_day_rows(self.store, b.mode, ("open",)) if norm(r["symbol"]) == norm(sym) and r["target"]]
                      + [{"type": "limit", "side": "sell", "qty": q, "stop_price": None, "limit_price": tg, "status": "watched"}
                         for sy, q, tg in self.store.q("SELECT symbol, qty, target FROM crypto_entries WHERE mode=? AND "
                                                       "status IN ('protected','unprotected') AND target IS NOT NULL", (b.mode,))
                         if norm(sy) == norm(sym)],
            "bots": self._bots_today(),
            "score": {"closed": len(closed), "wins": sum(1 for t in closed if t["pnl"] > 0),
                      "losses": sum(1 for t in closed if t["pnl"] < 0), "realized": sum(t["pnl"] for t in closed),
                      "unrealized": sum(r["unrealized_pl"] for r in rows), "fills": len(today_fills)},
        }

    def _bots_today(self):
        out = {}
        s = self.settings()
        try:
            value = self.portfolio_value()
        except RuntimeError:
            value = 0.0
        for key in self.BOT_NAMES:
            sec = s[key]
            try:
                pl = self.bot_pl(key)
            except RuntimeError:
                pl = {"realized": 0, "unrealized": 0, "total": 0}
            out[key] = {**pl, "status": self.bot_status.get(key, ""), "in_hours": T.in_run_window(sec),
                        "target_pct": sec.get("daily_profit_target_pct", 0), "limit_pct": sec.get("daily_loss_limit_pct", 0),
                        "portfolio": value,
                        "run_from": sec.get("run_from_hhmm", 0), "run_to": sec.get("run_to_hhmm", 0),
                        "every": sec.get("check_every_min")}
        return out

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
            if body["side"] == "sell" and body.get("order_class") != "oco":
                T.cancel_protection(b, sym)  # free shares held by a trailing stop or crypto stop-loss
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
            T.cancel_protection(b, str(symbol).upper())
            b._api(f"/v2/positions/{sym}", method="DELETE")
        T.journal("close_position", mode=b.mode, trade={"symbol": symbol, "side": "sell", "qty": "all"})
        self.log(f"Selling all {symbol} at market.")
        self.forget()

    def check_updates(self):
        try:
            self.update_info = {**U.check(), "error": None}
        except RuntimeError as e:
            self.update_info = {"available": False, "error": str(e)}
        return self.update_info

    def update_loop(self):
        delay = 5
        while not self.stopping.wait(delay):
            info = self.check_updates()
            delay = 3600 if info.get("error") else 6 * 3600

    def save_github_token(self, key):
        key = key.strip()
        if not re.fullmatch(r"(github_pat_|ghp_)[A-Za-z0-9_]{20,255}", key):
            raise ValueError("GitHub tokens start with github_pat_ or ghp_.")
        lines = []
        if T.CONFIG_FILE.exists():
            lines = [l for l in T.CONFIG_FILE.read_text().splitlines() if not l.startswith("TRADERHQ_GITHUB_TOKEN=")]
        T.save_private(T.CONFIG_FILE, "\n".join(lines + [f"TRADERHQ_GITHUB_TOKEN={key}"]) + "\n")
        os.environ["TRADERHQ_GITHUB_TOKEN"] = key
        self.log("GitHub token saved for updates.")
        return self.check_updates()

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
            "auto_approve": T.can_auto_approve(s), "auto_enabled": bool(s.get("auto_approve")), "auto_rules": s["auto_rules"],
            "plans": self.plans[:8], "proposals": [self.public(p) for p in self.proposals[:40]],
            "activity": self.activity[:60], "day": self.day_info(), "error": None,
            "notifications": self.store.notifications(15) if T.DATA.exists() else [],
            "crypto_day": {"running": bool(self.cday_thread and self.cday_thread.is_alive()), "next_check": self.cday_next},
            "bot_status": self.bot_status,
            "crypto": {"running": bool(self.crypto_thread and self.crypto_thread.is_alive()),
                       "next_check": self.crypto_next},
            "update": {**self.update_info, "just_updated": len(self.updated_files),
                       "version": (U.installed().get("sha") or "")[:7]},
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
        crypto = p.get("asset_class") == "crypto"
        name = T.describe_option(p["symbol"]) if option else F.CryptoPlan.slash(p["symbol"]) if crypto else p["symbol"]
        return {"symbol": p["symbol"], "name": name, "link": None if option else name,
                "kind": "option" if option else "crypto" if crypto else "stock", "qty": float(p["qty"]),
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
                warning = ""
                if plan.kind in ("run", "day") and t.get("side") == "buy":
                    soon = F.earnings_soon(self.market, t.get("symbol", ""))
                    if soon:
                        warning = f"Earnings report on {soon}: prices often jump or drop sharply around earnings."
                p = {"id": uuid.uuid4().hex[:10], "plan_id": entry["id"], "index": i, "kind": plan.kind, "warning": warning,
                     "label": plan.label, "headline": plan.headline(t, o), "reason": t.get("reason", ""),
                     "risk": t.get("risk", ""), "question": plan.question(t, o) if ok else "",
                     "status": "pending" if ok else "blocked", "why": "" if ok else why,
                     "created": now(), "expires": now() + ttl, "trade": t, "order": o, "result": ""}
                self.proposals.insert(0, p)
                if not ok:
                    T.journal("blocked", mode=plan.mode, trade=t, reason=why)
                elif T.can_auto_approve(s, plan.kind, T.trade_value(plan.kind, t, o)):
                    self._place(p, auto=True)
        del self.proposals[100:]
        if plan.outlook:
            self.log(f"{plan.label}: Claude's view: {plan.outlook}")
        if not plan.trades:
            self.log(f"{plan.label}: {plan.empty_message}")
        else:
            waiting = [p for p in self.proposals if p["plan_id"] == entry["id"] and p["status"] == "pending"]
            if waiting:
                self.log(f"{plan.label}: {len(waiting)} suggestion(s) waiting for your approval.")
                more = f" (+{len(waiting) - 1} more)" if len(waiting) > 1 else ""
                self.push("Approval needed", f"{waiting[-1]['headline']}{more}. Valid for {int(ttl // 60)} min.", "approval", s)
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
        if kind not in ("run", "options", "crypto"):
            raise ValueError("unknown plan")
        if self.busy:
            raise ValueError(f"Busy: {self.busy}")
        b = self.broker()
        s = self.settings()
        if not T.claude_backend():
            raise ValueError("Claude isn't set up yet: sign in to Claude Code on this Mac (run `claude` once), "
                             "or add an Anthropic API key in Settings.")
        self.cache.pop("claude", None)
        label = {"run": "Long-term", "options": "Options", "crypto": "Crypto"}[kind]
        self.busy = f"{label}: asking Claude for a plan..."

        def work():
            try:
                if kind == "crypto":
                    plan = F.CryptoPlan(b, s, self.market, self.store, progress=self.set_busy(label))
                else:
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
        self.bot_status["day"] = ""
        self.day_stop.clear()
        self.day_thread = threading.Thread(target=self._day_loop, args=(b, s, state), daemon=True)
        self.day_thread.start()
        self.log("Day trading started.")

    def _day_loop(self, b, s, state):
        progress = self.set_busy("Day trading")
        try:
            while not self.day_stop.is_set() and not self.stopping.is_set():
                sec = self.settings()["day"]
                gate = self.bot_gate("day", sec)
                if gate == "goal" and self.due("day-goal-log", 3600):
                    self.log("Day trading: " + self.bot_status["day"] + ". Day trades still close before the bell, then it stops.")
                if gate != "ok":
                    try:  # still close day trades before the bell, even while not searching
                        clock = b.clock()
                        near_close = not clock["is_open"] or T.minutes_until_close(clock) <= sec["flatten_min_before_close"]
                    except RuntimeError:
                        near_close = False
                    if not near_close:
                        self.day_next = None
                        self.day_stop.wait(60)
                        continue
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

def make_handler(app, shutdown, restart=lambda: None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _host_ok(self):  # blocks other websites reaching the bot through a look-alike address
            ip, port = self.server.server_address[:2]
            host = self.headers.get("Host", "")
            if self.server.remote:
                names = {ip} | ({self.server.dns} if self.server.dns else set())
                return host in {f"{n}:{port}" for n in names}
            return host in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == P.COOKIE:
                    return v
            return ""

        def _signed_in(self):
            return not self.server.remote or app.auth.valid(self._token())

        def _send(self, code, body, ctype="application/json", filename=None, cookie=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if cookie is not None:
                self.send_header("Set-Cookie", f"{P.COOKIE}={cookie}; HttpOnly; SameSite=Strict; Path=/; "
                                 f"Max-Age={31536000 if cookie else 0}")
            if self.server.remote:
                self.send_header("X-Frame-Options", "DENY")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(P.MANIFEST).encode(), "application/manifest+json")
            if self.server.remote and not self._signed_in():
                if path in ("/", "/index.html"):
                    return self._send(200, P.LOGIN_PAGE.encode(), "text/html; charset=utf-8")
                if path in ("/assets/favicon.png", "/assets/icon-180.png", "/assets/icon-192.png", "/assets/icon-512.png"):
                    file, ctype = STATIC[path]
                    return self._send(200, (HERE / file).read_bytes(), ctype)
                return self._send(401, {"error": "Please sign in again.", "login": True})
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if path == "/api/phone":
                    if self.server.remote:
                        return self._send(403, {"error": REMOTE_ONLY_MAC})
                    if params.get("refresh"):
                        app.phone_sync()
                    return self._send(200, app.phone_status())
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
                if path == "/api/live":
                    return self._send(200, app.live_view(params.get("symbol", "")))
                if path == "/api/orders/recent":
                    return self._send(200, {"rows": app.recent_orders()})
                if path == "/api/watchlists":
                    return self._send(200, app.watchlists())
                if path == "/api/alerts":
                    return self._send(200, {"rows": app.store.alerts()})
                if path == "/api/recurring":
                    return self._send(200, {"rows": app.store.recurring(), "mode": app.settings()["mode"]})
                if path == "/api/notifications":
                    return self._send(200, {"rows": app.store.notifications(100)})
                if path == "/api/reports/performance":
                    return self._send(200, F.performance(app.data_broker(), app.market, params.get("period", "3M")))
                if path == "/api/reports/tax":
                    year = int(params.get("year") or dt.date.today().year)
                    report = F.tax_report(app.data_broker(), year)
                    if params.get("format") == "csv":
                        return self._send(200, F.tax_csv(report).encode(), "text/csv; charset=utf-8",
                                          filename=f"trader-hq-gains-{year}.csv")
                    return self._send(200, report)
                if path == "/api/reports/daily-pl":
                    return self._send(200, app.cached("daily-pl", 30, lambda: F.daily_pl(app.data_broker())))
                if path == "/api/reports/summaries":
                    rows = app.store.q("SELECT json FROM summaries WHERE mode=? ORDER BY day DESC LIMIT 30",
                                       (app.settings()["mode"],))
                    return self._send(200, {"rows": [json.loads(r[0]) for r in rows]})
                if path == "/api/calendar":
                    b = app.data_broker()
                    held = [F.CryptoPlan.slash(p["symbol"]) if p.get("asset_class") == "crypto" else p["symbol"]
                            for p in b.positions() if p.get("asset_class") != "us_option"]
                    syms = sorted(set(held) | set(app.store.watched_symbols()))
                    has_key = bool(T.load_config() or os.environ.get("FINNHUB_API_KEY"))
                    return self._send(200, {"rows": F.calendar(app.market, syms), "has_finnhub": has_key, "symbols": len(syms)})
                if path == "/api/market/events":
                    sym = app.valid_symbol(params.get("symbol"))
                    out = {"earnings": None, "dividends": []}
                    try:
                        out["earnings"] = F.earnings(app.market, sym)
                    except RuntimeError:
                        pass
                    if not M.is_crypto(sym):
                        try:
                            out["dividends"] = F.dividends(app.market, sym)
                        except RuntimeError:
                            pass
                    return self._send(200, out)
                if path == "/api/ping":
                    return self._send(200, {"ok": True})
                if path == "/api/state":
                    app.expire_old()
                    return self._send(200, {**app.state(), "remote": self.server.remote})
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
                if self.server.remote:
                    if path == "/api/phone/login":
                        try:
                            token = app.auth.login(body.get("passcode"), body.get("device"), self.client_address[0])
                        except PermissionError as e:
                            return self._send(401, {"error": str(e)})
                        app.log(f"A phone signed in ({str(body.get('device') or 'Phone')[:40]}).")
                        return self._send(200, {"ok": True}, cookie=token)
                    if not self._signed_in():
                        return self._send(401, {"error": "Please sign in again.", "login": True})
                    if path == "/api/phone/logout":
                        app.auth.revoke(token=self._token())
                        return self._send(200, {"ok": True}, cookie="")
                    if path in REMOTE_BLOCKED or path.startswith("/api/phone/"):
                        return self._send(403, {"error": REMOTE_ONLY_MAC})
                    if path == "/api/settings":
                        posted = body.get("settings") or {}
                        if "phone" in posted or ("mode" in posted and posted["mode"] != app.settings()["mode"]):
                            return self._send(403, {"error": REMOTE_ONLY_MAC})
                elif path == "/api/phone/passcode":
                    app.auth.set_passcode(body.get("passcode"))
                    app.log("Phone passcode set. Any phones that were signed in need to sign in again.")
                    app.phone_sync()
                    return self._send(200, app.phone_status())
                elif path == "/api/phone/revoke":
                    app.auth.revoke(sid=body.get("id")) if body.get("id") else app.auth.revoke()
                    return self._send(200, app.phone_status())
                elif path == "/api/phone/new-topic":
                    save_settings({"phone": {"push_topic": P.new_topic()}})
                    return self._send(200, app.phone_status())
                elif path == "/api/phone/test-push":
                    try:
                        P.push(app.settings(), "Trader HQ test", "Phone notifications are working.", "info",
                               click=app.phone_url("#live"), force=True)
                    except (OSError, ValueError) as e:
                        raise RuntimeError(f"Couldn't reach ntfy.sh ({e})") from None
                    return self._send(200, {"ok": True})
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
                elif path == "/api/watchlists":
                    return self._send(200, app.watchlist_action(body))
                elif path == "/api/alerts":
                    if body.get("action") == "delete":
                        app.store.delete_alert(int(M.num(body.get("id"), 0)))
                    else:
                        app.store.add_alert(app.valid_symbol(body.get("symbol")), body.get("kind"), body.get("value"), body.get("note", ""))
                        app.log(f"Alert set on {body.get('symbol', '').upper()}.")
                elif path == "/api/recurring":
                    action = body.get("action", "add")
                    rid = int(M.num(body.get("id"), 0))
                    if action == "add":
                        mode = app.settings()["mode"]
                        if mode == "live" and not app.live_ok:
                            raise NeedLive("Type LIVE to confirm real-money trading first.")
                        sym = app.valid_symbol(body.get("symbol"))
                        app.store.add_recurring(sym, body.get("amount"), body.get("frequency"), body.get("day"), mode)
                        app.log(f"Recurring buy of {sym} set up.")
                    elif action in ("pause", "resume"):
                        app.store.set_recurring_active(rid, action == "resume")
                    elif action == "delete":
                        app.store.delete_recurring(rid)
                    else:
                        raise ValueError("unknown action")
                elif path == "/api/reports/summary-now":
                    return self._send(200, app.maybe_daily_summary(force=True))
                elif path == "/api/market/explain":
                    sym = app.valid_symbol(body.get("symbol"))
                    info = app.market.info(sym)
                    held = next((app.position_row(p) for p in app.data_broker().positions()
                                 if p["symbol"] in (sym, sym.replace("/", ""))), None)
                    return self._send(200, F.explain(app.market, info, held))
                elif path == "/api/crypto/start":
                    app.start_crypto()
                elif path == "/api/crypto-day/start":
                    app.start_crypto_day()
                elif path == "/api/crypto-day/stop":
                    app.stop_crypto_day()
                elif path == "/api/crypto/stop":
                    app.stop_crypto()
                elif path == "/api/market/scan":
                    kind = body.get("kind", "stock")
                    return self._send(200, app.market.scan(kind, body.get("filters") or {}, str(body.get("sort", "change_pct")),
                                                           symbols=app.store.watched_symbols() if kind == "watchlist" else None))
                elif path == "/api/orders/preview":
                    return self._send(200, app.manual_order(body.get("order") or {}, place=False))
                elif path == "/api/orders":
                    return self._send(200, app.manual_order(body.get("order") or {}, place=True))
                elif path == "/api/positions/close":
                    app.close_position(body.get("symbol", ""))
                elif path == "/api/github-token":
                    return self._send(200, app.save_github_token(str(body.get("key", ""))))
                elif path == "/api/check-updates":
                    return self._send(200, app.check_updates())
                elif path == "/api/finnhub-key":
                    app.save_finnhub_key(str(body.get("key", "")))
                elif path == "/api/cancel-order":
                    app.cancel_order(str(body.get("id", "")))
                elif path == "/api/restart":
                    if app.day_info()["running"] or (app.crypto_thread and app.crypto_thread.is_alive()) or \
                            (app.cday_thread and app.cday_thread.is_alive()):
                        raise ValueError("Stop day trading and the crypto bot first, then restart.")
                    app.stopping.set()
                    self._send(200, {"ok": True})
                    threading.Thread(target=restart, daemon=True).start()
                    return
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


REMOTE_ONLY_MAC = "For safety, this can only be changed on the Mac."
# Things a phone can't do: change keys or phone settings, switch paper/live, or quit the app it depends on.
REMOTE_BLOCKED = {"/api/keys", "/api/anthropic-key", "/api/github-token", "/api/finnhub-key", "/api/quit"}


def make_server(app, port=PORT, host="127.0.0.1", remote=False, dns=None):
    holder = {}

    def restart():
        """Install any update, then start a fresh copy of the app on the same address."""
        time.sleep(0.3)
        try:
            changed = U.update(quiet=True)
            if changed:
                os.environ["TRADERHQ_JUST_UPDATED"] = str(len(changed))
        except Exception:  # noqa: BLE001 - restart anyway
            pass
        os.environ["TRADERHQ_NO_UPDATE_ONCE"] = "1"  # already checked just now
        holder["s"].shutdown()
        holder["s"].server_close()
        os.execv(sys.executable, [sys.executable, str(HERE / "trader.py"), "app", "--no-open"])

    if remote:  # the phone address restarts with the app; the app's own server does the restart
        restart = app_restart_via_main
    server = ThreadingHTTPServer((host, port), make_handler(app, lambda: holder["s"].shutdown(), restart))
    server.remote, server.dns = remote, dns
    holder["s"] = server
    if not remote:
        MAIN["restart"] = restart
    return server


MAIN = {}


def app_restart_via_main():
    if MAIN.get("restart"):
        MAIN["restart"]()


def open_window(url):
    chrome = Path("/Applications/Google Chrome.app")
    if sys.platform == "darwin" and chrome.exists():  # an app-style window with no tabs or address bar
        subprocess.Popen(["open", "-na", "Google Chrome", "--args", f"--app={url}", "--window-size=1400,950"])
    else:
        webbrowser.open(url)


def serve(open_win=True, port=PORT):
    url = f"http://127.0.0.1:{port}"
    app = App()
    try:
        server = make_server(app, port)
    except OSError:
        print(f"Trader HQ is already running. Opening {url}")
        if open_win:
            open_window(url)
        return
    if os.environ.pop("TRADERHQ_NO_UPDATE_ONCE", None) != "1" and os.environ.get("TRADERHQ_NO_UPDATE") != "1":
        try:
            changed = U.update(quiet=True)
        except Exception as e:  # never let an update problem stop the app from opening
            changed = []
            app.log(f"Couldn't check for updates: {e}")
        if changed:  # restart so the new version is the one that runs
            print(f"Installed an update ({len(changed)} files). Restarting...")
            server.server_close()
            os.environ["TRADERHQ_JUST_UPDATED"] = str(len(changed))
            os.execv(sys.executable, [sys.executable, str(HERE / "trader.py"), "app"] + ([] if open_win else ["--no-open"]))
    try:
        if refresh_launcher():
            app.log("The Trader HQ icon on your Desktop was refreshed for this version.")
    except Exception:  # noqa: BLE001 - never block startup over the icon
        pass
    if os.environ.get("TRADERHQ_JUST_UPDATED"):
        app.updated_files = ["x"] * int(os.environ.pop("TRADERHQ_JUST_UPDATED") or 0)
        app.log("Trader HQ was updated to the latest version.")
    threading.Thread(target=app.update_loop, daemon=True).start()
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    threading.Thread(target=lambda: (app.phone_sync(), app.keep_awake()), daemon=True).start()
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
  for i in $(seq 1 300); do curl -s -o /dev/null "$URL/api/ping" && break; sleep 0.2; done
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
  <key>CFBundleVersion</key><string>1.1</string>
  <key>CFBundleIconFile</key><string>TraderHQ</string>
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


def install_app(desktop=None, python=None, here=None, home=None, quiet=False):
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
    icon = here / "assets" / "TraderHQ.icns"
    if icon.exists():
        (app / "Contents" / "Resources").mkdir()
        shutil.copyfile(icon, app / "Contents" / "Resources" / "TraderHQ.icns")
    launcher = macos / "TraderHQ"
    launcher.write_text(launcher_text(here, python))
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.utime(app)  # nudge Finder to show the new icon
    if not quiet:
        print(f'Created "{app}". Double-click it to open the dashboard.')
        print("If you move this folder later, run `python trader.py install-app` again from its new place.")
    return app


def launcher_text(here, python=None):
    return LAUNCHER.format(dir=shell_quote(here), python=shell_quote(python or sys.executable), port=PORT)


def refresh_launcher(desktop=None, here=None):
    """Keep the Desktop icon current: if this version changed the launcher or the icon, rebuild it in place."""
    here = Path(here or HERE)
    app = (Path(desktop) if desktop else Path.home() / "Desktop") / "Trader HQ.app"
    plist = app / "Contents" / "Info.plist"
    if not plist.exists() or "local.traderhq.launcher" not in plist.read_text(errors="ignore"):
        return False  # no Trader HQ icon (or someone else's app with this name): leave it alone
    launcher = app / "Contents" / "MacOS" / "TraderHQ"
    icon = app / "Contents" / "Resources" / "TraderHQ.icns"
    src_icon = here / "assets" / "TraderHQ.icns"
    current = (plist.read_text() == PLIST and launcher.exists() and launcher.read_text() == launcher_text(here)
               and (not src_icon.exists() or (icon.exists() and icon.read_bytes() == src_icon.read_bytes())))
    if current:
        return False
    try:
        install_app(desktop=app.parent, here=here, quiet=True)
    except (RuntimeError, OSError):
        return False
    return True
