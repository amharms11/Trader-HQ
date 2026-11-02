"""Run with: python -m unittest test_market.py  (no network, no keys needed)"""
import copy
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
import market as M
import trader as T

NOW = dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


ASSETS = {
    "us_equity": [
        {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "class": "us_equity", "exchange": "NASDAQ", "tradable": True, "fractionable": True, "shortable": True},
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "class": "us_equity", "exchange": "ARCA", "tradable": True, "fractionable": True, "shortable": True},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust, Series 1", "class": "us_equity", "exchange": "NASDAQ", "tradable": True, "fractionable": True, "shortable": True},
        {"symbol": "TINY", "name": "Tiny Corp Class A Common Stock", "class": "us_equity", "exchange": "NYSE", "tradable": True, "fractionable": False, "shortable": False},
        {"symbol": "OTCX", "name": "Pink Sheet Co", "class": "us_equity", "exchange": "OTC", "tradable": True},
        {"symbol": "DEAD", "name": "Not Tradable Inc", "class": "us_equity", "exchange": "NYSE", "tradable": False},
    ],
    "crypto": [{"symbol": "BTC/USD", "name": "Bitcoin / US Dollar", "class": "crypto", "exchange": "CRYPTO", "tradable": True, "fractionable": True}],
}


def snap(price, prev, open_=None, high=None, vol=1000, prev_vol=500):
    return {"latestTrade": {"p": price, "t": iso(NOW)}, "latestQuote": {"bp": price - 0.01, "ap": price + 0.01},
            "dailyBar": {"o": open_ or prev, "h": high or price, "l": min(price, prev), "c": price, "v": vol, "vw": price},
            "prevDailyBar": {"c": prev, "v": prev_vol}}


SNAPS = {"AAPL": snap(210, 200, 205, 211, 5000, 2000), "SPY": snap(500, 505, 504), "QQQ": snap(400, 400),
         "TINY": snap(5.5, 5.0, 5.3, 5.6, 900, 100)}


class FakeBroker:
    mode = "paper"

    def __init__(self):
        self.calls, self.orders, self.deleted = [], [], []
        self.cash = 10_000

    def _api(self, path, params=None, body=None, method="GET"):
        self.calls.append((method, path, params))
        if path == "/v2/assets":
            return copy.deepcopy(ASSETS[params["asset_class"]])
        if path == "/v2/orders" and method == "POST":
            self.orders.append(body)
            return {"id": "ord1", "status": "accepted"}
        if path == "/v2/orders":
            return [{"id": "o1", "symbol": "AAPL", "side": "buy", "qty": "1", "type": "limit", "order_class": "bracket",
                     "status": "filled", "filled_qty": "1", "filled_avg_price": "205", "time_in_force": "day",
                     "legs": [{"id": "l1", "type": "limit", "side": "sell", "limit_price": "220", "status": "new"}]}]
        if path.startswith("/v2/positions/") and method == "DELETE":
            self.deleted.append(path)
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def _req(self, base, path, params=None, body=None, method="GET"):
        self.calls.append(("DATA", path, params))
        syms = (params or {}).get("symbols", "").split(",")
        if path == "/v2/stocks/snapshots":
            return {s: SNAPS[s] for s in syms if s in SNAPS}
        if path == "/v1beta3/crypto/us/snapshots":
            return {"snapshots": {"BTC/USD": snap(60000, 59000)}}
        if path in ("/v2/stocks/bars", "/v1beta3/crypto/us/bars"):
            sym = syms[0]
            if params["timeframe"] == "5Min":  # two sessions: only the last should be kept
                days = [NOW - dt.timedelta(days=1), NOW]
                bars = [{"t": iso(d.replace(hour=15, minute=m)), "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10} for d in days for m in (0, 5)]
            else:
                bars = [{"t": iso(NOW - dt.timedelta(days=300 - i)), "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1000 + i}
                        for i in range(0, 300, 10)]
            return {"bars": {sym: bars}, "next_page_token": None}
        if path == "/v1beta1/news":
            return {"news": [{"headline": "Apple does a thing", "source": "benzinga", "url": "https://example.com/a",
                              "summary": "s", "created_at": iso(NOW), "images": [{"size": "thumb", "url": "https://img/x.jpg"}]}]}
        if path == "/v1beta1/screener/stocks/movers":
            return {"gainers": [{"symbol": "TINY", "percent_change": 10, "change": 0.5, "price": 5.5}], "losers": [], "last_updated": "x"}
        if path == "/v1beta1/screener/stocks/most-actives":
            return {"most_actives": [{"symbol": "AAPL", "volume": 5000, "trade_count": 40}], "last_updated": "x"}
        raise AssertionError(f"unexpected data {path}")

    def account(self):
        return {"cash": str(self.cash), "equity": "10000", "last_equity": "10000"}

    def positions(self):
        return [{"symbol": "AAPL", "qty": "3", "asset_class": "us_equity", "avg_entry_price": "190", "current_price": "210",
                 "market_value": "630", "unrealized_pl": "60", "unrealized_plpc": "0.1", "cost_basis": "570"},
                {"symbol": "BTCUSD", "qty": "0.01", "asset_class": "crypto", "avg_entry_price": "50000", "current_price": "60000",
                 "market_value": "600", "unrealized_pl": "100", "unrealized_plpc": "0.2", "cost_basis": "500"}]

    def clock(self):
        return {"is_open": True, "timestamp": iso(NOW), "next_close": iso(NOW + dt.timedelta(hours=3))}

    def open_orders(self):
        return []


def concept(unit, rows):
    return {"units": {unit: rows}}


def fy(year, val, form="10-K"):
    return {"start": f"{year - 1}-10-01", "end": f"{year}-09-30", "val": val, "form": form, "filed": f"{year}-11-01", "fy": year, "fp": "FY"}


def fake_http(url, headers=None, timeout=20):
    if "company_tickers.json" in url:
        return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    if "submissions/CIK0000320193" in url:
        return {"name": "Apple Inc.", "sicDescription": "ELECTRONIC COMPUTERS", "website": "", "investorWebsite": "https://investor.apple.com",
                "fiscalYearEnd": "0928", "stateOfIncorporation": "CA", "addresses": {"business": {"city": "CUPERTINO", "stateOrCountry": "CA"}}}
    if "us-gaap/Revenues.json" in url:
        return {}
    if "RevenueFromContractWithCustomerExcludingAssessedTax" in url:
        quarter = {"start": "2024-07-01", "end": "2024-09-30", "val": 1, "form": "10-K", "filed": "2024-11-01"}
        return concept("USD", [fy(2022, 394e9), fy(2023, 383e9), fy(2024, 391e9), quarter, fy(2021, 365e9), fy(2020, 274e9)])
    if "NetIncomeLoss" in url:
        return concept("USD", [fy(2023, 97e9), fy(2024, 93.7e9)])
    if "EarningsPerShareDiluted" in url:
        return concept("USD/shares", [fy(2023, 6.13), fy(2024, 6.08)])
    if "EntityCommonStockSharesOutstanding" in url:
        return concept("shares", [{"end": "2024-10-18", "val": 15.1e9, "filed": "2024-11-01"}, {"end": "2024-07-19", "val": 15.2e9, "filed": "2024-08-02"}])
    if "search/title" in url:
        return {"pages": [{"key": "Apple_Inc.", "title": "Apple Inc."}]}
    if "page/summary/Apple_Inc." in url:
        return {"type": "standard", "title": "Apple Inc.", "extract": "Apple Inc. is an American technology company.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Apple_Inc."}}}
    if "page/summary" in url:
        return {"type": "disambiguation", "extract": "x"}
    raise RuntimeError("not found")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(T, "DATA", self.tmp), mock.patch.object(T, "JOURNAL_FILE", self.tmp / "j.jsonl"),
                        mock.patch.object(T, "SETTINGS_FILE", self.tmp / "s.json"), mock.patch.object(T, "CONFIG_FILE", self.tmp / "c.env"),
                        mock.patch.object(T, "SOFI_CONFIG", self.tmp / "n.env"), mock.patch.object(T, "claude_backend", return_value="api"),
                        mock.patch.dict(os.environ, {"ALPACA_PAPER_KEY_ID": "PK1", "ALPACA_PAPER_SECRET_KEY": "s"}, clear=True)]
        for p in self.patches:
            p.start()
        self.b = FakeBroker()
        self.mk = M.Market(lambda: self.b, http=fake_http)

    def tearDown(self):
        for p in self.patches:
            p.stop()


class DataTests(Base):
    def test_assets_classified_and_filtered(self):
        self.mk.ensure_assets()
        self.assertEqual(self.mk.db.counts(), {"stock": 2, "etf": 2, "crypto": 1})
        self.assertEqual(self.mk.db.asset("QQQ")["kind"], "etf")
        self.assertIsNone(self.mk.db.asset("OTCX"))
        self.assertIsNone(self.mk.db.asset("DEAD"))
        calls = len(self.b.calls)
        self.mk.ensure_assets()  # cached for a day
        self.assertEqual(len(self.b.calls), calls)

    def test_list_and_search(self):
        r = self.mk.list_assets("stock", "", 0, 50)
        self.assertEqual([x["symbol"] for x in r["rows"]], ["AAPL", "TINY"])
        self.assertAlmostEqual(r["rows"][0]["quote"]["change_pct"], 5.0)
        self.assertEqual([x["symbol"] for x in self.mk.search("apple")], ["AAPL"])
        self.assertEqual(self.mk.search("SP")[0]["symbol"], "SPY")
        self.assertEqual(self.mk.list_assets("crypto", "btc")["rows"][0]["quote"]["price"], 60000)

    def test_snapshot_summary(self):
        q = M.summarize_snapshot(SNAPS["AAPL"])
        self.assertEqual((q["price"], q["prev_close"], q["open"], q["volume"]), (210, 200, 205, 5000))
        self.assertAlmostEqual(q["change"], 10)
        self.assertEqual(M.summarize_snapshot({})["change_pct"], None)

    def test_bars_1d_keeps_latest_session_and_caches(self):
        r = self.mk.bars("AAPL", "1D")
        self.assertEqual((r["timeframe"], len(r["bars"])), ("5Min", 2))
        calls = len(self.b.calls)
        self.mk.bars("AAPL", "1D")
        self.assertEqual(len(self.b.calls), calls)
        y = self.mk.bars("AAPL", "1Y")
        self.assertEqual(y["timeframe"], "1Day")
        bars_call = [c for c in self.b.calls if c[1] == "/v2/stocks/bars"][-1][2]
        self.assertEqual((bars_call["feed"], bars_call["adjustment"]), ("iex", "all"))
        with self.assertRaises(ValueError):
            self.mk.bars("AAPL", "10Y")

    def test_crypto_bars_path(self):
        self.mk.bars("BTC/USD", "1M")
        self.assertIn("/v1beta3/crypto/us/bars", [c[1] for c in self.b.calls])

    def test_info_with_sec_and_wikipedia(self):
        info = self.mk.info("AAPL")
        c = info["company"]
        self.assertEqual(c["name"], "Apple Inc.")
        self.assertEqual(c["profile"]["industry"], "Electronic Computers")
        self.assertEqual(c["profile"]["headquarters"], "Cupertino, CA")
        self.assertEqual(c["profile"]["fiscal_year_end"], "09/28")
        self.assertEqual([f["year_end"] for f in c["financials"]], ["2021-09-30", "2022-09-30", "2023-09-30", "2024-09-30"])
        self.assertEqual(c["financials"][-1]["eps_diluted"], 6.08)
        self.assertEqual(c["facts"]["shares_outstanding"], 15.1e9)
        self.assertIn("technology company", c["about"])
        self.assertAlmostEqual(info["stats"]["market_cap"], 210 * 15.1e9)
        self.assertAlmostEqual(info["stats"]["pe_ratio"], 210 / 6.08)
        self.assertIn("high_52w", info["stats"])
        with self.assertRaises(ValueError):
            self.mk.info("NOPE")

    def test_company_sources_fail_gracefully(self):
        mk = M.Market(lambda: self.b, http=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
        c = mk.info("TINY")["company"]
        self.assertEqual(c["name"], "Tiny Corp")
        self.assertTrue(any("SEC data unavailable" in n for n in c["notes"]))

    def test_news_and_screeners(self):
        n = self.mk.news("BTC/USD")
        self.assertEqual(n[0]["image"], "https://img/x.jpg")
        self.assertEqual([c for c in self.b.calls if c[1] == "/v1beta1/news"][-1][2]["symbols"], "BTCUSD")
        self.assertEqual(self.mk.movers()["gainers"][0]["symbol"], "TINY")
        self.assertEqual(self.mk.most_actives()["rows"][0]["volume"], 5000)

    def test_scan_filters_and_sort(self):
        r = self.mk.scan("stock", {"change_min": 3})
        self.assertEqual([x["symbol"] for x in r["rows"]], ["TINY", "AAPL"])
        self.assertEqual(r["scanned"], 2)
        r = self.mk.scan("stock", {"rel_volume_min": 5, "price_max": "10"})
        self.assertEqual([x["symbol"] for x in r["rows"]], ["TINY"])
        r = self.mk.scan("etf", {"gap_max": -0.1}, sort="-gap_pct")
        self.assertEqual([x["symbol"] for x in r["rows"]], ["SPY"])
        self.assertEqual(self.mk.scan("stock", {"near_high_pct": 1}, sort="volume")["rows"][0]["symbol"], "AAPL")
        with self.assertRaises(ValueError):
            self.mk.scan("bonds", {})

    def test_name_cleanup_and_etf_detection(self):
        self.assertEqual(M.clean_name("Alphabet Inc. Class A Common Stock"), "Alphabet Inc.")
        self.assertEqual(M.classify({"class": "us_equity", "name": "iShares Core S&P 500 ETF"}), "etf")
        self.assertEqual(M.classify({"class": "us_equity", "name": "Microsoft Corporation Common Stock"}), "stock")


class OrderTests(Base):
    def order(self, spec, held=0, cash=10_000, quote=None, symbol="AAPL"):
        self.mk.ensure_assets()
        asset = self.mk.db.asset(symbol)
        return M.build_order(spec, asset, quote or {"price": 210}, held, T.load_settings(), cash)

    def test_market_and_limit(self):
        body, info = self.order({"side": "buy", "type": "market", "qty": 2})
        self.assertEqual((body["type"], body["qty"], body["time_in_force"]), ("market", "2", "day"))
        self.assertAlmostEqual(info["estimate"], 2 * 210 * 1.01)
        body, _ = self.order({"side": "buy", "type": "limit", "qty": 1, "limit_price": 205.5, "extended_hours": True})
        self.assertEqual((body["limit_price"], body["extended_hours"]), ("205.50", True))

    def test_dollar_amount_and_fractional(self):
        body, _ = self.order({"side": "buy", "type": "market", "notional": 50})
        self.assertEqual(body["notional"], "50.00")
        self.assertNotIn("qty", body)
        with self.assertRaises(ValueError):
            self.order({"side": "buy", "type": "limit", "notional": 50, "limit_price": 1})
        with self.assertRaises(ValueError):  # TINY isn't fractional
            self.order({"side": "buy", "type": "market", "qty": 0.5}, symbol="TINY")
        self.assertEqual(self.order({"side": "buy", "type": "market", "qty": 0.5})[0]["qty"], "0.5")

    def test_stop_stop_limit_trailing(self):
        body, _ = self.order({"side": "sell", "type": "stop", "qty": 3, "stop_price": 195}, held=3)
        self.assertEqual(body["stop_price"], "195.00")
        body, _ = self.order({"side": "sell", "type": "stop_limit", "qty": 1, "stop_price": 195, "limit_price": 194}, held=3)
        self.assertEqual((body["stop_price"], body["limit_price"]), ("195.00", "194.00"))
        body, _ = self.order({"side": "sell", "type": "trailing_stop", "qty": 1, "trail_percent": 3, "time_in_force": "gtc"}, held=3)
        self.assertEqual(body["trail_percent"], "3")
        with self.assertRaises(ValueError):
            self.order({"side": "sell", "type": "trailing_stop", "qty": 1, "trail_percent": 3, "trail_price": 2}, held=3)
        with self.assertRaises(ValueError):
            self.order({"side": "sell", "type": "stop", "qty": 1}, held=3)

    def test_bracket(self):
        body, info = self.order({"side": "buy", "type": "limit", "qty": 2, "limit_price": 208, "order_class": "bracket",
                                 "take_profit": 230, "stop_loss": 200, "stop_loss_limit": 199})
        self.assertEqual(body["order_class"], "bracket")
        self.assertEqual(body["take_profit"], {"limit_price": "230.00"})
        self.assertEqual(body["stop_loss"], {"stop_price": "200.00", "limit_price": "199.00"})
        self.assertIn("take-profit", info["summary"])
        for bad in ({"take_profit": 200, "stop_loss": 190}, {"take_profit": 230, "stop_loss": 215}, {"take_profit": 230}):
            with self.assertRaises(ValueError):
                self.order({"side": "buy", "type": "limit", "qty": 2, "limit_price": 208, "order_class": "bracket", **bad})
        with self.assertRaises(ValueError):  # bracket entries are market or limit
            self.order({"side": "buy", "type": "stop", "qty": 1, "stop_price": 220, "order_class": "bracket", "take_profit": 240, "stop_loss": 200})

    def test_oco_protects_shares_owned(self):
        body, info = self.order({"side": "sell", "type": "limit", "qty": 3, "order_class": "oco", "take_profit": 230, "stop_loss": 195}, held=3)
        self.assertEqual((body["order_class"], body["type"]), ("oco", "limit"))
        self.assertNotIn("limit_price", body)
        self.assertIn("whichever comes first", info["summary"])
        with self.assertRaises(ValueError):
            self.order({"side": "buy", "type": "limit", "qty": 3, "order_class": "oco", "take_profit": 230, "stop_loss": 195})
        with self.assertRaises(ValueError):
            self.order({"side": "sell", "type": "limit", "qty": 4, "order_class": "oco", "take_profit": 230, "stop_loss": 195}, held=3)

    def test_oto(self):
        body, _ = self.order({"side": "buy", "type": "market", "qty": 1, "order_class": "oto", "stop_loss": 200})
        self.assertEqual(body["stop_loss"], {"stop_price": "200.00"})
        self.assertNotIn("take_profit", body)
        with self.assertRaises(ValueError):
            self.order({"side": "buy", "type": "market", "qty": 1, "order_class": "oto"})

    def test_safety_limits(self):
        with self.assertRaises(ValueError) as e:
            self.order({"side": "buy", "type": "market", "qty": 10})  # ~$2,121 > $1,000 limit
        self.assertIn("limit", str(e.exception))
        with self.assertRaises(ValueError):
            self.order({"side": "buy", "type": "market", "qty": 2}, cash=100)
        with self.assertRaises(ValueError) as e:
            self.order({"side": "sell", "type": "market", "qty": 1}, held=0)
        self.assertIn("short", str(e.exception))
        with self.assertRaises(ValueError):
            self.order({"side": "buy", "type": "market", "qty": 1}, quote={"price": None})

    def test_crypto_rules(self):
        body, _ = self.order({"side": "buy", "type": "market", "qty": 0.001}, symbol="BTC/USD", quote={"price": 60000})
        self.assertEqual(body["time_in_force"], "gtc")
        for bad in ({"type": "stop", "stop_price": 1}, {"type": "limit", "limit_price": 1, "order_class": "bracket", "take_profit": 2, "stop_loss": 0.5},
                    {"type": "market", "time_in_force": "day"}):
            with self.assertRaises(ValueError):
                self.order({"side": "buy", "qty": 0.001, **bad}, symbol="BTC/USD", quote={"price": 60000})


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.app = D.App(broker_factory=lambda mode: self.b)
        self.app.market = self.mk
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
                ctype = resp.headers["Content-Type"]
                data = resp.read()
                return resp.status, json.loads(data) if "json" in ctype else data
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_static_files(self):
        for path in ("/market.js", "/vendor/lightweight-charts.js"):
            code, data = self.req(path)
            self.assertEqual(code, 200)
            self.assertGreater(len(data), 1000)
        self.assertEqual(self.req("/desktop.py")[0], 404)

    def test_market_endpoints(self):
        self.assertEqual(self.req("/api/market/assets?kind=etf&q=s%26p")[1]["rows"][0]["symbol"], "SPY")
        self.assertEqual(self.req("/api/market/search?q=BTC")[1]["rows"][0]["symbol"], "BTC/USD")
        self.assertIn("BTC/USD", self.req("/api/market/quote?symbols=BTC%2FUSD,AAPL")[1]["quotes"])
        self.assertEqual(self.req("/api/market/bars?symbol=AAPL&range=1D")[1]["timeframe"], "5Min")
        info = self.req("/api/market/info?symbol=BTC%2FUSD")[1]
        self.assertEqual(info["position"]["qty"], 0.01)  # crypto positions come back without the slash
        self.assertEqual(self.req("/api/market/info?symbol=AAPL")[1]["position"]["qty"], 3)
        self.assertEqual(self.req("/api/market/movers")[1]["gainers"][0]["symbol"], "TINY")
        self.assertEqual(self.req("/api/market/scan", {"kind": "stock", "filters": {"change_min": 8}})[1]["rows"][0]["symbol"], "TINY")
        self.assertEqual(self.req("/api/orders/recent")[1]["rows"][0]["legs"][0]["limit_price"], "220")
        self.assertEqual(self.req("/api/market/bars?symbol=AAPL&range=bad")[0], 400)

    def test_preview_then_place(self):
        spec = {"symbol": "AAPL", "side": "buy", "type": "limit", "qty": 1, "limit_price": 205}
        code, p = self.req("/api/orders/preview", {"order": spec})
        self.assertEqual((code, p["confirm"]), (200, True))
        self.assertEqual(self.b.orders, [])
        code, r = self.req("/api/orders", {"order": spec})
        self.assertEqual((code, r["status"]), (200, "accepted"))
        self.assertIn("took_ms", r)
        self.assertEqual(self.b.orders[0]["limit_price"], "205.00")
        self.assertTrue(self.b.orders[0]["client_order_id"].startswith("amhm-"))
        code, err = self.req("/api/orders", {"order": {**spec, "side": "sell", "qty": 9}})
        self.assertEqual(code, 400)
        self.assertIn("own 3", err["error"])

    def test_live_orders_need_live_and_review(self):
        os.environ["ALPACA_LIVE_KEY_ID"] = "AK1"
        D.save_settings({"mode": "live"})
        spec = {"symbol": "AAPL", "side": "buy", "type": "market", "qty": 1}
        self.assertEqual(self.req("/api/market/info?symbol=AAPL")[0], 200)  # reading data is fine
        code, err = self.req("/api/orders", {"order": spec})
        self.assertEqual((code, err.get("need_live")), (409, True))
        self.app.confirm_live("LIVE")
        self.assertEqual(self.req("/api/orders", {"order": spec})[0], 400)  # must use the review step
        self.assertEqual(self.req("/api/orders", {"order": {**spec, "confirmed": True}})[0], 200)
        self.assertEqual(len(self.b.orders), 1)

    def test_close_position(self):
        self.assertEqual(self.req("/api/positions/close", {"symbol": "BTC/USD"})[0], 200)
        self.assertEqual(self.b.deleted, ["/v2/positions/BTCUSD"])
        self.assertEqual(self.req("/api/positions/close", {"symbol": "../v2/account"})[0], 400)

    def test_settings_market_section(self):
        self.assertEqual(self.req("/api/settings", {"settings": {"market": {"data_feed": "sip", "max_manual_order_usd": "2500",
                                                                            "sec_contact_email": ""}}})[0], 200)
        m = T.load_settings()["market"]
        self.assertEqual((m["data_feed"], m["max_manual_order_usd"]), ("sip", 2500))
        self.assertEqual(self.req("/api/settings", {"settings": {"market": {"data_feed": "fast"}}})[0], 400)
        self.assertEqual(self.req("/api/settings", {"settings": {"market": {"sec_contact_email": "nope"}}})[0], 400)
        self.assertEqual(self.req("/api/finnhub-key", {"key": "bad key!"})[0], 400)
        self.assertEqual(self.req("/api/finnhub-key", {"key": "abcdef1234567890"})[0], 200)
        self.assertIn("FINNHUB_API_KEY=abcdef1234567890", T.CONFIG_FILE.read_text())


if __name__ == "__main__":
    unittest.main()
