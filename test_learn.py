"""Tests for the bots' playbooks, their lessons from past trades, and the Learn page. No network or keys."""
import unittest
from unittest import mock

import learn as L
import trader as T
import updater as U
import test_features as TF

order, Base = TF.order, TF.Base


def closed_trades(n, cid="amhd-", start_day=2):
    out = []
    for i in range(n):
        day = f"2026-10-{start_day + i // 3:02d}"
        out.append(order(f"b{i}", "AAPL", "buy", 10, 200, f"{day}T14:{10 + i % 3 * 10}:00Z", f"{cid}{i}"))
        out.append(order(f"s{i}", "AAPL", "sell", 10, 199 if i % 2 else 202, f"{day}T15:{10 + i % 3 * 10}:00Z", f"amhx-{i}"))
    return out


REVIEW = {"summary": "Mixed results; early entries lost.", "lessons": [
    {"text": "Skip entries before 10:00 ET", "evidence": "3 of 4 early trades lost", "confidence": "low"},
    {"text": "  ", "evidence": "", "confidence": "high"},                       # empty: dropped
    {"text": "Prefer pullbacks to VWAP", "evidence": "2 wins", "confidence": "bogus"}]}


class PlaybookTests(unittest.TestCase):
    def test_every_bot_has_a_playbook(self):
        for kind in T.PLAYBOOKS:
            text = T.playbook(kind)
            self.assertIn("<playbook>", text, kind)
            self.assertIn("never overrides", text)
            self.assertLessEqual(len(text), T.PLAYBOOK_MAX_CHARS + 400)
        self.assertIn("Crypto day trading", T.playbook("crypto_day"))
        self.assertEqual(T.playbook("nope"), "")

    def test_with_knowledge_uses_hook(self):
        old = T.knowledge_hook
        try:
            T.knowledge_hook = lambda kind: ("\nSYS-" + kind, "LESSONS\n")
            self.assertEqual(T.with_knowledge("day", "P", "S"), ("LESSONS\nP", "S\nSYS-day"))
            self.assertEqual(T.with_knowledge(None, "P", "S"), ("P", "S"))
        finally:
            T.knowledge_hook = old

    def test_raw_answers_and_kind_passed(self):
        with mock.patch.object(T, "claude_backend", return_value="api"), \
                mock.patch.object(T, "_ask_api", return_value={"x": 1}) as api, \
                mock.patch.object(T, "knowledge_hook", lambda kind: ("+pb", "")):
            self.assertEqual(T.ask_claude("p", "s", {}, kind="options", raw=True), {"x": 1})
            self.assertEqual(api.call_args[0][1], "s+pb")
            self.assertTrue(api.call_args[0][4])
        self.assertEqual(T._raw_json('```json\n{"a": 2}\n```'), {"a": 2})
        with self.assertRaises(RuntimeError):
            T._raw_json("[1]")

    def test_library_and_safe_report_ids(self):
        ids = [r["id"] for r in L.library()]
        self.assertIn("report-stock-investing-guide", ids)
        self.assertTrue(L.report("report-crypto-day-trading")["text"].startswith("# "))
        for bad in ("../trader", "report-../../x", "playbook-general", "report-nope", ""):
            with self.assertRaises(ValueError):
                L.report(bad)

    def test_updater_ships_knowledge(self):
        self.assertTrue(U.wanted("knowledge/playbook-general.md"))
        self.assertTrue(U.wanted("knowledge/report-stock-investing-guide.md"))


class LessonTests(Base):
    def setUp(self):
        super().setUp()
        self.lessons = L.Lessons(self.store)
        self.s = T.load_settings()

    def test_needs_enough_trades_then_reviews(self):
        self.b.closed = closed_trades(3)
        r = L.review(self.lessons, self.b, "day", self.s)
        self.assertTrue(r["skipped"])
        self.assertIn("2 more", r["reason"])
        self.b.closed = closed_trades(6)
        with mock.patch.object(T, "ask_claude", return_value=REVIEW) as ask:
            r = L.review(self.lessons, self.b, "day", self.s)
        self.assertFalse(r["skipped"])
        prompt, system = ask.call_args[0][0], ask.call_args[0][1]
        self.assertIn('count="6"', prompt)
        self.assertIn("<playbook>", system)
        self.assertTrue(ask.call_args[1]["raw"])
        self.assertNotIn("kind", ask.call_args[1])  # the review itself doesn't get the old lessons twice
        active = self.lessons.active("day", "paper")
        self.assertEqual([x["text"] for x in active], ["Skip entries before 10:00 ET", "Prefer pullbacks to VWAP"])
        self.assertEqual(active[1]["confidence"], "low")
        # no new trades since: skipped unless forced
        self.assertTrue(L.review(self.lessons, self.b, "day", self.s)["skipped"])
        self.assertEqual(L.review(self.lessons, self.b, "crypto", self.s)["reason"], "Crypto bot has no closed trades yet.")

    def test_removed_lessons_are_remembered_and_prompt_text(self):
        self.lessons.replace("day", "paper", [{"text": "A", "evidence": "e", "confidence": "low"},
                                              {"text": "B", "evidence": "e", "confidence": "medium"}])
        a = self.lessons.active("day", "paper")[0]
        self.lessons.remove(a["id"])
        self.assertEqual([x["text"] for x in self.lessons.active("day", "paper")], ["B"])
        self.assertEqual([x["text"] for x in self.lessons.removed("day", "paper")], ["A"])
        self.assertEqual(self.lessons.active("day", "live"), [])
        text = L.prompt_lessons(self.lessons, "day", "paper")
        self.assertIn("- B (confidence: medium)", text)
        self.assertIn("never override the limits", text)
        self.assertEqual(L.prompt_lessons(self.lessons, "options", "paper"), "")
        self.b.closed = closed_trades(6)
        with mock.patch.object(T, "ask_claude", return_value=REVIEW) as ask:
            L.review(self.lessons, self.b, "day", self.s, force=True)
        self.assertIn('"A"', ask.call_args[0][0].split("<removed_by_owner>")[1])

    def test_hook_respects_switches(self):
        self.lessons.replace("day", "paper", [{"text": "Be careful", "evidence": "", "confidence": "low"}])
        s = dict(self.s)
        hook = L.make_hook(lambda: s, lambda: self.lessons, lambda: "paper")
        sys_text, prompt = hook("day")
        self.assertIn("<playbook>", sys_text)
        self.assertIn("Be careful", prompt)
        s.update(use_playbook=False, learn_from_trades=False)
        self.assertEqual(hook("day"), ("", ""))


class LearnApiTests(Base):
    req = TF.AppTests.req

    def setUp(self):
        super().setUp()
        self.app = TF.D.App(broker_factory=lambda mode: self.b)
        self.app.market, self.app._store = self.mk, self.store
        self.server = TF.D.make_server(self.app, port=0)
        self.port = self.server.server_address[1]
        TF.threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_learn_page_api(self):
        st, d, _ = self.req("/api/learn")
        self.assertEqual(st, 200)
        self.assertEqual([b["kind"] for b in d["bots"]], list(L.BOTS))
        self.assertTrue(d["bots"][0]["playbook"])
        self.assertTrue(d["library"])
        st, r, _ = self.req("/api/learn/report?id=report-chart-reading-and-timing")
        self.assertEqual(st, 200)
        self.assertEqual(self.req("/api/learn/report?id=../desktop")[0], 400)
        self.b.closed = closed_trades(6)
        with mock.patch.object(T, "ask_claude", return_value=REVIEW):
            st, r, _ = self.req("/api/learn/review", {"kind": "day"})
        self.assertEqual((st, len(r["lessons"])), (200, 2))
        self.assertEqual(self.req("/api/learn/review", {"kind": "nope"})[0], 400)
        lid = self.req("/api/learn")[1]["bots"][1]["lessons"][0]["id"]
        self.assertEqual(self.req("/api/learn/remove", {"id": lid})[0], 200)
        self.assertEqual(len(self.req("/api/learn")[1]["bots"][1]["lessons"]), 1)

    def test_the_app_feeds_lessons_to_claude(self):
        self.app.lessons.replace("crypto_day", "paper", [{"text": "No meme coins at night", "evidence": "", "confidence": "low"}])
        sys_extra, prompt_extra = T.knowledge_hook("crypto_day")
        self.assertIn("No meme coins at night", prompt_extra)
        self.assertIn("Crypto day trading", sys_extra)


if __name__ == "__main__":
    unittest.main()
