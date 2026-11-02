"""
Market data and manual orders for the Trader HQ dashboard.

- Asset lists (stocks, ETFs, crypto) from Alpaca, stored in ~/.amh-trader/market.db (SQLite)
- Quotes, chart bars and news from Alpaca's market data API
- Company facts from SEC EDGAR (free), descriptions from Wikipedia, and optional extras from Finnhub
- Screeners: Alpaca's movers / most-active lists and a custom scan over every listed stock or ETF
- Orders you place yourself: market, limit, stop, stop-limit, trailing stop, bracket, OCO and OTO,
  checked against your limits before they are sent

None of this text is ever sent to Claude; the trading bot still only sees prices and account numbers.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import trader as T

STOCK_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA"}
ETF_WORDS = re.compile(r"\b(ETF|ETN|ETFs|iShares|SPDR|ProShares|Direxion|Invesco QQQ|Select Sector|"
                       r"Index Fund|Exchange[- ]Traded|VanEck|WisdomTree|Global X|ARK .* ETF|Vanguard .* (Index|Fund))\b",
                       re.I)
NAME_NOISE = re.compile(r"\s+(Common Stock|Ordinary Shares|Class [A-C]( Common Stock| Ordinary Shares)?|"
                        r"American Depositary Shares.*|Depositary Shares.*|Common Shares.*|Units?|"
                        r"Shares of Beneficial Interest)$", re.I)

# range -> (bar size, how far back). "1D" keeps only the latest session.
RANGES = {
    "1D": ("5Min", dt.timedelta(days=5)),
    "1W": ("30Min", dt.timedelta(days=7)),
    "1M": ("1Hour", dt.timedelta(days=31)),
    "3M": ("1Day", dt.timedelta(days=92)),
    "YTD": ("1Day", None),
    "1Y": ("1Day", dt.timedelta(days=366)),
    "5Y": ("1Week", dt.timedelta(days=5 * 366)),
    "ALL": ("1Month", dt.timedelta(days=40 * 366)),
}
ORDER_TYPES = ("market", "limit", "stop", "stop_limit", "trailing_stop")
ORDER_CLASSES = ("simple", "bracket", "oco", "oto")
USER_AGENT = "TraderHQ/1.0 (personal research app)"


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def is_crypto(symbol):
    return "/" in symbol


def classify(asset):
    if asset.get("class") == "crypto":
        return "crypto"
    return "etf" if ETF_WORDS.search(asset.get("name") or "") else "stock"


def clean_name(name):
    name = (name or "").strip()
    for _ in range(2):
        name = NAME_NOISE.sub("", name).strip(" ,-")
    return name


def iso_to_unix(ts):
    return int(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def num(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def summarize_snapshot(snap):
    """Alpaca snapshot -> the numbers the dashboard shows."""
    lt, lq = snap.get("latestTrade") or {}, snap.get("latestQuote") or {}
    d, p = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
    price = num(lt.get("p")) or num(d.get("c"))
    prev = num(p.get("c"))
    change = price - prev if price is not None and prev else None
    return {
        "price": price, "prev_close": prev, "change": change,
        "change_pct": change / prev * 100 if change is not None and prev else None,
        "open": num(d.get("o")), "high": num(d.get("h")), "low": num(d.get("l")),
        "volume": num(d.get("v")), "prev_volume": num(p.get("v")), "vwap": num(d.get("vw")),
        "bid": num(lq.get("bp")), "ask": num(lq.get("ap")), "time": lt.get("t") or d.get("t"),
    }


# ---------- local database ----------

class MarketDB:
    """SQLite file holding the asset lists and cached data (company facts, charts, scans)."""

    def __init__(self, path):
        path.parent.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS assets (
                    symbol TEXT PRIMARY KEY, name TEXT, kind TEXT, asset_class TEXT, exchange TEXT,
                    tradable INTEGER, fractionable INTEGER, shortable INTEGER, attributes TEXT);
                CREATE INDEX IF NOT EXISTS assets_kind ON assets(kind, symbol);
                CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, updated REAL);
            """)

    def __del__(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def get(self, key, max_age):
        with self.lock:
            row = self.conn.execute("SELECT value, updated FROM cache WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[1] < max_age:
            return json.loads(row[0])
        return None

    def put(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value), time.time()))
            self.conn.commit()

    def replace_assets(self, rows):
        with self.lock:
            self.conn.execute("DELETE FROM assets")
            self.conn.executemany("INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()

    def asset(self, symbol):
        with self.lock:
            row = self.conn.execute("SELECT * FROM assets WHERE symbol=?", (symbol,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(r):
        return {"symbol": r[0], "name": r[1], "kind": r[2], "asset_class": r[3], "exchange": r[4],
                "tradable": bool(r[5]), "fractionable": bool(r[6]), "shortable": bool(r[7]),
                "attributes": json.loads(r[8] or "[]")}

    def counts(self):
        with self.lock:
            return dict(self.conn.execute("SELECT kind, COUNT(*) FROM assets GROUP BY kind").fetchall())

    def search(self, kind, q, offset, limit):
        q = (q or "").strip().upper()
        where, args = [], []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if q:
            where.append("(symbol LIKE ? OR UPPER(name) LIKE ?)")
            args += [q.replace("%", "") + "%", "%" + q.replace("%", "") + "%"]
        sql = " WHERE " + " AND ".join(where) if where else ""
        order = (" ORDER BY CASE WHEN symbol=? THEN 0 WHEN symbol LIKE ? THEN 1 ELSE 2 END, LENGTH(symbol), symbol"
                 if q else " ORDER BY symbol")
        with self.lock:
            total = self.conn.execute("SELECT COUNT(*) FROM assets" + sql, args).fetchone()[0]
            rows = self.conn.execute("SELECT * FROM assets" + sql + order + " LIMIT ? OFFSET ?",
                                     args + ([q, q + "%"] if q else []) + [limit, offset]).fetchall()
        return total, [self._row(r) for r in rows]

    def symbols(self, kind):
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT symbol FROM assets WHERE kind=? ORDER BY symbol", (kind,))]


# ---------- data sources ----------

class Market:
    def __init__(self, broker_factory, settings_fn=T.load_settings, http=None):
        self.broker_factory = broker_factory   # () -> Broker for reading data
        self.settings = settings_fn
        self.http = http or self._http
        self._db = None
        self.scan_lock = threading.Lock()

    @property
    def db(self):
        if self._db is None:
            self._db = MarketDB(T.DATA / "market.db")
        return self._db

    @staticmethod
    def _http(url, headers=None, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                   **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or "null")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc} returned {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"couldn't reach {urllib.parse.urlsplit(url).netloc} ({e})") from None

    def feed(self):
        return self.settings()["market"]["data_feed"]

    def data(self, path, params=None):
        return self.broker_factory()._req(T.DATA_URL, path, params)

    # --- assets ---
    def ensure_assets(self, force=False):
        if not force and self.db.get("assets_loaded", 24 * 3600):
            return
        b = self.broker_factory()
        rows = []
        for cls in ("us_equity", "crypto"):
            for a in b._api("/v2/assets", params={"status": "active", "asset_class": cls}) or []:
                if not a.get("tradable"):
                    continue
                if cls == "us_equity" and a.get("exchange") not in STOCK_EXCHANGES:
                    continue  # skip over-the-counter listings
                rows.append((a["symbol"], a.get("name") or a["symbol"], classify(a), a.get("class", cls),
                             a.get("exchange"), int(bool(a.get("tradable"))), int(bool(a.get("fractionable"))),
                             int(bool(a.get("shortable"))), json.dumps(a.get("attributes") or [])))
        if rows:
            self.db.replace_assets(rows)
            self.db.put("assets_loaded", time.time())

    def list_assets(self, kind, q, offset=0, limit=50):
        self.ensure_assets()
        total, rows = self.db.search(kind, q, offset, limit)
        quotes = self.quotes([r["symbol"] for r in rows]) if rows else {}
        for r in rows:
            r["quote"] = quotes.get(r["symbol"])
        return {"total": total, "rows": rows, "counts": self.db.counts()}

    def search(self, q, limit=10):
        self.ensure_assets()
        return self.db.search(None, q, 0, limit)[1]

    # --- quotes and charts ---
    def snapshots_raw(self, symbols):
        out = {}
        stocks = [s for s in symbols if not is_crypto(s)]
        cryptos = [s for s in symbols if is_crypto(s)]
        for part in chunks(stocks, 200):
            res = self.data("/v2/stocks/snapshots", {"symbols": ",".join(part), "feed": self.feed()}) or {}
            out.update({k: v for k, v in res.items() if isinstance(v, dict)})
        for part in chunks(cryptos, 100):
            res = self.data("/v1beta3/crypto/us/snapshots", {"symbols": ",".join(part)}) or {}
            out.update(res.get("snapshots") or {})
        return out

    def quotes(self, symbols):
        return {s: summarize_snapshot(v) for s, v in self.snapshots_raw(symbols).items() if v}

    def bars(self, symbol, rng):
        if rng not in RANGES:
            raise ValueError("unknown range")
        timeframe, back = RANGES[rng]
        ttl = 30 if timeframe.endswith(("Min", "Hour")) else 3600
        key = f"bars:{symbol}:{rng}:{self.feed()}"
        hit = self.db.get(key, ttl)
        if hit is not None:
            return hit
        now = dt.datetime.now(dt.timezone.utc)
        start = (dt.datetime(now.year, 1, 1, tzinfo=dt.timezone.utc) if back is None else now - back)
        if is_crypto(symbol):
            path, params = "/v1beta3/crypto/us/bars", {}
        else:
            path, params = "/v2/stocks/bars", {"feed": self.feed(), "adjustment": "all"}
        params.update({"symbols": symbol, "timeframe": timeframe, "start": start.isoformat().replace("+00:00", "Z"),
                       "limit": 10000, "sort": "asc"})
        bars = []
        for _ in range(20):
            res = self.data(path, params) or {}
            bars += (res.get("bars") or {}).get(symbol) or []
            if not res.get("next_page_token"):
                break
            params["page_token"] = res["next_page_token"]
        out = [{"t": iso_to_unix(b["t"]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0),
                "vw": b.get("vw")} for b in bars]
        if rng == "1D" and out:
            if is_crypto(symbol):
                out = [b for b in out if b["t"] >= out[-1]["t"] - 24 * 3600]
            else:  # only the latest trading session
                day = dt.datetime.fromtimestamp(out[-1]["t"], T.ET).date()
                out = [b for b in out if dt.datetime.fromtimestamp(b["t"], T.ET).date() == day]
        result = {"symbol": symbol, "range": rng, "timeframe": timeframe, "bars": out}
        self.db.put(key, result)
        return result

    def key_stats(self, symbol, quote, facts):
        year = self.bars(symbol, "1Y")["bars"]
        stats = {}
        if year:
            stats["high_52w"] = max(b["h"] for b in year)
            stats["low_52w"] = min(b["l"] for b in year)
            last30 = year[-30:]
            stats["avg_volume_30d"] = sum(b["v"] for b in last30) / len(last30)
        price = (quote or {}).get("price")
        shares = facts.get("shares_outstanding")
        if price and shares:
            stats["market_cap"] = price * shares
        eps = facts.get("eps_diluted")
        if price and eps and eps > 0:
            stats["pe_ratio"] = price / eps
        return stats

    def news(self, symbol, limit=12):
        res = self.data("/v1beta1/news", {"symbols": symbol.replace("/", ""), "limit": limit, "sort": "desc"}) or {}
        out = []
        for n in res.get("news") or []:
            imgs = n.get("images") or []
            thumb = next((i.get("url") for i in imgs if i.get("size") == "thumb"), None) or (imgs[0].get("url") if imgs else None)
            out.append({"headline": n.get("headline", ""), "source": n.get("source", ""), "url": n.get("url"),
                        "summary": n.get("summary", ""), "created_at": n.get("created_at"), "image": thumb})
        return out

    # --- company information ---
    def info(self, symbol):
        self.ensure_assets()
        asset = self.db.asset(symbol)
        if not asset:
            raise ValueError(f"{symbol} isn't a listed stock, ETF or crypto that Alpaca can trade.")
        quote = self.quotes([symbol]).get(symbol)
        company = self.company(asset)
        stats = self.key_stats(symbol, quote, company.get("facts", {}))
        stats.update({k: v for k, v in (company.get("finnhub_stats") or {}).items() if v is not None})
        return {"asset": asset, "quote": quote, "company": company, "stats": stats}

    def company(self, asset):
        key = f"company:{asset['symbol']}"
        hit = self.db.get(key, 7 * 24 * 3600)
        if hit is not None:
            return hit
        out = {"name": clean_name(asset["name"]), "facts": {}, "financials": [], "profile": {}, "sources": [],
               "notes": []}
        if asset["kind"] != "crypto":
            try:
                self.sec(asset["symbol"], out)
            except RuntimeError as e:
                out["notes"].append(f"SEC data unavailable: {e}")
        try:
            self.wikipedia(asset, out)
        except RuntimeError as e:
            out["notes"].append(f"Wikipedia unavailable: {e}")
        T.load_config()
        if os.environ.get("FINNHUB_API_KEY") and asset["kind"] != "crypto":
            try:
                self.finnhub(asset["symbol"], out)
            except RuntimeError as e:
                out["notes"].append(f"Finnhub unavailable: {e}")
        self.db.put(key, out)
        return out

    def sec_headers(self):
        email = self.settings()["market"].get("sec_contact_email", "").strip()
        return {"User-Agent": f"TraderHQ personal research app {email}".strip()}

    def sec(self, symbol, out):
        ciks = self.db.get("sec:tickers", 7 * 24 * 3600)
        if ciks is None:
            raw = self.http("https://www.sec.gov/files/company_tickers.json", self.sec_headers()) or {}
            ciks = {v["ticker"].upper(): v["cik_str"] for v in raw.values()}
            self.db.put("sec:tickers", ciks)
        cik = ciks.get(symbol.upper().replace(".", "-")) or ciks.get(symbol.upper())
        if not cik:
            out["notes"].append("No SEC filings found for this symbol (common for ETFs and foreign companies).")
            return
        cik10 = f"{int(cik):010d}"
        sub = self.http(f"https://data.sec.gov/submissions/CIK{cik10}.json", self.sec_headers()) or {}
        addr = (sub.get("addresses") or {}).get("business") or {}
        fy_end = sub.get("fiscalYearEnd") or ""
        out["profile"].update({
            "legal_name": sub.get("name"), "industry": (sub.get("sicDescription") or "").title() or None,
            "headquarters": ", ".join(x for x in (addr.get("city", "").title(), addr.get("stateOrCountry")) if x) or None,
            "website": sub.get("website") or sub.get("investorWebsite") or None,
            "fiscal_year_end": f"{fy_end[:2]}/{fy_end[2:]}" if len(fy_end) == 4 else None,
            "incorporated_in": sub.get("stateOfIncorporation") or None,
            "sec_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik10}",
        })
        out["sources"].append({"name": "SEC EDGAR", "url": out["profile"]["sec_url"]})

        def concept(taxonomy, tag):
            try:
                return self.http(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10}/{taxonomy}/{tag}.json",
                                 self.sec_headers()) or {}
            except RuntimeError:
                return {}

        def annual(data, unit):
            by_year = {}
            for e in (data.get("units") or {}).get(unit, []):
                if e.get("form") not in ("10-K", "20-F", "40-F") or not e.get("start") or not e.get("end"):
                    continue
                days = (dt.date.fromisoformat(e["end"]) - dt.date.fromisoformat(e["start"])).days
                if 350 <= days <= 380:
                    prev = by_year.get(e["end"])
                    if not prev or e.get("filed", "") > prev.get("filed", ""):
                        by_year[e["end"]] = e
            return {end: e["val"] for end, e in sorted(by_year.items())[-4:]}

        revenue = {}
        for tag in ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"):
            revenue = annual(concept("us-gaap", tag), "USD")
            if revenue:
                break
        income = annual(concept("us-gaap", "NetIncomeLoss"), "USD")
        eps = annual(concept("us-gaap", "EarningsPerShareDiluted"), "USD/shares")
        shares = (concept("dei", "EntityCommonStockSharesOutstanding").get("units") or {}).get("shares") or []
        if shares:
            out["facts"]["shares_outstanding"] = max(shares, key=lambda e: (e.get("end", ""), e.get("filed", "")))["val"]
        if eps:
            out["facts"]["eps_diluted"] = eps[max(eps)]
        years = sorted(set(revenue) | set(income) | set(eps))[-4:]
        out["financials"] = [{"year_end": y, "revenue": revenue.get(y), "net_income": income.get(y),
                              "eps_diluted": eps.get(y)} for y in years]

    def wikipedia(self, asset, out):
        if asset["kind"] == "crypto":
            query = (asset["name"] or asset["symbol"]).split("/")[0].strip()
        else:
            query = out["profile"].get("legal_name") or out["name"]
        if not query:
            return
        hits = self.http("https://en.wikipedia.org/w/rest.php/v1/search/title?" +
                         urllib.parse.urlencode({"q": query, "limit": 1})) or {}
        pages = hits.get("pages") or []
        if not pages:
            return
        summary = self.http("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                            urllib.parse.quote(pages[0]["key"], safe="")) or {}
        if summary.get("type") == "disambiguation" or not summary.get("extract"):
            return
        url = ((summary.get("content_urls") or {}).get("desktop") or {}).get("page")
        out["about"] = summary["extract"]
        out["about_title"] = summary.get("title")
        out["sources"].append({"name": "Wikipedia", "url": url})

    def finnhub(self, symbol, out):
        token = os.environ["FINNHUB_API_KEY"]
        base = "https://finnhub.io/api/v1"
        prof = self.http(f"{base}/stock/profile2?" + urllib.parse.urlencode({"symbol": symbol, "token": token})) or {}
        metric = (self.http(f"{base}/stock/metric?" + urllib.parse.urlencode(
            {"symbol": symbol, "metric": "all", "token": token})) or {}).get("metric") or {}
        if prof.get("logo"):
            out["profile"]["logo"] = prof["logo"]
        if prof.get("finnhubIndustry") and not out["profile"].get("industry"):
            out["profile"]["industry"] = prof["finnhubIndustry"]
        if prof.get("weburl"):
            out["profile"]["website"] = prof["weburl"]
        if prof.get("ipo"):
            out["profile"]["ipo_date"] = prof["ipo"]
        out["finnhub_stats"] = {
            "beta": num(metric.get("beta")),
            "dividend_yield_pct": num(metric.get("dividendYieldIndicatedAnnual") or metric.get("currentDividendYieldTTM")),
            "pe_ratio": num(metric.get("peTTM") or metric.get("peBasicExclExtraTTM")),
        }
        out["sources"].append({"name": "Finnhub", "url": "https://finnhub.io"})

    # --- screeners ---
    def movers(self, market="stocks", top=20):
        res = self.data(f"/v1beta1/screener/{market}/movers", {"top": top}) or {}
        return {"gainers": res.get("gainers") or [], "losers": res.get("losers") or [],
                "updated": res.get("last_updated")}

    def most_actives(self, by="volume", top=20):
        res = self.data("/v1beta1/screener/stocks/most-actives", {"by": by, "top": top}) or {}
        return {"rows": res.get("most_actives") or [], "updated": res.get("last_updated")}

    def scan(self, kind, filters, sort="change_pct", limit=100):
        """Check every listed stock (or ETF) against the filters, using one snapshot per symbol."""
        if kind not in ("stock", "etf", "crypto"):
            raise ValueError("unknown list")
        self.ensure_assets()
        with self.scan_lock:
            key = f"scan:{kind}:{self.feed()}"
            quotes = self.db.get(key, 60)
            if quotes is None:
                quotes = self.quotes(self.db.symbols(kind))
                self.db.put(key, quotes)
        f = {k: num(v) for k, v in (filters or {}).items() if num(v) is not None}
        rows = []
        for sym, q in quotes.items():
            price, vol = q.get("price"), q.get("volume") or 0
            if price is None:
                continue
            gap = (q["open"] / q["prev_close"] - 1) * 100 if q.get("open") and q.get("prev_close") else None
            rel = vol / q["prev_volume"] if q.get("prev_volume") else None
            checks = [
                ("price_min", price, lambda v, x: v >= x), ("price_max", price, lambda v, x: v <= x),
                ("change_min", q.get("change_pct"), lambda v, x: v >= x),
                ("change_max", q.get("change_pct"), lambda v, x: v <= x),
                ("volume_min", vol, lambda v, x: v >= x),
                ("dollar_volume_min", vol * price, lambda v, x: v >= x),
                ("gap_min", gap, lambda v, x: v >= x), ("gap_max", gap, lambda v, x: v <= x),
                ("rel_volume_min", rel, lambda v, x: v >= x),
                ("near_high_pct", (1 - price / q["high"]) * 100 if q.get("high") else None, lambda v, x: v <= x),
            ]
            if all(name not in f or (val is not None and test(val, f[name])) for name, val, test in checks):
                rows.append({"symbol": sym, **q, "gap_pct": gap, "rel_volume": rel})
        reverse = not sort.startswith("-")
        field = sort.lstrip("-")
        if field not in ("change_pct", "volume", "price", "gap_pct", "rel_volume"):
            field = "change_pct"
        rows.sort(key=lambda r: (r.get(field) is not None, r.get(field) or 0), reverse=reverse)
        names = {}
        for r in rows[:limit]:
            a = self.db.asset(r["symbol"])
            names[r["symbol"]] = a["name"] if a else ""
        return {"scanned": len(quotes), "matched": len(rows),
                "rows": [{**r, "name": names.get(r["symbol"], "")} for r in rows[:limit]]}


# ---------- orders you place yourself ----------

def fmt_price(p, crypto=False):
    if crypto:
        return f"{p:.9g}"
    return f"{p:.2f}" if p >= 1 else f"{p:.4f}"


def build_order(spec, asset, quote, position_qty, settings, cash):
    """Check an order from the order ticket and turn it into Alpaca's format.
    Returns (body, summary). Raises ValueError with a plain-English reason."""
    m = settings["market"]
    sym = asset["symbol"]
    crypto = asset["kind"] == "crypto"
    side = spec.get("side")
    otype = spec.get("type", "market")
    oclass = spec.get("order_class", "simple")
    tif = spec.get("time_in_force", "gtc" if crypto else "day")
    if not asset.get("tradable"):
        raise ValueError(f"{sym} can't be traded right now.")
    if side not in ("buy", "sell"):
        raise ValueError("Choose Buy or Sell.")
    if otype not in ORDER_TYPES:
        raise ValueError("Unknown order type.")
    if oclass not in ORDER_CLASSES:
        raise ValueError("Unknown order strategy.")
    if crypto and (otype not in ("market", "limit", "stop_limit") or oclass != "simple"):
        raise ValueError("Crypto supports market, limit and stop-limit orders only (no brackets or OCO).")
    if tif not in (("gtc", "ioc") if crypto else ("day", "gtc")):
        raise ValueError("Unsupported time in force.")

    qty, notional = num(spec.get("qty")), num(spec.get("notional"))
    if notional is not None and qty is None:
        if otype != "market" or oclass != "simple" or not (asset.get("fractionable") or crypto):
            raise ValueError("Dollar amounts work only for simple market orders on fractional-eligible assets.")
        if notional < 1:
            raise ValueError("The dollar amount must be at least $1.")
        if not crypto and tif != "day":
            raise ValueError("Dollar-amount stock orders must be Day orders.")
    elif qty is None or qty <= 0:
        raise ValueError("Enter how many shares (or coins) to trade.")
    elif not crypto and qty != int(qty):
        if not asset.get("fractionable") or oclass != "simple" or tif != "day":
            raise ValueError("Fractional shares need a fractional-eligible stock and a simple Day order.")

    price = (quote or {}).get("price")
    limit_p, stop_p = num(spec.get("limit_price")), num(spec.get("stop_price"))
    trail_pct, trail_px = num(spec.get("trail_percent")), num(spec.get("trail_price"))
    body = {"symbol": sym, "side": side, "type": otype, "time_in_force": tif,
            "client_order_id": f"amhm-{dt.datetime.now():%Y%m%d%H%M%S%f}"[:48]}
    if qty is not None:
        body["qty"] = f"{qty:g}"
    else:
        body["notional"] = f"{notional:.2f}"
    if otype in ("limit", "stop_limit") and oclass != "oco":  # OCO prices are its take-profit and stop-loss
        if not limit_p or limit_p <= 0:
            raise ValueError("Enter a limit price.")
        body["limit_price"] = fmt_price(limit_p, crypto)
    if otype in ("stop", "stop_limit"):
        if not stop_p or stop_p <= 0:
            raise ValueError("Enter a stop price.")
        body["stop_price"] = fmt_price(stop_p, crypto)
    if otype == "trailing_stop":
        if (trail_pct is None) == (trail_px is None) or (trail_pct or trail_px) <= 0:
            raise ValueError("Enter a trail as a percent or as a dollar amount (one of them).")
        body["trail_percent" if trail_pct is not None else "trail_price"] = f"{trail_pct if trail_pct is not None else trail_px:g}"
    if spec.get("extended_hours"):
        if otype != "limit" or tif != "day" or oclass != "simple" or crypto:
            raise ValueError("Extended-hours trading needs a simple Day limit order.")
        body["extended_hours"] = True

    tp, sl, sl_limit = num(spec.get("take_profit")), num(spec.get("stop_loss")), num(spec.get("stop_loss_limit"))
    if oclass != "simple":
        if qty is None or qty != int(qty):
            raise ValueError("Bracket, OCO and OTO orders need a whole number of shares.")
        body["order_class"] = oclass
        if oclass == "oco":
            if side != "sell" or otype != "limit":
                raise ValueError("An OCO order protects shares you own: choose Sell and Limit.")
            if not tp or not sl:
                raise ValueError("An OCO order needs both a take-profit and a stop-loss price.")
        elif otype not in ("market", "limit"):
            raise ValueError("Bracket and OTO entries must be market or limit orders.")
        if oclass in ("bracket", "oco") and not (tp and sl):
            raise ValueError("Enter both a take-profit and a stop-loss price.")
        if oclass == "oto" and not (tp or sl):
            raise ValueError("An OTO order needs a take-profit or a stop-loss price.")
        ref = limit_p if otype == "limit" and oclass != "oco" else price
        long = side == "buy" or oclass == "oco"
        if ref:
            if tp and (tp <= ref if long else tp >= ref):
                raise ValueError("The take-profit must be " + ("above" if long else "below") + " the current price.")
            if sl and (sl >= ref if long else sl <= ref):
                raise ValueError("The stop-loss must be " + ("below" if long else "above") + " the current price.")
        if tp:
            body["take_profit"] = {"limit_price": fmt_price(tp)}
        if sl:
            body["stop_loss"] = {"stop_price": fmt_price(sl)}
            if sl_limit:
                body["stop_loss"]["limit_price"] = fmt_price(sl_limit)

    # Safety: no short selling, no borrowing, and a size limit on buys.
    if side == "sell":
        if position_qty <= 0:
            raise ValueError(f"You don't own any {sym}. Selling what you don't own (short selling) is turned off.")
        if qty is not None and qty > position_qty + 1e-9:
            raise ValueError(f"You own {position_qty:g}; you can't sell more than that.")
        est = (qty or 0) * (price or 0) if qty is not None else notional
    else:
        ref_price = limit_p if otype in ("limit", "stop_limit") else (stop_p if otype == "stop" else price)
        if notional is not None:
            est = notional
        elif ref_price:
            est = qty * ref_price * (1.0 if otype in ("limit", "stop_limit") else 1.01)
        else:
            raise ValueError("No current price for this symbol, so the cost can't be checked. Try a limit order.")
        if est > m["max_manual_order_usd"]:
            raise ValueError(f"This order is about {T.money(est)}, over your {T.money(m['max_manual_order_usd'])} "
                             "limit for orders you place yourself (change it in Settings).")
        if est > cash:
            raise ValueError(f"This order is about {T.money(est)} but only {T.money(cash)} cash is available.")

    words = {"market": "market", "limit": "limit", "stop": "stop (stop-loss)", "stop_limit": "stop-limit",
             "trailing_stop": "trailing stop"}
    amount = f"{qty:g}" if qty is not None else T.money(notional) + " of"
    summary = f"{side.upper()} {amount} {sym}, {words[otype]} order"
    if oclass == "bracket":
        summary += f", with take-profit {T.money(tp)} and stop-loss {T.money(sl)}"
    elif oclass == "oco":
        summary = f"SELL {qty:g} {sym}: take profit at {T.money(tp)} OR stop out at {T.money(sl)}, whichever comes first"
    elif oclass == "oto":
        summary += ", then " + (f"take-profit {T.money(tp)}" if tp else f"stop-loss {T.money(sl)}")
    return body, {"summary": summary, "estimate": est}
