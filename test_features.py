"""Run with: python -m unittest test_features.py  (no network, no keys needed)"""
import datetime as dt
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import desktop as D
import features as F
import market as M
import test_market as TM
import trader as T

D0 = dt.date(2026, 11, 2)  # a Monday


def order(oid, sym, side, qty, price, when, cid="", asset_class="us_equity", legs=None):
    return {"id": oid, "symbol": sym, "side": side, "filled_qty": str(qty), "filled_avg_price": str(price),
            "filled_at": when, "submitted_at": when, "client_order_id": cid, "asset_class": asset_class,
            "status": "filled", "legs": legs or []}


class Broker(TM.FakeBroker):
    def __init__(self, mode="paper"):
        super().__init__()
        self.mode = mode
        self.open = []
        self.closed = []
        self.cancelled = []
        self.is_open = True
        self.pos = [{"symbol": "AAPL", "qty": "10", "asset_class": "us_equity", "side": "long", "avg_entry_price": "190",
                     "current_price": "210", "market_value": "2100", "unrealized_pl": "200", "unrealized_plpc": "0.1",
                     "cost_basis": "1900", "change_today": "0.02"},
                    {"symbol": "BTCUSD", "qty": "0.01", "asset_class": "crypto", "side": "long", "avg_entry_price": "50000",
                     "current_price": "60000", "market_value": "600", "unrealized_pl": "100", "unrealized_plpc": "0.2",
                     "cost_basis": "500", "change_today": "-0.01"}]

    def _api(self, path, params=None, body=None, method="GET"):
        if path == "/v2/orders" and method == "POST":
            self.orders.append(body)
            return {"id": f"new{len(self.orders)}", "status": "accepted"}
        if path == "/v2/orders" and (params or {}).get("status") == "closed":
            return list(self.closed)
        if path.startswith("/v2/orders/") and method == "DELETE":
            self.cancelled.append(path.rsplit("/", 1)[1])
            self.open = [o for o in self.open if o["id"] != path.rsplit("/", 1)[1]]
            return None
        return super()._api(path, params, body, method)

    def positions(self):
        return self.pos

    def stock_positions(self):
        return [p for p in self.pos if p.get("asset_class", "us_equity") == "us_equity"]

    def open_orders(self):
        return list(self.open)

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.open = [o for o in self.open if o["id"] != oid]

    def get_order(self, oid):
        if oid == "buy1":
            return {"status": "filled", "filled_qty": "0.004", "filled_avg_price": "60000"}
        return {"status": "canceled"}

    def clock(self):
        return {"is_open": self.is_open, "timestamp": "2026-11-02T15:00:00Z", "next_close": "2026-11-02T21:00:00Z"}

    def portfolio_history(self, period, timeframe):
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        return {"timestamp": [now - (60 - i * 10) * 86400 for i in range(5)], "equity": [10000, 10100, 9900, 10200, 10300]}


class Base(TM.Base):
    def setUp(self):
        super().setUp()
        self.b = Broker()
        self.mk = M.Market(lambda: self.b, http=TM.fake_http)
        self.mk.ensure_assets()
        self.store = F.Store(self.mk.db)


class ScheduleTests(unittest.TestCase):
    def test_next_run(self):
        self.assertEqual(F.next_run("weekly", 0, D0), D0)                       # Monday -> today
        self.assertEqual(F.next_run("weekly", 4, D0), dt.date(2026, 11, 6))     # Friday
        self.assertEqual(F.next_run("weekly", 0, dt.date(2026, 11, 3)), dt.date(2026, 11, 9))
        self.assertEqual(F.next_run("monthly", 15, D0), dt.date(2026, 11, 15))
        self.assertEqual(F.next_run("monthly", 1, D0), dt.date(2026, 12, 1))
        self.assertEqual(F.next_run("monthly", 1, dt.date(2026, 12, 2)), dt.date(2027, 1, 1))
        self.assertEqual(F.describe_schedule("biweekly", 2), "Every other Wednesday")

    def test_strategy_names(self):
        self.assertEqual(F.strategy_of("amhd-1"), "Day trading")
        self.assertEqual(F.strategy_of("amh-20261102"), "Long-term bot")
        self.assertEqual(F.strategy_of("amhr-x"), "Recurring buys")
        self.assertEqual(F.strategy_of("web-123"), "Outside Trader HQ")


class StoreTests(Base):
    def test_watchlists(self):
        lists = self.store.watchlists()
        self.assertEqual(lists[0]["name"], "My watchlist")
        lid = lists[0]["id"]
        self.store.set_watch(lid, "AAPL", True)
        self.store.set_watch(lid, "AAPL", True)  # no duplicates
        self.store.set_watch(lid, "BTC/USD", True)
        self.assertEqual(self.store.watchlists()[0]["symbols"], ["AAPL", "BTC/USD"])
        with self.assertRaises(ValueError):
            self.store.delete_list(lid)  # keep at least one
        other = self.store.create_list("Tech")
        with self.assertRaises(ValueError):
            self.store.create_list("Tech")
        self.store.delete_list(other)
        self.store.set_watch(lid, "AAPL", False)
        self.assertEqual(self.store.watched_symbols(), ["BTC/USD"])

    def test_alerts_trigger_once(self):
        self.store.add_alert("AAPL", "above", 205, "breakout")
        self.store.add_alert("AAPL", "below", 150)
        self.store.add_alert("TINY", "move", 8)
        with self.assertRaises(ValueError):
            self.store.add_alert("AAPL", "sideways", 1)
        fired = self.store.check_alerts(self.mk.quotes)
        self.assertEqual(sorted((a["symbol"], a["kind"]) for a in fired), [("AAPL", "above"), ("TINY", "move")])
        self.assertEqual(self.store.check_alerts(self.mk.quotes), [])
        notes = self.store.notifications()
        self.assertIn("rose to $210.00", notes[-1]["body"] + notes[0]["body"])
        self.assertEqual(sum(a["active"] for a in self.store.alerts()), 1)

    def test_notifications_on_mac_use_osascript(self):
        with mock.patch.object(F.sys, "platform", "darwin"), mock.patch.object(F.subprocess, "Popen") as popen:
            self.store.notify("Title", 'He said "hi"')
        args = popen.call_args[0][0]
        self.assertEqual(args[:2], ["osascript", "-e"])
        self.assertIn('\\"hi\\"', args[2])


class RecurringTests(Base):
    def setUp(self):
        super().setUp()
        self.s = T.load_settings()

    def test_fractional_and_whole_shares(self):
        self.store.add_recurring("AAPL", 50, "weekly", 0, "paper", today=D0)
        self.store.add_recurring("TINY", 20, "weekly", 0, "paper", today=D0)  # $5.50, not fractionable -> 3 shares
        self.store.add_recurring("TINY", 3, "weekly", 0, "paper", today=D0)   # too small for one share
        res = F.run_recurring(self.store, self.b, self.mk, self.s, False, today=D0)
        self.assertEqual(len(res), 3)
        self.assertEqual(self.b.orders[0]["notional"], "50.00")
        self.assertEqual(self.b.orders[1]["qty"], "3")
        self.assertEqual(len(self.b.orders), 2)
        self.assertIn("isn't enough", res[2])
        nxt = {r["symbol"] + str(r["amount"]): r["next_run"] for r in self.store.recurring()}
        self.assertEqual(nxt["AAPL50.0"], "2026-11-09")
        self.assertEqual(F.run_recurring(self.store, self.b, self.mk, self.s, False, today=D0), [])  # not due again

    def test_biweekly_and_limits(self):
        self.store.add_recurring("AAPL", 5000, "biweekly", 0, "paper", today=D0)
        res = F.run_recurring(self.store, self.b, self.mk, self.s, False, today=D0)
        self.assertIn("over your", res[0])
        self.assertEqual(self.store.recurring()[0]["next_run"], "2026-11-16")

    def test_waits_for_market_and_live_confirmation(self):
        self.store.add_recurring("AAPL", 50, "weekly", 0, "paper", today=D0)
        self.store.add_recurring("BTC/USD", 25, "weekly", 0, "paper", today=D0)
        self.b.is_open = False
        F.run_recurring(self.store, self.b, self.mk, self.s, False, today=D0)
        self.assertEqual([o["symbol"] for o in self.b.orders], ["BTC/USD"])  # crypto trades 24/7
        live = Broker(mode="live")
        self.store.add_recurring("AAPL", 50, "weekly", 0, "live", today=D0)
        self.assertEqual(F.run_recurring(self.store, live, self.mk, self.s, False, today=D0), [])
        self.assertEqual(len(F.run_recurring(self.store, live, self.mk, self.s, True, today=D0)), 1)

    def test_validation(self):
        for args in (("AAPL", 0, "weekly", 0), ("AAPL", 10, "daily", 0), ("AAPL", 10, "monthly", 31), ("AAPL", 10, "weekly", 6)):
            with self.assertRaises(ValueError):
                self.store.add_recurring(*args, "paper", today=D0)


class ProtectionTests(Base):
    def test_places_trailing_stop_once(self):
        self.assertEqual(F.protect_positions(self.b, 5), ["AAPL"])
        o = self.b.orders[0]
        self.assertEqual((o["type"], o["qty"], o["trail_percent"], o["time_in_force"]), ("trailing_stop", "10", "5", "gtc"))
        self.b.open = [{"id": "t1", "symbol": "AAPL", "side": "sell", "qty": "10", "trail_percent": "5", "client_order_id": o["client_order_id"]}]
        self.assertEqual(F.protect_positions(self.b, 5), [])  # already protected
        self.b.pos[0]["qty"] = "12"
        self.assertEqual(F.protect_positions(self.b, 5), ["AAPL"])  # resized
        self.assertEqual(self.b.cancelled, ["t1"])
        self.assertEqual(F.protect_positions(self.b, 0), [])

    def test_skips_positions_with_other_sell_orders(self):
        self.b.open = [{"id": "oco1", "symbol": "AAPL", "side": "sell", "qty": "10", "client_order_id": "amhm-x"}]
        self.assertEqual(F.protect_positions(self.b, 5), [])

    def test_cancel_protection_before_selling(self):
        self.b.open = [{"id": "t1", "symbol": "AAPL", "side": "sell", "client_order_id": "amht-1"},
                       {"id": "c1", "symbol": "BTC/USD", "side": "sell", "client_order_id": "amhc-s-1"},
                       {"id": "m1", "symbol": "AAPL", "side": "sell", "client_order_id": "amhm-1"}]
        self.assertEqual(T.cancel_protection(self.b, "AAPL"), ["t1"])
        self.assertEqual(T.cancel_protection(self.b, "BTCUSD"), ["c1"])


class ReportTests(Base):
    def setUp(self):
        super().setUp()
        self.b.closed = [
            order("s2", "AAPL", "sell", 5, 120, "2026-03-01T15:00:00Z", "amh-2"),
            order("b2", "AAPL", "buy", 5, 110, "2025-01-02T15:00:00Z", "amh-1"),
            order("b1", "AAPL", "buy", 5, 100, "2025-01-01T15:00:00Z", "amh-0",
                  legs=[{"id": "l1", "side": "sell", "filled_qty": "5", "filled_avg_price": "90", "filled_at": "2026-02-01T15:00:00Z", "client_order_id": "x"}]),
            order("o1", "AAPL261218C00200000", "buy", 1, 2.0, "2026-04-01T15:00:00Z", "amho-1", "us_option"),
            order("o2", "AAPL261218C00200000", "sell", 1, 3.5, "2026-04-05T15:00:00Z", "amho-2", "us_option"),
            order("d1", "TINY", "buy", 10, 5, "2026-11-02T15:00:00Z", "amhd-1"),
            order("d2", "TINY", "sell", 10, 4.5, "2026-11-02T19:00:00Z", "amhd-2"),
        ]

    def test_fifo_matching(self):
        fills = F.fetch_fills(self.b)
        self.assertEqual(len(fills), 8)
        trades, open_lots = F.match_trades(fills)
        aapl = [t for t in trades if t["symbol"] == "AAPL"]
        self.assertEqual([(t["buy_price"], t["sell_price"]) for t in aapl], [(100, 90), (110, 120)])  # FIFO
        self.assertEqual([t["strategy"] for t in aapl], ["Long-term bot", "Long-term bot"])
        opt = [t for t in trades if t["asset_class"] == "us_option"][0]
        self.assertAlmostEqual(opt["pnl"], 150)  # options x100
        self.assertEqual(open_lots, [])
        s = F.trade_stats(trades)
        self.assertEqual((s["trades"], round(s["win_rate"])), (4, 50))
        self.assertAlmostEqual(s["total_pnl"], -50 + 50 + 150 - 5)
        self.assertEqual(F.max_drawdown([100, 120, 90, 130]), -25)

    def test_tax_report(self):
        r = F.tax_report(self.b, 2026)
        terms = sorted((t["symbol"], t["term"]) for t in r["rows"])
        self.assertIn(("AAPL", "Long-term"), terms)  # held over a year
        self.assertIn(("TINY", "Short-term"), terms)
        self.assertAlmostEqual(r["totals"]["Short-term"]["gain"], 150 - 5)   # option and TINY
        self.assertAlmostEqual(r["totals"]["Long-term"]["gain"], -50 + 50)   # both AAPL lots were held over a year
        csv_text = F.tax_csv(r)
        self.assertTrue(csv_text.startswith("Symbol,Quantity,Date acquired"))
        self.assertEqual(F.tax_report(self.b, 2024)["rows"], [])

    def test_performance_vs_spy(self):
        r = F.performance(self.b, self.mk, "ALL")
        self.assertAlmostEqual(r["account_return"], 3.0)
        self.assertAlmostEqual(r["max_drawdown"], (9900 / 10100 - 1) * 100)
        self.assertIsNotNone(r["spy_return"])
        self.assertEqual(r["overall"]["trades"], 4)
        names = {s["strategy"] for s in r["by_strategy"]}
        self.assertEqual(names, {"Long-term bot", "Options", "Day trading"})

    def test_daily_summary(self):
        with mock.patch.object(F, "today_et", return_value=D0):
            s = F.daily_summary(self.b, D0)
        self.assertEqual(len(s["fills"]), 2)
        self.assertAlmostEqual(s["realized_pnl"], -5)
        self.assertIn("Account $10,000.00", s["text"])


def events_http(url, headers=None, timeout=20):
    if "calendar/earnings" in url:
        return {"earningsCalendar": [{"date": (D0 + dt.timedelta(days=3)).isoformat(), "hour": "amc", "epsEstimate": 1.5, "symbol": "AAPL"}]}
    return TM.fake_http(url, headers, timeout)


class EventTests(Base):
    def setUp(self):
        super().setUp()
        orig = self.b._req

        def req(base, path, params=None, body=None, method="GET"):
            if path == "/v1/corporate-actions":
                return {"corporate_actions": {"cash_dividends": [
                    {"symbol": "AAPL", "rate": 0.26, "ex_date": "2026-08-10", "payable_date": "2026-08-14"},
                    {"symbol": "AAPL", "rate": 0.26, "ex_date": "2026-11-09", "payable_date": "2026-11-13"}]}}
            return orig(base, path, params, body, method)
        self.b._req = req
        self.mk.http = events_http

    def test_dividends_and_earnings(self):
        with mock.patch.object(F, "today_et", return_value=D0):
            self.assertEqual([d["ex_date"] for d in F.dividends(self.mk, "AAPL")], ["2026-08-10", "2026-11-09"])
            self.assertIsNone(F.earnings(self.mk, "AAPL"))  # no Finnhub key
            os.environ["FINNHUB_API_KEY"] = "abc123abc123"
            self.assertEqual(F.earnings(self.mk, "AAPL")[0]["hour"], "amc")
            self.assertEqual(F.earnings_soon(self.mk, "AAPL"), "2026-11-05")
            cal = F.calendar(self.mk, ["AAPL", "BTC/USD"])
        self.assertEqual(cal[0]["symbol"], "AAPL")
        self.assertEqual((cal[0]["earnings"], cal[0]["ex_dividend"]), ("2026-11-05", "2026-11-09"))


class ExplainTests(Base):
    def test_explain_wraps_news_and_caches(self):
        info = self.mk.info("AAPL")
        with mock.patch.object(T, "ask_text", return_value="**What it is** Apple makes phones.") as ask:
            r = F.explain(self.mk, info)
            F.explain(self.mk, info)
        self.assertEqual(ask.call_count, 1)
        prompt, system = ask.call_args[0]
        self.assertIn("<news>\n- Apple does a thing", prompt)
        self.assertIn("untrusted", system)
        self.assertIn("Apple makes phones", r["text"])


class CryptoTests(Base):
    def plan(self, trades):
        raw = {"outlook": "calm", "trades": [], "raw_trades": trades}
        with mock.patch.object(T, "ask_claude", return_value=raw):
            return F.CryptoPlan(self.b, T.load_settings(), self.mk, self.store, progress=lambda *_: None)

    def trade(self, **kw):
        return {"symbol": "BTC/USD", "side": "buy", "amount_usd": 100, "sell_fraction": 0, "stop_loss_pct": 5, "reason": "r", "risk": "k", **kw}

    def test_buy_then_stop_after_fill(self):
        p = self.plan([self.trade()])
        ok, why, o = p.check(p.trades[0])
        self.assertTrue(ok, why)
        self.assertAlmostEqual(o["stop_price"], 57000)
        order = p.place(p.trades[0], o, 1)
        self.assertEqual(self.b.orders[0]["notional"], "100.00")
        self.store.x("UPDATE crypto_entries SET order_id='buy1' WHERE order_id=?", (order["id"],))
        self.assertEqual(F.protect_crypto_fills(self.b, self.store), ["BTC/USD"])
        stop = self.b.orders[-1]
        self.assertEqual((stop["type"], stop["side"], stop["qty"]), ("stop_limit", "sell", "0.004"))
        self.assertAlmostEqual(float(stop["stop_price"]), 57000)
        self.assertEqual(F.protect_crypto_fills(self.b, self.store), [])  # only once

    def test_checks(self):
        p = self.plan([])
        for bad, word in ((self.trade(symbol="DOGE/USD"), "watchlist"), (self.trade(amount_usd=500), "limit"),
                          (self.trade(stop_loss_pct=20), "stop-loss"), (self.trade(amount_usd=200, symbol="ETH/USD"), "")):
            ok, why, _ = p.check(F.parse_crypto_trade(bad))
            if word:
                self.assertFalse(ok)
                self.assertIn(word, why)
        ok, _, o = p.check(F.parse_crypto_trade(self.trade(side="sell", sell_fraction=0.5)))
        self.assertTrue(ok)
        self.assertAlmostEqual(o["qty"], 0.005)
        self.assertFalse(p.check(F.parse_crypto_trade(self.trade(symbol="ETH/USD", side="sell", sell_fraction=1)))[0])

    def test_position_cap(self):
        self.b.pos[1]["market_value"] = "950"  # 9.5% of $10k already in BTC
        p = self.plan([])
        ok, why, _ = p.check(F.parse_crypto_trade(self.trade(amount_usd=100)))
        self.assertFalse(ok)
        self.assertIn("10%", why)


class AppTests(Base):
    def setUp(self):
        super().setUp()
        self.app = D.App(broker_factory=lambda mode: self.b)
        self.app.market = self.mk
        self.app._store = self.store
        self.server = D.make_server(self.app, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, path, body=None):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Trader-HQ": "1"})
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                data = resp.read()
                return resp.status, (json.loads(data) if "json" in resp.headers["Content-Type"] else data), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), e.headers

    def test_static_and_watchlists(self):
        self.assertEqual(self.req("/features.js")[0], 200)
        lid = self.req("/api/watchlists")[1]["lists"][0]["id"]
        self.assertEqual(self.req("/api/watchlists", {"action": "add", "id": lid, "symbol": "aapl"})[0], 200)
        self.assertEqual(self.req("/api/watchlists", {"action": "add", "id": lid, "symbol": "NOPE"})[0], 400)
        self.req("/api/watchlists", {"action": "add", "id": lid, "symbol": "BTC/USD"})
        data = self.req("/api/watchlists")[1]
        self.assertEqual(data["lists"][0]["symbols"], ["AAPL", "BTC/USD"])
        self.assertIn("AAPL", data["quotes"])
        self.assertEqual(self.req("/api/watchlists", {"action": "use", "id": lid, "target": "crypto"})[0], 200)
        self.assertEqual(T.load_settings()["crypto"]["watchlist"], ["BTC/USD"])
        self.req("/api/watchlists", {"action": "use", "id": lid, "target": "long_term"})
        self.assertEqual(T.load_settings()["watchlist"], ["AAPL"])
        self.assertEqual(self.req("/api/market/scan", {"kind": "watchlist", "filters": {}})[1]["scanned"], 2)

    def test_alerts_and_recurring_endpoints(self):
        self.assertEqual(self.req("/api/alerts", {"symbol": "AAPL", "kind": "below", "value": 100})[0], 200)
        self.assertEqual(len(self.req("/api/alerts")[1]["rows"]), 1)
        self.assertEqual(self.req("/api/recurring", {"action": "add", "symbol": "AAPL", "amount": 25, "frequency": "monthly", "day": 3})[0], 200)
        row = self.req("/api/recurring")[1]["rows"][0]
        self.assertEqual((row["schedule"], row["mode"]), ("Monthly on day 3", "paper"))
        self.req("/api/recurring", {"action": "pause", "id": row["id"]})
        self.assertFalse(self.req("/api/recurring")[1]["rows"][0]["active"])

    def test_recurring_in_live_needs_live(self):
        os.environ["ALPACA_LIVE_KEY_ID"] = "AK1"
        D.save_settings({"mode": "live"})
        code, err, _ = self.req("/api/recurring", {"action": "add", "symbol": "AAPL", "amount": 25, "frequency": "weekly", "day": 1})
        self.assertEqual((code, err.get("need_live")), (409, True))

    def test_reports_endpoints(self):
        self.b.closed = [order("b", "AAPL", "buy", 1, 100, "2026-01-02T15:00:00Z", "amh-1"),
                         order("s", "AAPL", "sell", 1, 110, "2026-02-02T15:00:00Z", "amh-2")]
        self.assertEqual(self.req("/api/reports/performance?period=ALL")[1]["overall"]["trades"], 1)
        code, body, headers = self.req("/api/reports/tax?year=2026&format=csv")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"AAPL,1,2026-01-02,2026-02-02,110.00,100.00,10.00,Short-term", body)
        s = self.req("/api/reports/summary-now", {})[1]
        self.assertIn("text", s)
        self.assertEqual(len(self.req("/api/reports/summaries")[1]["rows"]), 1)

    def test_explain_endpoint(self):
        with mock.patch.object(T, "ask_text", return_value="Explained."):
            self.assertEqual(self.req("/api/market/explain", {"symbol": "AAPL"})[1]["text"], "Explained.")

    def test_tick_runs_jobs_and_notifies(self):
        self.store.add_alert("AAPL", "above", 1)
        self.store.add_recurring("AAPL", 10, "weekly", F.today_et().weekday() if F.today_et().weekday() < 5 else 0, "paper",
                                 today=F.today_et())
        D.save_settings({"market": {"auto_trailing_stop_pct": 4, "mac_notifications": False}})
        self.app.tick()
        types = [o.get("type") for o in self.b.orders]
        self.assertIn("trailing_stop", types)
        titles = [n["title"] for n in self.store.notifications()]
        self.assertIn("AAPL alert", titles)
        state = self.req("/api/state")[1]
        self.assertTrue(state["notifications"])

    def test_tick_skips_trading_in_unconfirmed_live(self):
        os.environ["ALPACA_LIVE_KEY_ID"] = "AK1"
        D.save_settings({"mode": "live", "market": {"auto_trailing_stop_pct": 4}})
        self.app.tick()
        self.assertEqual(self.b.orders, [])

    def test_earnings_warning_on_buy_suggestions(self):
        class P(T.Plan):
            kind, label = "run", "Long-term"

            def check(self, t):
                return True, "", {"qty": 1, "limit_price": 1, "cost": 1}

            def question(self, t, o):
                return "?"
        plan = P(self.b, T.load_settings())
        plan.trades = [{"symbol": "AAPL", "side": "buy", "qty": 1, "reason": "", "risk": ""}]
        with mock.patch.object(F, "earnings_soon", return_value="2026-11-05"):
            self.app.add_plan(plan, 60)
        self.assertIn("Earnings report on 2026-11-05", self.app.proposals[0]["warning"])


if __name__ == "__main__":
    unittest.main()
