"""Tests for fast mode: setup checks, the trigger engine and its safety rules. No network or keys."""
import datetime as dt
import time
import unittest
from unittest import mock

import fast as FT
import trader as T
import test_features as TF

NOW = time.time()


def bar(minute, o, h, l, c, v=1000, hour=9):
    t = dt.datetime(2026, 11, 5, hour, minute, tzinfo=T.ET).astimezone(dt.timezone.utc)
    return {"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": h, "l": l, "c": c, "v": v, "vw": (h + l + c) / 3}


# 9:30-9:39: opening range 99.5-101 in the first 5 minutes, then drifting up toward 100.9
BARS = [bar(30 + i, 100, 101 if i == 2 else 100.6, 99.5 if i == 1 else 99.9, 100.4) for i in range(5)] + \
       [bar(35 + i, 100.5, 100.95, 100.3, 100.8, 1500) for i in range(5)]


def quote(price, bid=None, ask=None, age=1):
    t = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"latestTrade": {"p": price, "t": t}, "latestQuote": {"bp": bid or price - 0.01, "ap": ask or price + 0.01}}


class FastBroker(TF.Broker):
    def __init__(self):
        super().__init__()
        self.snaps = {"AAPL": quote(100.8)}
        self.order_book = {}
        self.sells = []

    def _req(self, base, path, params=None, body=None, method="GET"):
        if path == "/v2/stocks/snapshots":
            return {s: self.snaps[s] for s in params["symbols"].split(",") if s in self.snaps}
        return super()._req(base, path, params, body, method)

    def bars(self, symbols, timeframe, start, feed="iex"):
        return {s: list(BARS) for s in symbols}

    def daily_bars(self, symbols, days=60):
        return {s: [{"t": "2026-10-01T04:00:00Z", "o": 99, "h": 101, "l": 98, "c": 100, "v": 1e6}] * 20 for s in symbols}

    def place_bracket_buy(self, symbol, qty, limit_price, take_profit, stop_price, client_order_id):
        oid = f"e{len(self.orders) + 1}"
        self.orders.append({"symbol": symbol, "qty": qty, "limit": limit_price, "tp": take_profit, "sl": stop_price, "cid": client_order_id})
        self.order_book[oid] = {"id": oid, "status": "new", "filled_qty": "0",
                                "legs": [{"id": oid + "tp", "type": "limit", "status": "held"}, {"id": oid + "sl", "type": "stop", "status": "held"}]}
        return dict(self.order_book[oid])

    def get_order(self, oid):
        return self.order_book.get(oid, {"status": "canceled"})

    def cancel_order(self, oid):
        super().cancel_order(oid)
        for o in self.order_book.values():
            for x in [o] + o["legs"]:
                if x["id"] == oid and x["status"] not in ("filled",):
                    x["status"] = "canceled"

    def place_market_sell(self, symbol, qty, client_order_id):
        self.sells.append((symbol, qty, client_order_id))
        return {"id": "x1"}


def settings(**day):
    s = T.load_settings()
    s["day"].update({"watchlist": ["AAPL", "MSFT"], "max_risk_per_trade_usd": 25, "max_stop_loss_pct": 2, "max_take_profit_pct": 6,
                     "max_entries_per_day": 6, "no_new_entries_min_before_close": 30, "fast_mode": True, **day})
    s["max_order_usd"] = 5000
    s["max_position_pct"] = 100
    return s


def setup(**kw):
    return {"symbol": "AAPL", "setup": "orb", "side": "buy", "trigger_price": 101.05, "stop_price": 100.3, "target_price": 102.4,
            "valid_minutes": 15, "reason": "r", "risk": "k", "qty": 0, **kw}


class LevelTests(unittest.TestCase):
    def test_levels(self):
        lv = FT.levels(BARS)
        self.assertEqual((lv["opening_range_high"], lv["opening_range_low"]), (101, 99.5))
        self.assertEqual(lv["last"], 100.8)
        self.assertEqual(lv["minutes_of_data"], 10)
        self.assertGreater(lv["last_5min_volume_vs_session_avg"], 1)
        self.assertIsNone(FT.levels(BARS[:3])["opening_range_high"])  # first 5 minutes not done yet
        self.assertIsNone(FT.levels([]))

    def test_spread(self):
        self.assertAlmostEqual(FT.spread_pct({"bp": 99.95, "ap": 100.05}), 0.1)
        self.assertIsNone(FT.spread_pct({"bp": 0, "ap": 100}))


class Base(TF.Base):
    def setUp(self):
        super().setUp()
        self.b = FastBroker()
        self.s = settings()
        self.state = {"baseline": {}, "entries": [], "flattened": False}
        self.logs, self.notes = [], []
        self.eng = FT.Engine(log=self.logs.append, notify=lambda *a: self.notes.append(a))

    def snap(self, minutes_left=200):
        return FT.fast_snapshot(self.b, self.s, self.state, minutes_left)

    def arm(self, **kw):
        ok, why, o = FT.check_setup(setup(**kw), self.snap(), self.s)
        self.assertTrue(ok, why)
        self.eng.arm(setup(**kw), o)
        return o

    def tick(self, now=None, gate=lambda: ""):
        self.eng.tick(self.b, self.s, self.state, gate, now=now or time.time())


class CheckTests(Base):
    def test_good_setup_and_sizing(self):
        ok, why, o = FT.check_setup(setup(), self.snap(), self.s)
        self.assertTrue(ok, why)
        self.assertEqual(o["qty"], 33)                       # $25 risk / $0.75 per share
        self.assertAlmostEqual(o["risk"], 33 * 0.75)
        self.assertEqual(o["minutes"], 15)

    def test_sized_to_fit_auto_approve(self):
        ok, why, o = FT.check_setup(setup(), self.snap(), self.s, cap=325)
        self.assertTrue(ok, why)
        self.assertEqual(o["qty"], 3)                         # 3 x ~$101 fits under $325 even after the chase allowance
        self.assertLessEqual(T.trade_value("day", setup(), o), 325)

    def test_rejections(self):
        snap = self.snap()
        cases = [(setup(symbol="TSLA"), "watchlist"), (setup(stop_price=101.2), "stop must be below"),
                 (setup(stop_price=98.5), "stop-loss must be"), (setup(target_price=101.5), "1.5x"),
                 (setup(target_price=110), "more than 6%"), (setup(trigger_price=100.85, stop_price=100.4, target_price=101.6), "range high"),
                 (setup(trigger_price=104.5, stop_price=104, target_price=106), "more than 3% above"),
                 (setup(setup="vwap_pullback", trigger_price=100.2, stop_price=99.8, target_price=101), "already above"),
                 (setup(setup="nope"), "turned off")]
        for t, word in cases:
            ok, why, _ = FT.check_setup(t, snap, self.s)
            self.assertFalse(ok, t)
            self.assertIn(word, why)
        self.s["day"]["fast_orb"] = False
        self.assertIn("turned off", FT.check_setup(setup(), snap, self.s)[1])
        self.s["day"]["fast_orb"] = True
        self.assertIn("close", FT.check_setup(setup(), self.snap(minutes_left=20), self.s)[1])
        self.b.order_book.clear()
        with mock.patch.object(FT, "levels", return_value={**FT.levels(BARS), "opening_range_high": None}):
            self.assertIn("opening range isn't complete", FT.check_setup(setup(), self.snap(), self.s)[1])

    def test_plan_from_claude(self):
        answer = {"outlook": "AAPL strong", "setups": [setup(), {"symbol": "MSFT"}]}
        with mock.patch.object(T, "ask_claude", return_value=answer) as ask:
            p = FT.FastPlan(self.b, self.s, self.state, 200, self.eng, progress=lambda *_: None)
        self.assertEqual(len(p.trades), 1)  # the malformed one is dropped
        self.assertEqual(ask.call_args[1]["kind"], "day")
        self.assertTrue(ask.call_args[1]["raw"])
        ok, _, o = p.check(p.trades[0])
        self.assertIn("ARM Opening range breakout: buy 33 AAPL", p.headline(p.trades[0], o))
        self.assertEqual(p.place(p.trades[0], o, 1)["status"], "armed")
        self.assertEqual(T.trade_value("day", p.trades[0], o), 33 * 101.05)
        with mock.patch.object(T, "ask_claude", return_value=answer):
            again = FT.FastPlan(self.b, self.s, self.state, 200, self.eng, progress=lambda *_: None)
        self.assertEqual(again.trades, [])  # already armed: not repeated


class EngineTests(Base):
    def test_trigger_places_bracket_then_target(self):
        self.arm()
        self.tick()
        self.assertEqual(self.b.orders, [])                          # 100.80 < trigger 101.05
        self.b.snaps["AAPL"] = quote(101.10, 101.09, 101.11)
        self.tick()
        o = self.b.orders[0]
        self.assertEqual((o["qty"], o["sl"], o["tp"]), (29, 100.3, 102.4))  # re-sized: risk checked at the real entry price
        self.assertEqual(o["limit"], round(101.11 * 1.0005, 2))
        self.assertTrue(o["cid"].startswith("amhd-f-"))
        self.assertEqual(self.eng.armed_list(), [])                   # one shot
        self.assertEqual(len(self.state["entries"]), 1)
        self.assertEqual(self.state["fast"][0]["status"], "entering")
        self.b.order_book["e1"].update(status="filled", filled_avg_price="101.12", filled_qty="33")
        self.tick(now=time.time() + 6)
        self.assertEqual(self.state["fast"][0]["status"], "open")
        self.b.order_book["e1"]["legs"][0].update(status="filled", filled_avg_price="102.40")
        self.tick(now=time.time() + 12)
        f = self.state["fast"][0]
        self.assertEqual((f["status"], f["result"], f["how"]), ("closed", "win", "target"))
        self.assertAlmostEqual(f["pnl"], (102.40 - 101.12) * 29, places=2)

    def test_no_chasing_wide_spread_stale_and_breakdown(self):
        self.arm()
        self.b.snaps["AAPL"] = quote(101.6)                            # 0.54% past the trigger: too far
        self.tick()
        self.assertIn("not chasing", self.eng.status)
        self.b.snaps["AAPL"] = quote(101.10, 100.9, 101.3)             # spread ~0.4%
        self.tick()
        self.assertIn("spread is too wide", self.eng.status)
        self.b.snaps["AAPL"] = quote(101.10, age=300)
        self.tick()
        self.assertIn("stale", self.eng.status)
        self.assertEqual(self.b.orders, [])
        self.assertEqual(len(self.eng.armed_list()), 1)               # still waiting
        self.b.snaps["AAPL"] = quote(100.2)                            # fell through the stop first
        self.tick()
        self.assertEqual(self.eng.armed_list(), [])
        self.assertTrue(any("dropped to its stop" in x for x in self.logs))

    def test_expiry_and_gate(self):
        self.arm()
        self.tick(now=time.time() + 16 * 60)
        self.assertTrue(any("expired" in x for x in self.logs))
        self.arm()
        self.b.snaps["AAPL"] = quote(101.10)
        self.tick(gate=lambda: "daily loss limit reached")
        self.assertEqual(self.b.orders, [])
        self.assertTrue(any("daily loss limit reached" in x for x in self.logs))

    def test_unfilled_entry_cancelled_and_time_stop(self):
        self.arm()
        self.b.snaps["AAPL"] = quote(101.10)
        t0 = time.time()
        self.tick(now=t0)
        self.tick(now=t0 + FT.FILL_WAIT_SEC + 6)
        self.assertIn("e1", self.b.cancelled)
        self.assertEqual(self.state["fast"][0]["result"], "not filled")
        self.arm()
        self.tick(now=t0 + 100)
        self.b.order_book["e2"].update(status="filled", filled_avg_price="101.12", filled_qty="33")
        self.tick(now=t0 + 106)
        self.b.snaps["AAPL"] = quote(100.90)
        self.tick(now=t0 + 106 + 45 * 60)                               # 45-minute time stop
        self.assertEqual(self.b.sells[-1][:2], ("AAPL", 29))
        self.assertIn("e2tp", self.b.cancelled)
        f = self.state["fast"][1]
        self.assertEqual((f["result"], f["how"]), ("loss", "time stop (45 min)"))

    def test_loss_cooldown_and_daily_loss_cap(self):
        self.state["fast"] = [{"status": "closed", "result": "loss"}] * 3
        self.assertIn("3 losing trades", self.eng.entry_block(self.s, self.state, time.time()))
        self.state["fast"] = []
        self.eng.last_loss_at = time.time()
        self.assertIn("cooling down", self.eng.entry_block(self.s, self.state, time.time()))
        self.assertEqual(self.eng.entry_block(self.s, self.state, time.time() + 16 * 60), "")
        self.state["fast"] = [{"status": "open", "symbol": "X"}] * 2
        self.assertIn("2 fast trade(s) open", self.eng.entry_block(self.s, self.state, time.time() + 16 * 60))

    def test_flatten_closes_fast_records(self):
        self.state["fast"] = [{"status": "open", "symbol": "AAPL"}]
        T.flatten(self.b, self.state, progress=lambda *_: None)
        self.assertEqual(self.state["fast"][0]["status"], "closed")

    def test_day_tick_uses_fast_planner(self):
        made = []
        self.b.is_open = True
        with mock.patch.object(T, "minutes_until_close", return_value=200):
            plan, wait, done = T.day_tick(self.b, self.s, self.state, progress=lambda *_: None,
                                          make_plan=lambda *a: made.append(a) or "P", every_min=5)
        self.assertEqual((plan, wait, done), ("P", 300, False))


class ScanMarket:
    def __init__(self, feed="iex"):
        self._feed, self.calls = feed, []

    def feed(self):
        return self._feed

    def scan(self, kind, filters, sort="change_pct", limit=100, symbols=None):
        self.calls.append((kind, filters))
        rows = [  # symbol, price, change %, volume so far (IEX), previous day volume (IEX)
            {"symbol": "HOT", "price": 40, "change_pct": 6.2, "volume": 90_000, "prev_volume": 60_000, "name": "Hot Co"},
            {"symbol": "WARM", "price": 20, "change_pct": 2.5, "volume": 45_000, "prev_volume": 40_000, "name": "Warm"},
            {"symbol": "THIN", "price": 8, "change_pct": 9.0, "volume": 2_000, "prev_volume": 500, "name": "Thin"},
            {"symbol": "NORMAL", "price": 100, "change_pct": 1.5, "volume": 50_000, "prev_volume": 400_000, "name": "Normal"}]
        return {"scanned": 9000, "matched": len(rows), "rows": rows}


class ScanTests(unittest.TestCase):
    def test_stocks_in_play(self):
        d = T.load_settings()["day"]
        m = ScanMarket()
        noon = dt.datetime(2026, 11, 5, 12, 45, tzinfo=T.ET)   # half the session gone
        res = TF.F.stocks_in_play(m, d, now=noon)
        self.assertEqual(res["scanned"], 9000)
        self.assertEqual([r["symbol"] for r in res["rows"]], ["HOT", "WARM"])  # THIN: too little traded; NORMAL: normal volume
        self.assertEqual(res["rows"][0]["rel_volume"], 3.0)                    # 90k vs 60k x 0.5 of the day
        self.assertEqual(res["rows"][0]["dollar_volume_m"], 180)               # $3.6M on IEX ~ $180M whole market
        self.assertEqual(m.calls[0][1], {"price_min": 5, "change_min": 1})
        d["scan_top"] = 1
        self.assertEqual(len(TF.F.stocks_in_play(m, d, now=noon)["rows"]), 1)


class AppTests(Base):
    def test_scan_adds_stocks_in_play_to_the_watchlist(self):
        app = TF.D.App(broker_factory=lambda mode: self.b)
        app.market = ScanMarket()
        s = T.load_settings()
        self.assertIs(app.with_stocks_in_play(s), s)                         # off: unchanged
        s["day"]["scan_all"] = True
        with mock.patch.object(TF.F, "stocks_in_play", return_value={"scanned": 9000, "rows": [
                {"symbol": "HOT", "change_pct": 6.2, "rel_volume": 3.0, "dollar_volume_m": 180, "name": ""},
                {"symbol": "SPY", "change_pct": 1.1, "rel_volume": 2.1, "dollar_volume_m": 900, "name": ""}]}):
            out = app.with_stocks_in_play(s)
        self.assertEqual(out["day"]["watchlist"][-1], "HOT")                 # your list first, no duplicate SPY
        self.assertEqual(out["day"]["watchlist"].count("SPY"), 1)
        self.assertNotIn("HOT", T.load_settings()["day"]["watchlist"])       # saved settings untouched
        self.assertEqual(app.in_play["rows"][0]["symbol"], "HOT")
        with mock.patch.object(TF.F, "stocks_in_play", side_effect=RuntimeError("rate limited")):
            self.assertEqual(app.with_stocks_in_play(s)["day"]["watchlist"], s["day"]["watchlist"])

    def test_disarm_api_and_live_view(self):
        app = TF.D.App(broker_factory=lambda mode: self.b)
        app.market, app._store = self.mk, self.store
        app.fast = self.eng
        self.arm()
        view = self.eng.view(self.state)
        self.assertEqual(view["armed"][0]["symbol"], "AAPL")
        self.assertGreater(view["armed"][0]["seconds_left"], 800)
        aid = view["armed"][0]["id"]
        self.assertEqual(self.eng.disarm(aid, "cancelled by you"), 1)
        self.assertEqual(self.eng.armed_list(), [])


if __name__ == "__main__":
    unittest.main()
