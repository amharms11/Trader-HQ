"""Run with: python -m unittest test_desktop.py  (no network, no keys needed)"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import desktop as D
import trader as T


class FakeBroker:
    def __init__(self, mode="paper", is_open=True):
        self.mode, self.is_open, self.orders, self.cancelled = mode, is_open, [], []

    def account(self):
        return {"equity": "10000", "last_equity": "9900", "cash": "8000", "options_trading_level": 3}

    def clock(self):
        return {"is_open": self.is_open, "timestamp": "2026-11-02T15:00:00Z",
                "next_open": "2026-11-03T14:30:00Z", "next_close": "2026-11-02T21:00:00Z"}

    def positions(self):
        return [{"symbol": "SPY", "asset_class": "us_equity", "qty": "3", "avg_entry_price": "500",
                 "current_price": "510", "market_value": "1530", "unrealized_pl": "30", "unrealized_plpc": "0.02",
                 "cost_basis": "1500"},
                {"symbol": "AAPL261218C00200000", "asset_class": "us_option", "qty": "1", "avg_entry_price": "1.0",
                 "current_price": "1.2", "market_value": "120", "unrealized_pl": "20", "unrealized_plpc": "0.2",
                 "cost_basis": "100"}]

    def open_orders(self):
        return [{"id": "o9", "symbol": "SPY", "side": "buy", "qty": "1", "type": "limit", "limit_price": "499",
                 "status": "new", "asset_class": "us_equity"}]

    def portfolio_history(self, period, timeframe):
        return {"timestamp": [1, 2, 3], "equity": [9800, None, 10000]}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)


class FakePlan(T.Plan):
    kind, label = "run", "Long-term"

    def __init__(self, broker, settings, trades, room=2):
        super().__init__(broker, settings)
        self.outlook, self.trades, self.room = "Looks calm.", trades, room
        self.placed = []

    def check(self, t):
        if t["symbol"] == "BAD":
            return False, "not on your watchlist", None
        if len(self.placed) >= self.room:
            return False, "daily limit reached", None
        return True, "", {"qty": t["qty"], "limit_price": 100.0, "cost": 100.0 * t["qty"]}

    def question(self, t, o):
        return f"Buy {t['qty']} {t['symbol']}?"

    def place(self, t, o, i):
        self.placed.append(t["symbol"])
        return {"id": f"x{i}", "status": "accepted"}


def trade(sym="SPY", qty=1):
    return {"symbol": sym, "side": "buy", "qty": qty, "reason": "r", "risk": "k"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(T, "DATA", self.tmp), mock.patch.object(T, "JOURNAL_FILE", self.tmp / "j.jsonl"),
            mock.patch.object(T, "SETTINGS_FILE", self.tmp / "settings.json"),
            mock.patch.object(T, "CONFIG_FILE", self.tmp / "config.env"),
            mock.patch.object(T, "SOFI_CONFIG", self.tmp / "none.env"),
            mock.patch.object(T, "claude_backend", return_value="api"),
            mock.patch.dict(os.environ, {"ALPACA_PAPER_KEY_ID": "PK1", "ALPACA_PAPER_SECRET_KEY": "s"}, clear=True),
        ]
        for p in self.patches:
            p.start()
        self.broker = FakeBroker()
        self.app = D.App(broker_factory=lambda mode: self.broker)

    def tearDown(self):
        for p in self.patches:
            p.stop()


class ApprovalTests(Base):
    def test_pending_then_approve_places_order(self):
        plan = FakePlan(self.broker, T.load_settings(), [trade(), trade("BAD")])
        self.app.add_plan(plan, ttl=60)
        statuses = {p["headline"]: p["status"] for p in self.app.proposals}
        self.assertEqual(statuses, {"BUY 1 SPY": "pending", "BUY 1 BAD": "blocked"})
        pid = next(p["id"] for p in self.app.proposals if p["status"] == "pending")
        self.assertEqual(self.app.decide(pid, True)["status"], "placed")
        self.assertEqual(plan.placed, ["SPY"])
        with self.assertRaises(ValueError):  # can't approve twice
            self.app.decide(pid, True)

    def test_skip(self):
        plan = FakePlan(self.broker, T.load_settings(), [trade()])
        self.app.add_plan(plan, ttl=60)
        self.assertEqual(self.app.decide(self.app.proposals[0]["id"], False)["status"], "skipped")
        self.assertEqual(plan.placed, [])

    def test_rechecked_at_approval(self):
        plan = FakePlan(self.broker, T.load_settings(), [trade("A"), trade("B")], room=1)
        self.app.add_plan(plan, ttl=60)
        ids = [p["id"] for p in sorted(self.app.proposals, key=lambda p: p["index"])]
        self.assertEqual(self.app.decide(ids[0], True)["status"], "placed")
        second = self.app.decide(ids[1], True)
        self.assertEqual((second["status"], second["why"]), ("blocked", "daily limit reached"))

    def test_expired(self):
        plan = FakePlan(self.broker, T.load_settings(), [trade()])
        self.app.add_plan(plan, ttl=-1)
        self.assertEqual(self.app.decide(self.app.proposals[0]["id"], True)["status"], "expired")
        self.assertEqual(plan.placed, [])

    def test_market_closed_at_approval(self):
        plan = FakePlan(self.broker, T.load_settings(), [trade()])
        self.app.add_plan(plan, ttl=60)
        self.broker.is_open = False
        self.assertEqual(self.app.decide(self.app.proposals[0]["id"], True)["status"], "blocked")
        self.assertEqual(plan.placed, [])

    def test_auto_approve_paper_only(self):
        s = T.load_settings()
        s["auto_approve"] = True
        plan = FakePlan(self.broker, s, [trade()])
        self.app.add_plan(plan, ttl=60)
        self.assertEqual(plan.placed, ["SPY"])
        s["mode"] = "live"
        live = FakePlan(FakeBroker(mode="live"), s, [trade()])
        self.app.add_plan(live, ttl=60)
        self.assertEqual(live.placed, [])
        self.assertEqual(self.app.proposals[0]["status"], "pending")

    def test_live_plan_needs_live_confirmation(self):
        s = T.load_settings()
        s["mode"] = "live"
        plan = FakePlan(FakeBroker(mode="live"), s, [trade()])
        self.app.add_plan(plan, ttl=60)
        with self.assertRaises(D.NeedLive):
            self.app.decide(self.app.proposals[0]["id"], True)
        self.app.confirm_live("LIVE")
        self.assertEqual(self.app.decide(self.app.proposals[0]["id"], True)["status"], "placed")


class SettingsTests(Base):
    def test_clean_settings_coerces_types(self):
        out = D.clean_settings({"max_order_usd": "1000", "watchlist": "spy, qqq", "auto_approve": True,
                                "limit_slippage_pct": "0.25", "day": {"check_every_min": "5"},
                                "options": {"watchlist": ["aapl"]}, "unknown": 1})
        self.assertEqual(out, {"max_order_usd": 1000, "watchlist": ["SPY", "QQQ"], "auto_approve": True,
                               "limit_slippage_pct": 0.25, "day": {"check_every_min": 5},
                               "options": {"watchlist": ["AAPL"]}})

    def test_clean_settings_rejects_bad_values(self):
        for bad in ({"max_order_usd": "abc"}, {"max_order_usd": -5}, {"max_order_usd": 0},
                    {"watchlist": ""}, {"watchlist": "SPY; rm -rf"}, {"mode": "real"}, {"auto_approve": "yes"}):
            with self.assertRaises(ValueError, msg=bad):
                D.clean_settings(bad)

    def test_save_merges_and_live_needs_keys(self):
        s = D.save_settings({"max_order_usd": 900, "day": {"check_every_min": 7}})
        self.assertEqual((s["max_order_usd"], s["day"]["check_every_min"], s["day"]["max_entries_per_day"]), (900, 7, 6))
        with self.assertRaises(ValueError):
            D.save_settings({"mode": "live"})
        os.environ["ALPACA_LIVE_KEY_ID"] = "AK1"
        self.assertEqual(D.save_settings({"mode": "live"})["mode"], "live")


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.server = D.make_server(self.app, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, path, body=None, headers=None, host=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Trader-HQ": "1"})
        h.update(headers or {})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"null") if "json" in resp.headers["Content-Type"] else resp.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_page_and_state(self):
        code, page = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Trader HQ", page)
        code, s = self.req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(s["account"]["equity"], 10000.0)
        self.assertEqual(s["account"]["options_paid"], 100.0)
        self.assertEqual([p["kind"] for p in s["positions"]], ["stock", "option"])
        self.assertEqual(s["positions"][1]["name"], "AAPL $200 CALL expiring 2026-12-18")
        self.assertEqual(s["orders"][0]["id"], "o9")

    def test_equity_skips_gaps(self):
        code, e = self.req("/api/equity?period=1M")
        self.assertEqual(e["points"], [{"t": 1, "v": 9800}, {"t": 3, "v": 10000}])

    def test_post_needs_custom_header(self):
        code, _ = self.req("/api/day/stop", {}, headers={"X-Trader-HQ": "0"})
        self.assertEqual(code, 403)

    def test_wrong_host_refused(self):
        self.assertEqual(self.req("/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("/api/day/stop", {}, host="evil.example:80")[0], 403)

    def test_decide_over_http(self):
        self.app.add_plan(FakePlan(self.broker, T.load_settings(), [trade()]), ttl=60)
        pid = self.app.proposals[0]["id"]
        code, p = self.req("/api/decide", {"id": pid, "approve": True})
        self.assertEqual((code, p["status"]), (200, "placed"))
        self.assertNotIn("order", p)

    def test_live_mode_gate(self):
        os.environ["ALPACA_LIVE_KEY_ID"] = "AK1"
        D.save_settings({"mode": "live"})
        code, s = self.req("/api/state")
        self.assertEqual((s["mode"], s["live_ok"]), ("live", False))
        self.assertNotIn("account", s)
        self.assertEqual(self.req("/api/confirm-live", {"text": "live"})[0], 400)
        self.assertEqual(self.req("/api/confirm-live", {"text": "LIVE"})[0], 200)
        self.assertIn("account", self.req("/api/state")[1])

    def test_settings_roundtrip_and_errors(self):
        code, _ = self.req("/api/settings", {"settings": {"max_order_usd": "750"}})
        self.assertEqual(code, 200)
        self.assertEqual(self.req("/api/settings")[1]["settings"]["max_order_usd"], 750)
        code, err = self.req("/api/settings", {"settings": {"max_order_usd": "x"}})
        self.assertEqual(code, 400)
        self.assertIn("number", err["error"])

    def test_restart_waits_for_running_bots(self):
        self.app.day_info = lambda: {"running": True}
        code, err = self.req("/api/restart", {})
        self.assertEqual(code, 400)
        self.assertIn("Stop day trading", err["error"])
        self.assertFalse(self.app.stopping.is_set())

    def test_favicon(self):
        h = {"Host": f"127.0.0.1:{self.port}"}
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.port}/assets/favicon.png", headers=h)) as r:
            self.assertEqual(r.headers["Content-Type"], "image/png")

    def test_cancel_order(self):
        self.assertEqual(self.req("/api/cancel-order", {"id": "o9"})[0], 200)
        self.assertEqual(self.broker.cancelled, ["o9"])

    def test_plan_runs_in_background(self):
        with mock.patch.object(T, "LongTermPlan", side_effect=lambda b, s, progress: FakePlan(b, s, [trade()])):
            self.assertEqual(self.req("/api/plan", {"kind": "run"})[0], 200)
            for _ in range(50):
                if self.app.plans:
                    break
                time.sleep(0.05)
        self.assertEqual(self.app.plans[0]["outlook"], "Looks calm.")
        self.assertEqual(self.app.proposals[0]["status"], "pending")

    def test_missing_keys_shows_setup(self):
        del os.environ["ALPACA_PAPER_KEY_ID"]
        code, s = self.req("/api/state")
        self.assertEqual(s["has_keys"]["paper"], False)
        self.assertNotIn("account", s)


class InstallAppTests(unittest.TestCase):
    def test_creates_double_clickable_app(self):
        desk = Path(tempfile.mkdtemp())
        app = D.install_app(desktop=desk, python="/Users/someone/My Folder/.venv/bin/python")
        launcher = app / "Contents" / "MacOS" / "TraderHQ"
        self.assertTrue(os.access(launcher, os.X_OK))
        text = launcher.read_text()
        self.assertIn("PY='/Users/someone/My Folder/.venv/bin/python'", text)
        self.assertIn("trader.py app --no-open", text)
        self.assertIn("<string>TraderHQ</string>", (app / "Contents" / "Info.plist").read_text())
        self.assertIn("couldn", text)  # explains a failed start instead of opening a dead page

    def test_icon_and_self_refresh(self):
        desk = Path(tempfile.mkdtemp())
        app = D.install_app(desktop=desk, quiet=True)
        self.assertTrue((app / "Contents" / "Resources" / "TraderHQ.icns").exists())
        self.assertIn("<key>CFBundleIconFile</key>", (app / "Contents" / "Info.plist").read_text())
        self.assertFalse(D.refresh_launcher(desktop=desk))  # already current
        launcher = app / "Contents" / "MacOS" / "TraderHQ"
        launcher.write_text("#!/bin/bash\n# an older launcher\n")
        self.assertTrue(D.refresh_launcher(desktop=desk))
        self.assertIn("trader.py app --no-open", launcher.read_text())
        other = Path(tempfile.mkdtemp())
        (other / "Trader HQ.app" / "Contents").mkdir(parents=True)
        (other / "Trader HQ.app" / "Contents" / "Info.plist").write_text("<plist>someone else's app</plist>")
        self.assertFalse(D.refresh_launcher(desktop=other))  # never touches an app we didn't make
        self.assertFalse(D.refresh_launcher(desktop=Path(tempfile.mkdtemp())))  # no icon: nothing to do

    def test_refuses_folders_macos_protects(self):
        home = Path(tempfile.mkdtemp())
        for name in ("Desktop", "Documents", "Downloads"):
            folder = home / name / "Trader-HQ"
            folder.mkdir(parents=True)
            with self.assertRaises(RuntimeError) as e:
                D.install_app(desktop=home, here=folder, home=home)
            self.assertIn("mv ", str(e.exception))
        ok = home / "Trader-HQ"
        ok.mkdir()
        self.assertTrue(D.install_app(desktop=home, here=ok, home=home).exists())


if __name__ == "__main__":
    unittest.main()
