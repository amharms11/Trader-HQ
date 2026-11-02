"""Run with: python -m unittest test_trader.py  (no network, no keys needed)"""
import builtins
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import trader as pt

SETTINGS = copy.deepcopy(pt.DEFAULT_SETTINGS)
SETTINGS["watchlist"] = ["SPY", "AAPL"]
SETTINGS["day"]["watchlist"] = ["SPY", "AAPL"]


def settings(**overrides):
    s = copy.deepcopy(SETTINGS)
    sections = {k: overrides.pop(k, {}) for k in ("day", "options", "market", "crypto", "auto_rules")}
    s.update(overrides)
    for k, v in sections.items():
        s[k].update(v)
    return s


def snap(cash=10_000.0, equity=10_000.0, last_equity=10_000.0, positions=None):
    return {
        "account": {"equity": equity, "cash": cash, "equity_at_last_close": last_equity},
        "positions": positions or [],
        "market": {"SPY": {"latest_price": 100.0}, "AAPL": {"latest_price": 200.0},
                   "TSLA": {"latest_price": 300.0}},
    }


def day_snap(minutes=200, entries=0, day_positions=None, holdings=None, **kw):
    s = snap(**kw)
    s.update({"minutes_until_close": minutes, "entries_today": entries,
              "day_positions": day_positions or [], "all_holdings": holdings or {}})
    return s


def trade(symbol="SPY", side="buy", qty=1, **extra):
    return {"symbol": symbol, "side": side, "qty": qty, "reason": "r", "risk": "k", **extra}


def day_trade(symbol="SPY", side="buy", qty=5, stop=1.0, take=2.0):
    return trade(symbol, side, qty, stop_loss_pct=stop, take_profit_pct=take)


class TempData(unittest.TestCase):
    """Points ~/.amh-trader at a temp folder for the test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [mock.patch.object(pt, "DATA", self.tmp),
                         mock.patch.object(pt, "JOURNAL_FILE", self.tmp / "j.jsonl"),
                         mock.patch.object(pt, "SETTINGS_FILE", self.tmp / "settings.json"),
                         mock.patch.object(pt, "CONFIG_FILE", self.tmp / "config.env")]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class CheckTradeTests(unittest.TestCase):
    def check(self, t, s=None, placed=0, market_open=True, **overrides):
        return pt.check_trade(t, s or snap(), settings(**overrides), placed, market_open)

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


class CheckDayTradeTests(unittest.TestCase):
    def check(self, t, s=None, **overrides):
        return pt.check_day_trade(t, s or day_snap(), settings(**overrides))

    def test_ok_entry_has_stop_and_target(self):
        ok, why, o = self.check(day_trade(qty=4, stop=1, take=2))
        self.assertTrue(ok, why)
        self.assertEqual(o["limit_price"], 100.5)
        self.assertEqual(o["stop_price"], 99.5)
        self.assertEqual(o["take_profit"], 102.51)
        self.assertAlmostEqual(o["risk"], 4.0)

    def test_risk_per_trade_limit(self):
        ok, why, _ = self.check(day_trade(qty=4, stop=2), day={"max_risk_per_trade_usd": 5})
        self.assertFalse(ok)
        self.assertIn("risk", why)

    def test_stop_loss_required_and_capped(self):
        self.assertFalse(self.check(day_trade(stop=0))[0])
        self.assertFalse(self.check(day_trade(stop=5))[0])
        self.assertFalse(self.check(day_trade(take=0))[0])

    def test_no_entries_near_close(self):
        ok, why, _ = self.check(day_trade(), day_snap(minutes=25))
        self.assertFalse(ok)
        self.assertIn("close", why)

    def test_entry_count(self):
        self.assertFalse(self.check(day_trade(), day_snap(entries=6))[0])

    def test_day_watchlist(self):
        self.assertFalse(self.check(day_trade("TSLA"))[0])

    def test_shared_buy_limits_apply(self):
        self.assertFalse(self.check(day_trade(qty=5, stop=0.5), day_snap(cash=100))[0])  # no cash
        self.assertFalse(self.check(day_trade(qty=6, stop=0.5))[0])  # over $500 per order

    def test_position_cap_counts_long_term_holdings(self):
        self.assertFalse(self.check(day_trade(qty=2, stop=0.5), day_snap(holdings={"SPY": 19}))[0])

    def test_sell_closes_whole_day_position_only(self):
        s = day_snap(day_positions=[{"symbol": "SPY", "qty": 7}], holdings={"SPY": 10, "AAPL": 3})
        ok, _, o = self.check(day_trade(side="sell", qty=2), s)
        self.assertTrue(ok)
        self.assertEqual(o["qty"], 7)
        ok, why, _ = self.check(day_trade("AAPL", side="sell"), s)  # long-term holding: untouched
        self.assertFalse(ok)
        self.assertIn("long-term", why)

    def test_sells_allowed_near_close_and_after_loss(self):
        s = day_snap(minutes=15, equity=9_000, day_positions=[{"symbol": "SPY", "qty": 2}])
        self.assertTrue(self.check(day_trade(side="sell"), s)[0])


class PlanParsingTests(unittest.TestCase):
    def test_parse_and_normalize(self):
        plan = pt.parse_plan(json.dumps({"outlook": "ok", "trades": [
            {"symbol": " spy ", "side": "BUY", "qty": 2, "reason": "r", "risk": "k"},
            {"symbol": "AAPL", "side": "buy"},  # missing qty: dropped
        ]}))
        self.assertEqual(plan["trades"], [trade(qty=2)])

    def test_day_fields_kept(self):
        plan = pt.parse_plan(json.dumps({"outlook": "", "trades": [
            {"symbol": "SPY", "side": "buy", "qty": 1, "reason": "", "risk": "",
             "stop_loss_pct": 1, "take_profit_pct": "2.5"}]}))
        self.assertEqual(plan["trades"][0]["take_profit_pct"], 2.5)

    def test_code_fence(self):
        self.assertEqual(pt.parse_plan('```json\n{"outlook": "x", "trades": []}\n```')["trades"], [])

    def test_garbage(self):
        with self.assertRaises(RuntimeError):
            pt.parse_plan("not json")

    def test_day_schema_requires_stop_and_target(self):
        req = pt.DAY_SCHEMA["properties"]["trades"]["items"]["required"]
        self.assertIn("stop_loss_pct", req)
        self.assertNotIn("stop_loss_pct", pt.PLAN_SCHEMA["properties"]["trades"]["items"]["required"])


class FakeBroker:
    def __init__(self, is_open=True, mode="paper", positions=None):
        self.is_open, self.mode, self.orders, self.cancelled = is_open, mode, [], []
        self._positions = positions or []

    def clock(self):
        return {"is_open": self.is_open, "timestamp": "2026-11-02T15:00:00Z", "next_close": "2026-11-02T21:00:00Z"}

    def orders_today(self):
        return []

    def positions(self):
        return self._positions

    def stock_positions(self):
        return [p for p in self._positions if not pt.is_option(p)]

    def place_limit_order(self, symbol, side, qty, limit_price, client_order_id):
        self.orders.append((symbol, side, qty, limit_price))
        return {"id": "o1", "status": "accepted"}

    def place_bracket_buy(self, symbol, qty, limit_price, take_profit, stop_price, client_order_id):
        self.orders.append((symbol, "bracket", qty, limit_price, stop_price, take_profit))
        return {"id": f"p{len(self.orders)}", "status": "accepted",
                "legs": [{"id": f"tp{len(self.orders)}"}, {"id": f"sl{len(self.orders)}"}]}

    def place_market_sell(self, symbol, qty, client_order_id):
        self.orders.append((symbol, "market_sell", qty))
        return {"id": "m1", "status": "accepted"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def get_order(self, order_id):
        return {"status": "canceled"}


class RunTests(TempData):
    def run_with(self, broker, plan, answers, s=None):
        with mock.patch.object(pt, "Broker", return_value=broker), \
             mock.patch.object(pt, "load_settings", return_value=s or settings()), \
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

    def test_later_buys_checked_against_earlier_ones(self):
        b = FakeBroker()
        self.run_with(b, {"outlook": "o", "trades": [trade(qty=4)] * 6}, ["y"] * 6)
        # 20% cap of $10k: 4 orders of 4 SPY at $100.50 = $1,608; a 5th would make $2,010
        self.assertEqual(len(b.orders), 4)

    def test_market_closed_places_nothing(self):
        b = FakeBroker(is_open=False)
        self.run_with(b, {"outlook": "o", "trades": [trade()]}, [])
        self.assertEqual(b.orders, [])

    def test_auto_approve_on_paper(self):
        b = FakeBroker()
        self.run_with(b, {"outlook": "o", "trades": [trade()]}, [], settings(auto_approve=True))
        self.assertEqual(len(b.orders), 1)

    def test_live_requires_typing_live_and_ignores_auto_approve(self):
        b = FakeBroker(mode="live")
        s = settings(mode="live", auto_approve=True)
        with self.assertRaises(SystemExit):
            self.run_with(b, {"outlook": "o", "trades": [trade()]}, ["yes"], s)
        self.assertEqual(b.orders, [])
        self.run_with(b, {"outlook": "o", "trades": [trade()]}, ["LIVE", "n"], s)  # still asks per order
        self.assertEqual(b.orders, [])


class DayTests(TempData):
    def day_with(self, broker, plans, answers, s=None):
        with mock.patch.object(pt, "Broker", return_value=broker), \
             mock.patch.object(pt, "load_settings", return_value=s or settings()), \
             mock.patch.object(pt, "day_snapshot", side_effect=lambda *a: copy.deepcopy(day_snap())), \
             mock.patch.object(pt, "ask_claude", side_effect=plans), \
             mock.patch.object(builtins, "input", side_effect=answers), redirect_stdout(io.StringIO()) as out:
            pt.day(once=True)
        return out.getvalue()

    def test_approved_entry_is_bracket_order_and_saved(self):
        b = FakeBroker(positions=[{"symbol": "AAPL", "qty": "3"}])
        self.day_with(b, [{"outlook": "o", "trades": [day_trade(qty=4), day_trade(qty=4)]}], ["y", "n"])
        self.assertEqual(b.orders, [("SPY", "bracket", 4, 100.5, 99.5, 102.51)])
        state = json.loads(pt.day_state_file("paper").read_text())
        self.assertEqual(state["baseline"], {"AAPL": 3.0})
        self.assertEqual(state["entries"][0]["leg_ids"], ["tp1", "sl1"])

    def test_flatten_sells_only_day_shares(self):
        b = FakeBroker(positions=[{"symbol": "SPY", "qty": "10"}, {"symbol": "AAPL", "qty": "3"}])
        state = {"baseline": {"SPY": 4, "AAPL": 3}, "entries": [
            {"symbol": "SPY", "qty": 6, "order_id": "p1", "leg_ids": ["tp1", "sl1"]}], "flattened": False}
        with redirect_stdout(io.StringIO()):
            sold = pt.flatten(b, state)
        self.assertEqual(sold, {"SPY": 6})
        self.assertEqual(b.orders, [("SPY", "market_sell", 6)])
        self.assertEqual(b.cancelled, ["tp1", "sl1", "p1"])
        self.assertTrue(state["flattened"])

    def test_near_close_flattens_instead_of_trading(self):
        b = FakeBroker()
        b.clock = lambda: {"is_open": True, "timestamp": "2026-11-02T20:55:00Z",
                           "next_close": "2026-11-02T21:00:00Z"}
        out = self.day_with(b, [], [])
        self.assertIn("Closing all day positions", out)
        self.assertTrue(json.loads(pt.day_state_file("paper").read_text())["flattened"])

    def test_sell_cancels_stop_and_target_first(self):
        b = FakeBroker(positions=[{"symbol": "SPY", "qty": "5"}])
        pt.save_day_state("paper", {"baseline": {}, "entries": [
            {"symbol": "SPY", "qty": 5, "order_id": "p1", "leg_ids": ["tp1", "sl1"]}], "flattened": False})
        snap_with_pos = day_snap(day_positions=[{"symbol": "SPY", "qty": 5}])
        with mock.patch.object(pt, "day_snapshot", return_value=snap_with_pos), \
             mock.patch.object(pt, "Broker", return_value=b), \
             mock.patch.object(pt, "load_settings", return_value=settings()), \
             mock.patch.object(pt, "ask_claude", return_value={"outlook": "", "trades": [day_trade(side="sell")]}), \
             mock.patch.object(builtins, "input", side_effect=["y"]), redirect_stdout(io.StringIO()):
            pt.day(once=True)
        self.assertEqual(b.cancelled, ["tp1", "sl1", "p1"])
        self.assertEqual(b.orders, [("SPY", "sell", 5, 99.5)])

    def test_closed_market_does_nothing(self):
        b = FakeBroker(is_open=False)
        out = self.day_with(b, [], [])
        self.assertIn("closed", out)
        self.assertFalse(pt.day_state_file("paper").exists())

    def test_long_term_buy_raises_baseline(self):
        pt.save_day_state("paper", {"baseline": {"SPY": 2}, "entries": [], "flattened": False})
        pt.add_to_baseline("paper", "SPY", 3)
        pt.add_to_baseline("paper", "AAPL", 1)
        self.assertEqual(json.loads(pt.day_state_file("paper").read_text())["baseline"], {"SPY": 5, "AAPL": 1})

    def test_day_qty(self):
        positions = [{"symbol": "SPY", "qty": "10"}, {"symbol": "AAPL", "qty": "2"}, {"symbol": "MSFT", "qty": "1"}]
        self.assertEqual(pt.day_qty(positions, {"baseline": {"SPY": 4, "AAPL": 2}}), {"SPY": 6, "MSFT": 1})

    def test_minutes_until_close(self):
        self.assertEqual(pt.minutes_until_close({"timestamp": "2026-11-02T15:00:00-05:00",
                                                 "next_close": "2026-11-02T16:00:00-05:00"}), 60)


class ModeAndKeyTests(TempData):
    def broker(self, mode, env):
        with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(pt, "load_config"):
            return pt.Broker(mode)

    def test_paper_keys_go_to_paper(self):
        b = self.broker("paper", {"ALPACA_PAPER_KEY_ID": "PK1", "ALPACA_PAPER_SECRET_KEY": "s"})
        self.assertEqual(b.base, "https://paper-api.alpaca.markets")

    def test_live_keys_go_to_live(self):
        b = self.broker("live", {"ALPACA_LIVE_KEY_ID": "AK1", "ALPACA_LIVE_SECRET_KEY": "s"})
        self.assertEqual(b.base, "https://api.alpaca.markets")

    def test_mismatched_keys_refused(self):
        with self.assertRaises(SystemExit):
            self.broker("paper", {"ALPACA_PAPER_KEY_ID": "AK1", "ALPACA_PAPER_SECRET_KEY": "s"})
        with self.assertRaises(SystemExit):
            self.broker("live", {"ALPACA_LIVE_KEY_ID": "PK1", "ALPACA_LIVE_SECRET_KEY": "s"})

    def test_live_without_live_keys_refused(self):
        with self.assertRaises(SystemExit):
            self.broker("live", {"ALPACA_PAPER_KEY_ID": "PK1", "ALPACA_PAPER_SECRET_KEY": "s"})

    def test_old_paper_key_names_still_work(self):
        b = self.broker("paper", {"ALPACA_API_KEY_ID": "PK1", "ALPACA_API_SECRET_KEY": "s"})
        self.assertEqual(b.key, "PK1")

    def test_settings_default_to_paper_and_merge_day_section(self):
        pt.SETTINGS_FILE.write_text(json.dumps({"max_order_usd": 100, "day": {"check_every_min": 5}}))
        s = pt.load_settings()
        self.assertEqual(s["mode"], "paper")
        self.assertEqual(s["max_order_usd"], 100)
        self.assertEqual(s["day"]["check_every_min"], 5)
        self.assertEqual(s["day"]["max_stop_loss_pct"], 2)

    def test_bad_mode_rejected(self):
        pt.SETTINGS_FILE.write_text(json.dumps({"mode": "real"}))
        with self.assertRaises(RuntimeError):
            pt.load_settings()

    def test_auto_approve_rules(self):
        s = settings(auto_approve=True, auto_rules={"max_usd": 300, "options": False})
        self.assertTrue(pt.can_auto_approve(s))                       # it's on
        self.assertTrue(pt.can_auto_approve(s, "run", 250))
        self.assertFalse(pt.can_auto_approve(s, "run", 301))          # over the slider amount
        self.assertFalse(pt.can_auto_approve(s, "options", 50))       # that bot isn't ticked
        self.assertTrue(pt.can_auto_approve(s, "crypto", 300))
        self.assertFalse(pt.can_auto_approve(s, "run", None))         # unknown size: ask
        self.assertFalse(pt.can_auto_approve(settings(auto_rules={"max_usd": 999}), "run", 1))  # switch off
        self.assertEqual(pt.trade_value("run", {"qty": 2}, {"qty": 2, "limit_price": 100.5, "cost": 201}), 201)
        self.assertEqual(pt.trade_value("options", {"qty": 1}, {"qty": 1, "limit_price": 1.2}), 120)
        self.assertEqual(pt.trade_value("day", {"qty": 3}, {"qty": 3, "limit_price": 10}), 30)

    def test_auto_approve_never_in_live(self):
        self.assertTrue(pt.can_auto_approve(settings(auto_approve=True)))
        self.assertFalse(pt.can_auto_approve(settings(mode="live", auto_approve=True)))

    def test_setup_live_keeps_paper_keys_and_mode(self):
        pt.CONFIG_FILE.write_text("ALPACA_PAPER_KEY_ID=PK1\nALPACA_PAPER_SECRET_KEY=s1\nANTHROPIC_API_KEY=x\n")
        fake = mock.Mock()
        fake.return_value.account.return_value = {"equity": "100"}
        with mock.patch.object(pt, "Broker", fake), mock.patch.object(pt, "claude_backend", return_value="api"), \
             mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(builtins, "input", side_effect=["AK9", "s9"]), redirect_stdout(io.StringIO()):
            pt.setup("live")
        text = pt.CONFIG_FILE.read_text()
        for line in ("ALPACA_LIVE_KEY_ID=AK9", "ALPACA_PAPER_KEY_ID=PK1", "ANTHROPIC_API_KEY=x"):
            self.assertIn(line, text)
        self.assertEqual(json.loads(pt.SETTINGS_FILE.read_text())["mode"], "paper")


class SummaryTests(unittest.TestCase):
    def test_summarize_bars(self):
        bars = [{"c": 100 + i, "h": 101 + i, "l": 99 + i} for i in range(30)]
        s = pt.summarize_bars(bars)
        self.assertEqual(s["last_close"], 129)
        self.assertEqual(s["high_60d"], 130)

    def test_summarize_intraday_without_vwap(self):
        s = pt.summarize_intraday([{"o": 10, "h": 11, "l": 9, "c": 10, "v": 100, "vw": None}])
        self.assertEqual(s["vwap"], 10)

    def test_summarize_intraday(self):
        bars = [{"o": 10, "h": 11, "l": 9, "c": 10, "v": 100, "vw": 10},
                {"o": 10, "h": 12, "l": 10, "c": 12, "v": 300, "vw": 12}]
        s = pt.summarize_intraday(bars)
        self.assertEqual(s["vwap"], 11.5)
        self.assertEqual(s["change_since_open_pct"], 20.0)
        self.assertEqual(s["last_bar_volume_vs_avg"], 1.5)


if __name__ == "__main__":
    unittest.main()


# ---------- options ----------

TODAY = pt.dt.date(2026, 11, 2)
CALL = "AAPL261218C00200000"   # AAPL $200 call, 2026-12-18 (46 days out)
PUT = "AAPL261218P00195000"
NEAR = "AAPL261106C00200000"   # 4 days out: too close to expiry to buy


def chain_snap(sym, bid, ask, delta=0.5):
    return {sym: {"latestQuote": {"bp": bid, "ap": ask}, "greeks": {"delta": delta}, "impliedVolatility": 0.3}}


def opt_snap(held=None, paid=0.0, equity=10_000.0, bp=5_000.0, candidates=None):
    return {
        "account": {"equity": equity, "cash": bp, "equity_at_last_close": equity, "options_buying_power": bp},
        "options_held": held or [],
        "options_paid_total": paid,
        "underlyings": {"AAPL": {"latest_price": 200.0}},
        "contracts_you_can_buy": {"AAPL": candidates if candidates is not None else [
            {"contract": CALL, "type": "call", "strike": 200.0, "expiration": "2026-12-18",
             "days_to_expiry": 46, "bid": 0.90, "ask": 1.00, "cost_per_contract": 98.0},
        ]},
    }


def held(contract=CALL, qty=1, dte=46, bid=1.2, ask=1.3, side="long"):
    return {"contract": contract, "qty": qty, "side": side, "days_to_expiry": dte, "bid": bid, "ask": ask,
            "paid_total": 100.0}


class OptionHelperTests(unittest.TestCase):
    def test_parse_occ(self):
        self.assertEqual(pt.parse_occ(CALL), {"underlying": "AAPL", "expiration": "2026-12-18",
                                              "type": "call", "strike": 200.0})
        self.assertEqual(pt.parse_occ("SPY261218P00512500")["strike"], 512.5)
        with self.assertRaises(ValueError):
            pt.parse_occ("AAPL")
        self.assertEqual(pt.describe_option(PUT), "AAPL $195 PUT expiring 2026-12-18")

    def test_limit_prices_stay_inside_bid_ask_on_valid_ticks(self):
        self.assertEqual(pt.buy_limit(0.90, 1.00), 0.98)
        self.assertEqual(pt.sell_limit(0.90, 1.00), 0.92)
        self.assertEqual(pt.buy_limit(4.00, 4.40), 4.30)   # $0.05 steps from $3
        self.assertEqual(pt.sell_limit(4.00, 4.40), 4.10)
        self.assertEqual(pt.buy_limit(1.00, 1.01), 1.01)   # never above the ask

    def test_candidates_filter_and_trim(self):
        s = settings()
        snaps = {}
        snaps.update(chain_snap(CALL, 0.90, 1.00))
        snaps.update(chain_snap(PUT, 0.50, 0.90))                     # spread too wide
        snaps.update(chain_snap(NEAR, 0.30, 0.32))                     # expires too soon
        snaps.update(chain_snap("AAPL261218C00205000", 2.50, 2.60))    # $255+ per contract: over $200
        snaps.update(chain_snap("AAPL261218C00210000", 0.00, 0.05))    # no bid
        rows = pt.option_candidates(snaps, 200.0, s, today=TODAY)
        self.assertEqual([r["contract"] for r in rows], [CALL])
        self.assertEqual(rows[0]["cost_per_contract"], 98.0)
        self.assertEqual(rows[0]["days_to_expiry"], 46)

    def test_candidates_keep_nearest_strikes(self):
        snaps = {}
        for strike in range(190, 211):
            snaps.update(chain_snap(f"AAPL261218C00{strike}000", 0.90, 1.00))
        rows = pt.option_candidates(snaps, 200.0, settings(), today=TODAY)
        self.assertEqual(sorted(r["strike"] for r in rows), [199.0, 200.0, 201.0])

    def test_stock_positions_exclude_options(self):
        b = FakeBroker(positions=[{"symbol": "SPY", "qty": "2", "asset_class": "us_equity"},
                                  {"symbol": CALL, "qty": "1", "asset_class": "us_option"}])
        self.assertEqual([p["symbol"] for p in b.stock_positions()], ["SPY"])


class CheckOptionTradeTests(unittest.TestCase):
    def check(self, t, s=None, buys=0, market_open=True, **overrides):
        return pt.check_option_trade(t, s or opt_snap(), settings(**overrides), buys, market_open)

    def test_ok_buy(self):
        ok, why, o = self.check(trade(CALL, qty=2))
        self.assertTrue(ok, why)
        self.assertEqual(o, {"qty": 2, "limit_price": 0.98, "cost": 196.0})

    def test_only_listed_contracts(self):
        ok, why, _ = self.check(trade(PUT))
        self.assertFalse(ok)
        self.assertIn("allowed", why)

    def test_never_sell_what_you_dont_own(self):
        ok, why, _ = self.check(trade(CALL, side="sell"))
        self.assertFalse(ok)
        self.assertIn("never", why)
        ok, _, _ = self.check(trade(CALL, side="sell"), opt_snap(held=[held(side="short")]))
        self.assertFalse(ok)
        self.assertFalse(self.check(trade(CALL, side="sell", qty=2), opt_snap(held=[held(qty=1)]))[0])

    def test_sell_held(self):
        ok, why, o = self.check(trade(CALL, side="sell"), opt_snap(held=[held()]))
        self.assertTrue(ok, why)
        self.assertEqual(o["limit_price"], 1.22)
        self.assertFalse(self.check(trade(CALL, side="sell"), opt_snap(held=[held(bid=0)]))[0])

    def test_premium_limit(self):
        ok, why, _ = self.check(trade(CALL, qty=2), options={"max_premium_per_trade_usd": 150})
        self.assertFalse(ok)
        self.assertIn("per-trade", why)

    def test_total_options_cap(self):
        ok, why, _ = self.check(trade(CALL), opt_snap(paid=950))  # 950 + 98 > 10% of $10k
        self.assertFalse(ok)
        self.assertIn("10%", why)

    def test_buying_power(self):
        self.assertFalse(self.check(trade(CALL), opt_snap(bp=50))[0])

    def test_contract_and_daily_limits(self):
        self.assertFalse(self.check(trade(CALL, qty=3))[0])
        self.assertFalse(self.check(trade(CALL), buys=3)[0])

    def test_market_closed(self):
        self.assertFalse(self.check(trade(CALL), market_open=False)[0])

    def test_expiring_sells(self):
        snap = opt_snap(held=[held(dte=1), held(PUT, dte=30), held(NEAR, dte=2, side="short")])
        self.assertEqual([t["symbol"] for t in pt.expiring_sells(snap, settings())], [CALL])


class OptionsCommandTests(TempData):
    def run_options(self, broker, snap, plan, answers, s=None):
        with mock.patch.object(pt, "Broker", return_value=broker), \
             mock.patch.object(pt, "load_settings", return_value=s or settings()), \
             mock.patch.object(pt, "options_snapshot", return_value=copy.deepcopy(snap)), \
             mock.patch.object(pt, "ask_claude", return_value=plan), \
             mock.patch.object(builtins, "input", side_effect=answers), redirect_stdout(io.StringIO()) as out:
            pt.options_cmd()
        return out.getvalue()

    def broker(self, level=3, **kw):
        b = FakeBroker(**kw)
        b.account = lambda: {"options_trading_level": level}
        b.option_orders = []
        b.place_option_order = lambda *a: b.option_orders.append(a[:4]) or {"id": "x", "status": "accepted"}
        return b

    def test_approved_buy_only(self):
        b = self.broker()
        plan = {"outlook": "o", "trades": [trade(CALL), trade(PUT), trade(CALL, qty=1)]}
        out = self.run_options(b, opt_snap(), plan, ["y", "n"])
        self.assertEqual(b.option_orders, [(CALL, "buy", 1, 0.98)])
        self.assertIn("AAPL $200 CALL", out)

    def test_expiring_contract_offered_for_sale_first(self):
        b = self.broker()
        snap = opt_snap(held=[held(dte=1)])
        plan = {"outlook": "o", "trades": [trade(CALL, side="sell")]}  # duplicate of the forced sell
        self.run_options(b, snap, plan, ["y"])
        self.assertEqual(b.option_orders, [(CALL, "sell", 1, 1.22)])

    def test_unapproved_live_account_stops(self):
        b = self.broker(level=1, mode="live")
        out = self.run_options(b, opt_snap(), {"outlook": "", "trades": []}, ["LIVE"], settings(mode="live"))
        self.assertIn("isn't approved", out)
        self.assertEqual(b.option_orders, [])

    def test_market_closed_places_nothing(self):
        b = self.broker(is_open=False)
        self.run_options(b, opt_snap(), {"outlook": "", "trades": [trade(CALL)]}, [])
        self.assertEqual(b.option_orders, [])
