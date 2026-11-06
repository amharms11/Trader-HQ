"""Tests for crypto fast mode: limit-order swing setups, the stop watcher and its safety rules. No network."""
import datetime as dt
import time
import unittest
from unittest import mock

import cfast as CF
import features as F
import trader as T
import test_features as TF


def iso(age=1):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeMarket:
    def __init__(self):
        self.snaps = {"BTC/USD": self.snap(60000)}

    @staticmethod
    def snap(price, age=1, spread=10):
        return {"latestTrade": {"p": price, "t": iso(age)}, "latestQuote": {"bp": price - spread / 2, "ap": price + spread / 2}}

    def data(self, path, params=None):
        syms = params["symbols"].split(",")
        if path == "/v1beta3/crypto/us/snapshots":
            return {"snapshots": {s: self.snaps[s] for s in syms if s in self.snaps}}
        if path == "/v1beta3/crypto/us/bars":
            now = dt.datetime.now(dt.timezone.utc)
            bars = [{"t": (now - dt.timedelta(minutes=120 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "o": 60000 + i, "h": 60050 + i,
                     "l": 59950 + i, "c": 60000 + i, "v": 2, "vw": 60000 + i} for i in range(120)]
            return {"bars": {syms[0]: bars}}
        raise AssertionError(path)


class Broker(TF.Broker):
    def __init__(self):
        super().__init__()
        self.book = {}

    def _api(self, path, params=None, body=None, method="GET"):
        if path == "/v2/orders" and method == "POST":
            self.orders.append(body)
            oid = f"o{len(self.orders)}"
            self.book[oid] = {"id": oid, "status": "new", "filled_qty": "0", **body}
            return {"id": oid, "status": "accepted"}
        return super()._api(path, params, body, method)

    def get_order(self, oid):
        return self.book.get(oid, {"status": "canceled"})

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        if oid in self.book and self.book[oid]["status"] != "filled":
            self.book[oid]["status"] = "canceled"


def setup(**kw):
    return {"symbol": "BTC/USD", "side": "buy", "entry_price": 59700, "stop_price": 59400, "target_price": 60300,
            "valid_minutes": 20, "reason": "bounce off support", "risk": "k", "qty": 0, **kw}


class Base(TF.Base):
    def setUp(self):
        super().setUp()
        self.b = Broker()
        self.m = FakeMarket()
        self.s = T.load_settings()
        self.s["crypto_day"].update(watchlist=["BTC/USD", "ETH/USD", "DOGE/USD"], fast_mode=True, max_risk_per_trade_usd=15,
                                    max_order_usd=3000, around_the_clock=True)
        self.s["crypto"]["max_position_pct"] = 100
        self.logs, self.notes = [], []
        self.eng = CF.Engine(self.store, log=self.logs.append, notify=lambda *a: self.notes.append(a))

    def plan(self, setups, outlook="ranging"):
        with mock.patch.object(T, "ask_claude", return_value={"outlook": outlook, "setups": setups}) as ask:
            p = CF.Plan(self.b, self.s, self.m, self.eng, progress=lambda *_: None)
        self.ask = ask
        return p

    def arm(self, **kw):
        p = self.plan([setup(**kw)])
        ok, why, o = p.check(p.trades[0])
        self.assertTrue(ok, why)
        p.place(p.trades[0], o, 1)
        return o

    def row(self):
        return self.eng.store.rows("paper")[-1]


class TimeTests(unittest.TestCase):
    def test_parse_alpaca_times(self):
        for text in ("2026-11-06T00:36:45.280316119+00:00", "2026-11-06T00:36:45.280316119Z", "2026-11-06T00:36:45.28Z",
                     "2026-11-06T00:36:45Z", "2026-11-05T19:36:45.123456789-05:00"):
            t = T.parse_time(text)
            self.assertEqual((t.hour if t.utcoffset().total_seconds() == 0 else t.astimezone(dt.timezone.utc).hour, t.minute, t.second), (0, 36, 45), text)
        self.assertEqual(T.parse_time("2026-11-06T00:36:45.280316119Z").microsecond, 280316)
        self.assertEqual(T.parse_time("2026-11-06T00:36:45.5Z").microsecond, 500000)


class CheckTests(Base):
    def test_good_setup_sized_from_risk(self):
        p = self.plan([setup()])
        ok, why, o = p.check(p.trades[0])
        self.assertTrue(ok, why)
        self.assertAlmostEqual(o["amount"], 15 / (300 / 59700), places=0)   # $15 risk / 0.5% stop = ~$2985
        self.assertAlmostEqual(o["risk"], 15, places=1)
        self.assertEqual(self.ask.call_args[1]["kind"], "crypto_day")
        self.assertIn("ARM swing: buy", p.headline(p.trades[0], o))

    def test_sized_to_fit_auto_approve(self):
        p = self.plan([setup()])
        p.size_cap = 325
        ok, why, o = p.check(p.trades[0])
        self.assertTrue(ok, why)
        self.assertEqual(o["amount"], 325)
        self.assertEqual(T.trade_value("crypto_day", p.trades[0], o), 325)

    def test_rejections(self):
        p = self.plan([])
        cases = [(setup(entry_price=60200, stop_price=60000, target_price=61000), "only buys dips"),
                 (setup(entry_price=58000, stop_price=57800, target_price=58800), "more than 1.5% below"),
                 (setup(target_price=59900), "minimum is 0.6%"),
                 (setup(stop_price=59000, target_price=60200), "1.5x the stop"),
                 (setup(stop_price=59800), "stop must be below"),
                 (setup(symbol="SOL/USD"), "no current price")]
        for t, word in cases:
            ok, why, _ = p.check(t)
            self.assertFalse(ok, t)
            self.assertIn(word, why)
        self.s["crypto_day"]["watchlist"] = ["ETH/USD"]
        self.assertIn("isn't on your crypto day-trading list", p.check(setup())[1])

    def test_minimum_target_and_meme_limits_and_counts(self):
        self.s["crypto_day"]["fast_min_target_pct"] = 1.2
        self.assertIn("minimum is 1.2%", self.plan([]).check(setup())[1])
        self.s["crypto_day"]["fast_min_target_pct"] = 0.6
        self.m.snaps["DOGE/USD"] = FakeMarket.snap(0.2, spread=0.0001)
        ok, why, o = self.plan([]).check(setup(symbol="DOGE/USD", entry_price=0.199, stop_price=0.196, target_price=0.205))
        self.assertTrue(ok, why)
        self.assertEqual(o["amount"], 75)                                      # meme coins: $75 cap
        self.arm()
        p = self.plan([setup()])
        self.assertEqual(p.trades, [])                                          # BTC already has a fast trade
        self.assertIn("already a fast BTC/USD", p.check(setup())[1])
        self.eng.last_loss_at = time.time()
        self.assertIn("cooling down", p.check(setup(symbol="DOGE/USD", entry_price=0.199, stop_price=0.196, target_price=0.205))[1])


class EngineTests(Base):
    def test_limit_buy_then_resting_target_wins(self):
        self.arm()
        buy = self.b.orders[0]
        self.assertEqual((buy["type"], buy["side"], buy["time_in_force"]), ("limit", "buy", "gtc"))
        self.assertTrue(buy["client_order_id"].startswith("amhy-f-"))           # counts as crypto day trading
        self.assertEqual(F.strategy_of(buy["client_order_id"]), "Crypto day trading")
        self.assertEqual(self.row()["status"], "buying")
        self.eng.tick(self.b, self.s, self.m)
        self.assertEqual(len(self.b.orders), 1)                                  # still waiting
        self.b.book["o1"].update(status="filled", filled_qty="0.05", filled_avg_price="59700")
        self.eng.tick(self.b, self.s, self.m)
        tgt = self.b.orders[1]
        self.assertEqual((tgt["type"], tgt["side"], tgt["qty"], float(tgt["limit_price"])), ("limit", "sell", "0.05", 60300))
        self.assertEqual(self.row()["status"], "open")
        self.b.book["o2"].update(status="filled", filled_avg_price="60300")
        self.eng.tick(self.b, self.s, self.m)
        r = self.row()
        self.assertEqual((r["status"], r["result"], r["how"]), ("closed", "win", "target"))
        self.assertAlmostEqual(r["pnl"], 30.0)
        view = self.eng.view("paper")
        self.assertEqual((view["wins"], view["today_pnl"]), (1, 30.0))

    def test_nanosecond_timestamps_from_alpaca(self):
        self.arm()
        self.b.book["o1"].update(status="filled", filled_qty="0.05", filled_avg_price="59700")
        self.eng.tick(self.b, self.s, self.m)
        nanos = (dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "119+00:00"
        self.m.snaps["BTC/USD"] = {"latestTrade": {"p": 59390, "t": nanos}, "latestQuote": {"bp": 59385, "ap": 59395}}
        self.eng.tick(self.b, self.s, self.m)
        self.assertEqual(self.row()["how"], "stop-loss")                       # stop still watched

    def test_stop_cancels_target_and_sells_at_market(self):
        self.arm()
        self.b.book["o1"].update(status="filled", filled_qty="0.05", filled_avg_price="59700")
        self.eng.tick(self.b, self.s, self.m)
        self.m.snaps["BTC/USD"] = FakeMarket.snap(59390)
        self.eng.tick(self.b, self.s, self.m)
        self.assertIn("o2", self.b.cancelled)
        sell = self.b.orders[-1]
        self.assertEqual((sell["type"], sell["side"], sell["qty"]), ("market", "sell", "0.05"))
        r = self.row()
        self.assertEqual((r["result"], r["how"]), ("loss", "stop-loss"))
        self.assertGreater(self.eng.last_loss_at, 0)

    def test_buy_cancelled_on_expiry_or_breakdown(self):
        self.arm()
        self.eng.tick(self.b, self.s, self.m, now=time.time() + 21 * 60)
        self.assertEqual((self.row()["status"], self.row()["result"]), ("closed", "not filled"))
        self.assertIn("o1", self.b.cancelled)
        self.arm()
        self.m.snaps["BTC/USD"] = FakeMarket.snap(59350)
        self.eng.tick(self.b, self.s, self.m)
        self.assertIn("fell to the stop", self.row()["how"])

    def test_partial_fill_then_cancel_keeps_what_was_bought(self):
        self.arm()
        self.b.book["o1"].update(filled_qty="0.02", filled_avg_price="59700", status="partially_filled")
        self.eng.tick(self.b, self.s, self.m, now=time.time() + 21 * 60)
        r = self.row()
        self.assertEqual((r["status"], r["qty"]), ("open", 0.02))
        self.assertEqual(self.b.orders[-1]["qty"], "0.02")

    def test_stale_prices_dont_trigger_stops_and_max_hold(self):
        self.arm()
        self.b.book["o1"].update(status="filled", filled_qty="0.05", filled_avg_price="59700")
        self.eng.tick(self.b, self.s, self.m)
        self.m.snaps["BTC/USD"] = FakeMarket.snap(59000, age=600)
        self.eng.tick(self.b, self.s, self.m)
        self.assertEqual(self.row()["status"], "open")
        self.assertIn("no fresh price", self.eng.status)
        self.m.snaps["BTC/USD"] = FakeMarket.snap(59800)
        self.eng.tick(self.b, self.s, self.m, now=time.time() + 9 * 3600)        # held past 8 hours
        self.assertEqual(self.row()["how"], "held 8 hours")

    def test_cancel_api_style(self):
        self.arm()
        self.eng.cancel(self.b, self.row()["id"])
        self.assertEqual(self.row()["result"], "not filled")
        with self.assertRaises(ValueError):
            self.eng.cancel(self.b, "nope")


if __name__ == "__main__":
    unittest.main()
