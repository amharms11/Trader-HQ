"""Run with: python -m unittest test_paper_trader.py  (no network, no keys needed)"""
import builtins
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import paper_trader as pt

SETTINGS = dict(pt.DEFAULT_SETTINGS, watchlist=["SPY", "AAPL"])


def snap(cash=10_000.0, equity=10_000.0, last_equity=10_000.0, positions=None):
    return {
        "account": {"equity": equity, "cash": cash, "equity_at_last_close": last_equity},
        "positions": positions or [],
        "market": {"SPY": {"latest_price": 100.0}, "AAPL": {"latest_price": 200.0},
                   "TSLA": {"latest_price": 300.0}},
    }


def trade(symbol="SPY", side="buy", qty=1):
    return {"symbol": symbol, "side": side, "qty": qty, "reason": "r", "risk": "k"}


class CheckTradeTests(unittest.TestCase):
    def check(self, t, s=None, placed=0, market_open=True, **overrides):
        return pt.check_trade(t, s or snap(), dict(SETTINGS, **overrides), placed, market_open)

    def test_ok_buy_sets_limit_above_price(self):
        ok, why, limit = self.check(trade(qty=2))
        self.assertTrue(ok, why)
        self.assertEqual(limit, 100.5)

    def test_market_closed(self):
        self.assertFalse(self.check(trade(), market_open=False)[0])

    def test_not_on_watchlist(self):
        ok, why, _ = self.check(trade("TSLA"))
        self.assertFalse(ok)
        self.assertIn("watchlist", why)

    def test_over_order_limit(self):
        ok, why, _ = self.check(trade(qty=5))  # 5 * 100.5 > 500
        self.assertFalse(ok)
        self.assertIn("per-order", why)

    def test_not_enough_cash(self):
        self.assertFalse(self.check(trade(qty=2), snap(cash=150))[0])

    def test_position_cap(self):
        s = snap(positions=[{"symbol": "SPY", "qty": 19}])  # 1900 + 100.5 > 20% of 10k
        ok, why, _ = self.check(trade(), s)
        self.assertFalse(ok)
        self.assertIn("%", why)

    def test_daily_loss_stop_blocks_buys_not_sells(self):
        s = snap(equity=9_600, last_equity=10_000, positions=[{"symbol": "SPY", "qty": 3}])
        self.assertFalse(self.check(trade(), s)[0])
        self.assertTrue(self.check(trade(side="sell"), s)[0])

    def test_no_short_selling(self):
        s = snap(positions=[{"symbol": "SPY", "qty": 1}])
        self.assertFalse(self.check(trade(side="sell", qty=2), s)[0])
        ok, _, limit = self.check(trade(side="sell", qty=1), s)
        self.assertTrue(ok)
        self.assertEqual(limit, 99.5)

    def test_daily_order_count(self):
        self.assertFalse(self.check(trade(), placed=5)[0])

    def test_bad_qty_and_side(self):
        self.assertFalse(self.check(trade(qty=0))[0])
        self.assertFalse(self.check(trade(side="short"))[0])

    def test_unknown_symbol_has_no_price(self):
        self.assertFalse(self.check(trade("ZZZZ"))[0])


class PlanParsingTests(unittest.TestCase):
    def test_parse_and_normalize(self):
        plan = pt.parse_plan(json.dumps({"outlook": "ok", "trades": [
            {"symbol": " spy ", "side": "BUY", "qty": 2, "reason": "r", "risk": "k"},
            {"symbol": "AAPL", "side": "buy"},  # missing qty: dropped
        ]}))
        self.assertEqual(plan["trades"], [trade(qty=2)])

    def test_code_fence(self):
        self.assertEqual(pt.parse_plan('```json\n{"outlook": "x", "trades": []}\n```')["trades"], [])

    def test_garbage(self):
        with self.assertRaises(RuntimeError):
            pt.parse_plan("not json")


class FakeBroker:
    def __init__(self, is_open=True):
        self.is_open, self.orders = is_open, []

    def clock(self):
        return {"is_open": self.is_open}

    def orders_today(self):
        return []

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id):
        self.orders.append((symbol, side, qty, limit_price))
        return {"id": "o1", "status": "accepted"}


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(pt, "DATA", tmp), mock.patch.object(pt, "JOURNAL_FILE", tmp / "j.jsonl"),
                        mock.patch.object(pt, "load_settings", return_value=dict(SETTINGS))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_with(self, broker, plan, answers):
        with mock.patch.object(pt, "Broker", return_value=broker), \
             mock.patch.object(pt, "snapshot", return_value=copy.deepcopy(snap())), \
             mock.patch.object(pt, "ask_claude", return_value=plan), \
             mock.patch.object(builtins, "input", side_effect=answers), redirect_stdout(io.StringIO()) as out:
            pt.run()
        return out.getvalue()

    def test_only_approved_orders_are_placed(self):
        b = FakeBroker()
        plan = {"outlook": "o", "trades": [trade(qty=2), trade("AAPL", qty=1), trade("TSLA")]}
        out = self.run_with(b, plan, ["y", "n"])  # TSLA is blocked, so it is never asked about
        self.assertEqual(b.orders, [("SPY", "buy", 2, 100.5)])
        self.assertIn("watchlist", out)

    def test_second_buy_checked_against_first(self):
        b = FakeBroker()
        plan = {"outlook": "o", "trades": [trade(qty=4), trade(qty=4), trade(qty=4), trade(qty=4),
                                           trade(qty=4), trade(qty=4)]}
        self.run_with(b, plan, ["y"] * 6)
        # 20% cap of $10k: 4 orders of 4 SPY at $100.50 = $1,608; a 5th would make $2,010
        self.assertEqual(len(b.orders), 4)

    def test_market_closed_places_nothing(self):
        b = FakeBroker(is_open=False)
        self.run_with(b, {"outlook": "o", "trades": [trade()]}, [])
        self.assertEqual(b.orders, [])


class SafetyTests(unittest.TestCase):
    def test_live_keys_refused(self):
        with mock.patch.dict("os.environ", {"ALPACA_API_KEY_ID": "AK123", "ALPACA_API_SECRET_KEY": "x"}), \
             mock.patch.object(pt, "load_config"):
            with self.assertRaises(SystemExit):
                pt.Broker()

    def test_paper_endpoint_only(self):
        self.assertEqual(pt.PAPER_URL, "https://paper-api.alpaca.markets")

    def test_summarize_bars(self):
        bars = [{"c": 100 + i, "h": 101 + i, "l": 99 + i} for i in range(30)]
        s = pt.summarize_bars(bars)
        self.assertEqual(s["last_close"], 129)
        self.assertEqual(s["high_60d"], 130)
        self.assertAlmostEqual(s["change_pct_1d"], round((129 / 128 - 1) * 100, 2))


if __name__ == "__main__":
    unittest.main()
