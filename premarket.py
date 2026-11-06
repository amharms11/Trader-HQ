"""
Premarket watch for stock day trading (4:00-9:30 AM ET).

While day trading is on before the open, the app:
- scans every stock Alpaca lists for premarket gaps (price now vs. yesterday's close), keeps the liquid ones and
  measures their premarket volume, high and low from 1-minute bars;
- pulls each gapper's latest headlines for YOU to read (Claude never reads news text: a headline could be written
  to steer it; it only learns how many headlines a stock has);
- around 9:15 asks Claude for a short game plan from the numbers;
- at the open, the gappers join the day's watchlist and Claude sees their premarket levels (gap, premarket high
  and low, yesterday's close), which opening range and gap-and-go setups key off.

Nothing is bought or sold before 9:30: premarket prices are thin and jumpy, and bracket orders don't work then.
"""
import datetime as dt
import json

import features as F
import market as M
import trader as T

PLAN_SYSTEM = """You write a short premarket game plan for a beginner day trader whose bot only BUYS (no shorting)
and only trades after the 9:30 AM ET open. You get numbers only: each premarket gapper's gap %, price, yesterday's
close, premarket high/low, premarket dollar volume and how many news headlines it has today (you can't see them).

Write 4-8 short lines in plain English:
- which 2-4 gap-ups look most worth watching at the open and the level that matters (usually a break above the
  premarket high after the first 5-15 minutes, or holding above VWAP), and which look too thin or extended;
- a reminder of the main risk today (e.g. big gaps often fade in the first 30 minutes);
- if nothing looks good, say so: no trade is a fine plan.
Never invent news, catalysts or facts that aren't in the numbers. Not financial advice."""


def session_start(now):
    return now.replace(hour=4, minute=0, second=0, microsecond=0)


def is_premarket(now=None):
    """4:00-9:30 AM ET on a weekday."""
    now = now or dt.datetime.now(T.ET)
    return now.weekday() < 5 and (4, 0) <= (now.hour, now.minute) < (9, 30)


def minutes_to_open(now=None):
    now = now or dt.datetime.now(T.ET)
    return max(0, (9 * 60 + 30) - (now.hour * 60 + now.minute))


def _prev_close(snap, today):
    """Yesterday's regular-session close. Before the open, Alpaca's dailyBar is still yesterday's bar."""
    d, p = snap.get("dailyBar") or {}, snap.get("prevDailyBar") or {}
    if d.get("t") and T.parse_time(d["t"]).astimezone(T.ET).date() < today:
        return M.num(d.get("c"))
    return M.num(p.get("c"))


def scan(market, d, now=None, news=True):
    """Premarket gappers: {"time", "scanned", "rows": gap-ups, "gap_downs": biggest gap-downs (for information)}."""
    now = now or dt.datetime.now(T.ET)
    today = now.date()
    market.ensure_assets()
    symbols = market.db.symbols("stock")
    snaps = market.snapshots_raw(symbols)
    cands = []
    for sym, s in snaps.items():
        trade = (s or {}).get("latestTrade") or {}
        price = M.num(trade.get("p"))
        if not price or not trade.get("t") or price < d["premarket_min_price"]:
            continue
        if T.parse_time(trade["t"]).astimezone(T.ET).date() != today:
            continue  # no trade yet today
        prev = _prev_close(s, today)
        if not prev:
            continue
        gap = (price / prev - 1) * 100
        if abs(gap) >= d["premarket_min_gap_pct"]:
            cands.append({"symbol": sym, "price": price, "prev_close": prev, "gap_pct": round(gap, 2)})
    cands.sort(key=lambda r: abs(r["gap_pct"]), reverse=True)
    cands = cands[:40]
    bars = _premarket_bars(market, [c["symbol"] for c in cands], now)
    scale = 1 / F.IEX_SHARE if market.feed() == "iex" else 1
    rows = []
    for c in cands:
        b = bars.get(c["symbol"]) or []
        vol = sum(x.get("v", 0) for x in b)
        dollars = sum(x.get("v", 0) * x["c"] for x in b) * scale
        if dollars < d["premarket_min_dollar_volume_m"] * 1e6:
            continue
        a = market.db.asset(c["symbol"])
        rows.append({**c, "name": a["name"] if a else "", "pm_volume": int(vol), "pm_dollar_volume_m": round(dollars / 1e6, 1),
                     "pm_high": max((x["h"] for x in b), default=c["price"]), "pm_low": min((x["l"] for x in b), default=c["price"])})
    ups = [r for r in rows if r["gap_pct"] > 0][:int(d["premarket_top"])]
    downs = [r for r in rows if r["gap_pct"] < 0][:3]
    if news:
        since = (now - dt.timedelta(hours=18)).astimezone(dt.timezone.utc)
        for r in ups + downs:
            try:
                items = [n for n in market.news(r["symbol"], limit=6)
                         if n.get("created_at") and T.parse_time(n["created_at"]) >= since]
            except Exception:  # noqa: BLE001 - news is a nice-to-have
                items = []
            r["news"] = [{k: n.get(k) for k in ("headline", "source", "url", "created_at")} for n in items[:3]]
            r["news_count"] = len(items)
    return {"time": now.timestamp(), "date": today.isoformat(), "scanned": len(snaps), "rows": ups, "gap_downs": downs}


def _premarket_bars(market, symbols, now):
    """1-minute bars from 4:00 AM ET today for each symbol."""
    if not symbols:
        return {}
    out, token = {}, None
    params = {"symbols": ",".join(symbols), "timeframe": "1Min", "feed": market.feed(), "limit": 10000,
              "start": session_start(now).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for _ in range(20):
        if token:
            params["page_token"] = token
        res = market.data("/v2/stocks/bars", params) or {}
        for sym, b in (res.get("bars") or {}).items():
            out.setdefault(sym, []).extend(b)
        token = res.get("next_page_token")
        if not token:
            break
    return out


def numbers_only(rows):
    """What Claude may see: numbers and a headline count, never the headlines."""
    keep = ("symbol", "gap_pct", "price", "prev_close", "pm_high", "pm_low", "pm_dollar_volume_m", "news_count")
    return [{k: r.get(k) for k in keep} for r in rows]


def game_plan(watch):
    """A short plan from Claude, written from the numbers only."""
    data = {"minutes_to_open": minutes_to_open(), "gap_ups": numbers_only(watch["rows"]),
            "gap_downs_for_context": numbers_only(watch["gap_downs"])}
    return T.ask_text(f"<premarket>\n{json.dumps(data, indent=1)}\n</premarket>\n\nWrite today's game plan.", PLAN_SYSTEM)


def levels_for_day(watch):
    """Premarket levels the day-trading checks get once the market opens."""
    return {r["symbol"]: {"gap_pct": r["gap_pct"], "prev_close": r["prev_close"], "premarket_high": r["pm_high"],
                          "premarket_low": r["pm_low"], "premarket_dollar_volume_m": r["pm_dollar_volume_m"],
                          "news_headlines_today": r.get("news_count", 0)} for r in watch["rows"]}
