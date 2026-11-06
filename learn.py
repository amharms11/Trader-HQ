"""
What the bots know and learn.

- Playbooks: short research-based rules for each bot (knowledge/playbook-*.md), added to every Claude check.
- Lessons: after trading, Claude reviews a bot's own closed trades (numbers only: prices, times, profit or loss)
  and keeps a short list of lessons. They're added to that bot's next checks. You can remove any lesson, and
  removed ones aren't brought back.
- Library: the full research reports (knowledge/report-*.md), readable on the Learn page.

Neither playbooks nor lessons can change the hard limits: every trade is still checked by the app.
"""
import datetime as dt
import json
import re
import threading
import time

import features as F
import trader as T

BOTS = {"long_term": ("Long-term bot", "Long-term bot"), "day": ("Day trading", "Day trading"),
        "options": ("Calls & puts", "Options"), "crypto": ("Crypto bot", "Crypto bot"),
        "crypto_day": ("Crypto day trading", "Crypto day trading")}  # kind: (name shown, strategy tag on orders)
MIN_NEW_TRADES = 5      # closed trades needed before a review (fewer is mostly noise)
MAX_LESSONS = 8
REVIEW_AFTER = (16, 10)  # New York time: reviews run once a day after the stock market closes
REPORT_RE = re.compile(r"report-[a-z0-9-]{1,80}")

REVIEW_SYSTEM = """You review one trading bot's own closed trades in a personal brokerage account and keep a short
list of lessons it should remember for future trades. You get the bot's strategy, statistics, its recent closed
trades (numbers only), its current lessons and lessons the owner removed.

Rules:
- Return the full updated list (at most 8). Keep current lessons that still hold, merge duplicates, drop ones the
  newer trades contradict. Never bring back a lesson the owner removed, or one with the same meaning.
- Each lesson is one concrete, checkable habit for future trades (e.g. "Skip entries in the first 15 minutes after
  the open: 5 of 6 such trades hit their stop"), not general advice and not a prediction.
- Be honest about sample size. With fewer than about 30 trades, patterns are weak: say so in evidence and use
  confidence "low". Prefer lessons about risk and timing over lessons about particular symbols.
- Lessons can never loosen the owner's limits (size, stop-loss, number of trades, goals). They may make the bot
  more selective or more careful.
- evidence: the numbers from these trades that support the lesson.
- summary: 2-3 plain sentences for a beginner on how the bot has done and what changed in the list."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "lessons": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "evidence": {"type": "string"},
                           "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["text", "evidence", "confidence"], "additionalProperties": False}},
    },
    "required": ["summary", "lessons"], "additionalProperties": False,
}


class Lessons:
    def __init__(self, store):
        self.store = store
        with store.db.lock:
            store.db.conn.executescript("""
                CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY, kind TEXT, mode TEXT, text TEXT, evidence TEXT,
                    confidence TEXT, created REAL, status TEXT);
                CREATE TABLE IF NOT EXISTS lesson_reviews (id INTEGER PRIMARY KEY, kind TEXT, mode TEXT, time REAL,
                    trades INTEGER, last_closed TEXT, summary TEXT);
            """)
            store.db.conn.commit()

    def _rows(self, kind, mode, status):
        rows = self.store.q("SELECT id, text, evidence, confidence, created FROM lessons WHERE kind=? AND mode=? AND status=? "
                            "ORDER BY id", (kind, mode, status))
        return [dict(zip(("id", "text", "evidence", "confidence", "created"), r)) for r in rows]

    def active(self, kind, mode):
        return self._rows(kind, mode, "active")

    def removed(self, kind, mode):
        return self._rows(kind, mode, "removed")[-20:]

    def remove(self, lid):
        self.store.x("UPDATE lessons SET status='removed' WHERE id=? AND status='active'", (int(lid),))

    def clear(self, kind, mode):
        self.store.x("UPDATE lessons SET status='removed' WHERE kind=? AND mode=? AND status='active'", (kind, mode))

    def replace(self, kind, mode, lessons):
        self.store.x("UPDATE lessons SET status='replaced' WHERE kind=? AND mode=? AND status='active'", (kind, mode))
        for x in lessons:
            self.store.x("INSERT INTO lessons (kind, mode, text, evidence, confidence, created, status) VALUES (?,?,?,?,?,?,?)",
                         (kind, mode, x["text"], x["evidence"], x["confidence"], time.time(), "active"))

    def last_review(self, kind, mode):
        r = self.store.q("SELECT time, trades, last_closed, summary FROM lesson_reviews WHERE kind=? AND mode=? "
                         "ORDER BY id DESC LIMIT 1", (kind, mode))
        return dict(zip(("time", "trades", "last_closed", "summary"), r[0])) if r else None

    def record(self, kind, mode, trades, last_closed, summary):
        self.store.x("INSERT INTO lesson_reviews (kind, mode, time, trades, last_closed, summary) VALUES (?,?,?,?,?,?)",
                     (kind, mode, time.time(), trades, last_closed, summary))


def bot_trades(fills, kind):
    closed, _ = F.match_trades(fills)
    return [t for t in closed if t["strategy"] == BOTS[kind][1]]


def _et(iso):
    return T.parse_time(iso).astimezone(T.ET)


def trade_rows(trades):
    rows = []
    for t in trades:
        opened, closed = _et(t["opened"]), _et(t["closed"])
        rows.append({"symbol": t["symbol"], "opened_et": opened.strftime("%a %Y-%m-%d %H:%M"), "held_minutes": round((closed - opened).total_seconds() / 60),
                     "buy": round(t["buy_price"], 4), "sell": round(t["sell_price"], 4), "pnl_usd": round(t["pnl"], 2),
                     "pnl_pct": round((t["sell_price"] / t["buy_price"] - 1) * 100, 2) if t["buy_price"] else None})
    return rows


def new_since(trades, last):
    if not last or not last.get("last_closed"):
        return trades
    return [t for t in trades if t["closed"] > last["last_closed"]]


def review(lessons, broker, kind, settings, fills=None, force=False):
    """Have Claude update this bot's lessons from its closed trades. Returns a result dict (skipped=True if there
    weren't enough new trades and force is off)."""
    if kind not in BOTS:
        raise ValueError("Unknown bot.")
    mode = broker.mode
    trades = bot_trades(fills if fills is not None else F.fetch_fills(broker, max_pages=4), kind)
    last = lessons.last_review(kind, mode)
    fresh = new_since(trades, last)
    if not trades:
        return {"skipped": True, "reason": f"{BOTS[kind][0]} has no closed trades yet."}
    if not force and len(fresh) < MIN_NEW_TRADES:
        return {"skipped": True, "reason": f"Waiting for {MIN_NEW_TRADES - len(fresh)} more closed trade(s) before the next review."}
    recent = trades[-60:]
    section = settings if kind == "long_term" else settings.get(kind, {})
    prompt = (f"<bot>{BOTS[kind][0]}</bot>\n\n<strategy>\n{section.get('strategy', '')}\n</strategy>\n\n"
              f"<stats>\n{json.dumps(F.trade_stats(trades), default=str)}\n</stats>\n\n"
              f"<closed_trades count=\"{len(recent)}\" new_since_last_review=\"{len(fresh)}\">\n"
              f"{json.dumps(trade_rows(recent), indent=0)}\n</closed_trades>\n\n"
              f"<current_lessons>\n{json.dumps([{k: x[k] for k in ('text', 'evidence', 'confidence')} for x in lessons.active(kind, mode)])}\n</current_lessons>\n\n"
              f"<removed_by_owner>\n{json.dumps([x['text'] for x in lessons.removed(kind, mode)])}\n</removed_by_owner>\n\n"
              "Update the lessons list.")
    system = REVIEW_SYSTEM + (T.playbook(kind) if settings.get("use_playbook", True) else "")
    out = T.ask_claude(prompt, system, REVIEW_SCHEMA, effort="medium", raw=True)
    kept = []
    for x in (out.get("lessons") or [])[:MAX_LESSONS]:
        if not isinstance(x, dict):
            continue
        text = F.clean_text(x.get("text"), 240)
        if text:
            conf = x.get("confidence") if x.get("confidence") in ("low", "medium", "high") else "low"
            kept.append({"text": text, "evidence": F.clean_text(x.get("evidence"), 240), "confidence": conf})
    summary = F.clean_text(out.get("summary"), 600)
    lessons.replace(kind, mode, kept)
    lessons.record(kind, mode, len(trades), trades[-1]["closed"], summary)
    T.journal("lessons", mode=mode, bot=kind, summary=summary, lessons=kept)
    return {"skipped": False, "summary": summary, "lessons": kept, "trades": len(trades)}


def prompt_lessons(lessons, kind, mode):
    rows = lessons.active(kind, mode)
    if not rows:
        return ""
    lines = "\n".join(f"- {x['text']} (confidence: {x['confidence']})" for x in rows)
    return ("<lessons>\nNotes you wrote after reviewing this bot's own past trades in this account. Weigh them, "
            "remembering they come from small samples. They never override the limits.\n" + lines + "\n</lessons>\n\n")


def make_hook(get_settings, get_lessons, get_mode):
    """What ask_claude adds for each bot: the playbook (system prompt) and the bot's lessons (prompt)."""
    def hook(kind):
        s = get_settings()
        extra_system = T.playbook(kind) if s.get("use_playbook", True) else ""
        extra_prompt = prompt_lessons(get_lessons(), kind, get_mode()) if s.get("learn_from_trades", True) else ""
        return extra_system, extra_prompt
    return hook


def due_for_daily_review(now=None):
    now = now or dt.datetime.now(T.ET)
    return (now.hour, now.minute) >= REVIEW_AFTER


# ---------- library and playbooks (Learn page) ----------

def _title(text, fallback):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def library():
    out = []
    for p in sorted(T.KNOWLEDGE.glob("report-*.md")):
        try:
            text = p.read_text()
        except OSError:
            continue
        out.append({"id": p.stem, "title": _title(text, p.stem), "words": len(text.split())})
    return out


def report(rid):
    if not REPORT_RE.fullmatch(str(rid or "")):
        raise ValueError("Unknown report.")
    p = T.KNOWLEDGE / f"{rid}.md"
    if not p.is_file():
        raise ValueError("Unknown report.")
    text = p.read_text()
    return {"id": rid, "title": _title(text, rid), "text": text}


def playbook_parts(kind):
    out = []
    for name in T.PLAYBOOKS.get(kind, []):
        try:
            text = (T.KNOWLEDGE / f"playbook-{name}.md").read_text().strip()
        except OSError:
            continue
        out.append({"name": name, "title": _title(text, name), "text": text})
    return out


class AutoReviewer:
    """Runs each bot's daily review in the background (one at a time), so it never holds up trading."""
    def __init__(self):
        self.lock = threading.Lock()
        self.done = set()

    def maybe_run(self, lessons, broker, settings, log, today=None):
        today = (today or F.today_et()).isoformat()
        if not settings.get("learn_from_trades", True) or not due_for_daily_review() or not self.lock.acquire(blocking=False):
            return False

        def run():
            try:
                fills = F.fetch_fills(broker, max_pages=4)
                for kind in BOTS:
                    if (kind, broker.mode, today) in self.done:
                        continue
                    self.done.add((kind, broker.mode, today))
                    try:
                        r = review(lessons, broker, kind, settings, fills=fills)
                        if not r["skipped"]:
                            log(f"{BOTS[kind][0]} reviewed its trades and updated its lessons ({len(r['lessons'])}).")
                    except Exception as e:  # noqa: BLE001 - tried again tomorrow
                        log(f"{BOTS[kind][0]}: trade review skipped: {e}")
            finally:
                self.lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True
