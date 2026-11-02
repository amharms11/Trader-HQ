"""Run with: python -m unittest test_phone.py  (no network needed)"""
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
import phone as P
import test_features as TF
import trader as T

PHONE_IP = "127.0.0.2"  # stands in for the Mac's Tailscale address


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(T, "DATA", self.tmp)
        self.p.start()
        self.auth = P.Auth()

    def tearDown(self):
        self.p.stop()

    def test_passcode_rules_and_storage(self):
        for bad in ("12345", "", "111111"):
            with self.assertRaises(ValueError):
                self.auth.set_passcode(bad)
        self.auth.set_passcode("tulip-2026")
        stored = json.loads((self.tmp / "phone.json").read_text())
        self.assertNotIn("tulip-2026", json.dumps(stored))  # only a salted hash is kept
        self.assertEqual(oct((self.tmp / "phone.json").stat().st_mode)[-3:], "600")

    def test_login_sessions_and_revoke(self):
        self.auth.set_passcode("tulip-2026")
        with self.assertRaises(PermissionError):
            self.auth.login("wrong", "iPhone", "1.2.3.4")
        t1 = self.auth.login("tulip-2026", "iPhone", "1.2.3.4")
        t2 = self.auth.login("tulip-2026", "iPad", "1.2.3.5")
        self.assertTrue(self.auth.valid(t1) and self.auth.valid(t2))
        self.assertFalse(self.auth.valid("made-up") or self.auth.valid(""))
        self.assertEqual([s["name"] for s in self.auth.sessions()], ["iPhone", "iPad"])
        self.assertNotIn("hash", self.auth.sessions()[0])
        self.auth.revoke(sid=self.auth.sessions()[0]["id"])
        self.assertFalse(self.auth.valid(t1))
        self.assertTrue(self.auth.valid(t2))
        self.auth.set_passcode("new-pass-99")  # changing the passcode signs everyone out
        self.assertFalse(self.auth.valid(t2))

    def test_lockout_after_five_wrong_tries(self):
        self.auth.set_passcode("tulip-2026")
        for _ in range(5):
            with self.assertRaises(PermissionError):
                self.auth.login("nope", "iPhone", "9.9.9.9")
        with self.assertRaises(PermissionError) as e:
            self.auth.login("tulip-2026", "iPhone", "9.9.9.9")  # even the right one waits
        self.assertIn("15 minutes", str(e.exception))
        self.assertTrue(self.auth.login("tulip-2026", "iPhone", "8.8.8.8"))  # a different device isn't blocked


class PushTests(unittest.TestCase):
    def settings(self, **phone):
        s = T.load_settings()
        s["phone"].update({"push_topic": "traderhq-abcdefghijkl", **phone})
        return s

    def test_push_format_and_filters(self):
        sent = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        opener = lambda req, timeout: sent.append(req) or Resp()
        self.assertTrue(P.push(self.settings(), "Approval needed", "BUY 5 QQQ — stop −1%", "approval", click="http://x/#live", opener=opener))
        body = json.loads(sent[0].data)
        self.assertEqual(sent[0].full_url, "https://ntfy.sh")
        self.assertEqual((body["topic"], body["title"], body["priority"], body["click"]), ("traderhq-abcdefghijkl", "Approval needed", 4, "http://x/#live"))
        self.assertIn("—", body["message"])  # sent as JSON, so any characters work
        self.assertFalse(P.push(self.settings(push_approvals=False), "t", "b", "approval", opener=opener))
        self.assertTrue(P.push(self.settings(push_approvals=False), "t", "b", "approval", opener=opener, force=True))
        self.assertFalse(P.push(self.settings(push_topic=""), "t", "b", "trade", opener=opener))
        self.assertFalse(P.push(self.settings(push_topic="bad topic!"), "t", "b", "trade", opener=opener))
        self.assertTrue(P.TOPIC_RE.fullmatch(P.new_topic()))

    def test_tailscale_detection(self):
        status = {"Self": {"TailscaleIPs": ["100.101.102.103", "fd7a::1"], "DNSName": "alex-mac.tail1234.ts.net."}}
        run = mock.Mock(return_value=mock.Mock(stdout=json.dumps(status)))
        with mock.patch.object(P, "tailscale_exe", return_value="/x/tailscale"), mock.patch.object(P.subprocess, "run", run):
            self.assertEqual(P.tailscale_info(), {"installed": True, "ip": "100.101.102.103", "dns": "alex-mac.tail1234.ts.net"})
        ifconfig = mock.Mock(return_value=mock.Mock(stdout="utun4: flags\n\tinet 100.88.1.2 --> 100.88.1.2 netmask 0xffffffff\n"))
        with mock.patch.object(P, "tailscale_exe", return_value=None), mock.patch.object(P.subprocess, "run", ifconfig):
            self.assertEqual(P.tailscale_info()["ip"], "100.88.1.2")
        with mock.patch.object(P, "tailscale_exe", return_value=None), \
             mock.patch.object(P.subprocess, "run", mock.Mock(return_value=mock.Mock(stdout="inet 192.168.1.5"))):
            self.assertEqual(P.tailscale_info(), {"installed": False, "ip": None, "dns": None})


class PhoneServerTests(TF.Base):
    def setUp(self):
        super().setUp()
        self.app = D.App(broker_factory=lambda mode: self.b)
        self.app.market, self.app._store = self.mk, self.store
        self.local = D.make_server(self.app, port=0)
        self.port = self.local.server_address[1]
        threading.Thread(target=self.local.serve_forever, daemon=True).start()
        self.app.auth.set_passcode("tulip-2026")
        D.save_settings({"phone": {"enabled": True}})
        self.push = mock.patch.object(P, "push_async").start()
        self.app.phone_sync(info={"installed": True, "ip": PHONE_IP, "dns": None})
        self.rport = self.app.phone_server.server_address[1]

    def tearDown(self):
        mock.patch.stopall()
        if self.app.phone_server:
            self.app.phone_server.shutdown()
            self.app.phone_server.server_close()
        self.local.shutdown()
        self.local.server_close()
        super().tearDown()

    def req(self, path, body=None, remote=True, cookie=None, host=None):
        ip, port = (PHONE_IP, self.rport) if remote else ("127.0.0.1", self.port)
        h = {"Host": host or f"{ip}:{port}"}
        if body is not None:
            h.update({"Content-Type": "application/json", "X-Trader-HQ": "1"})
        if cookie:
            h["Cookie"] = f"{P.COOKIE}={cookie}"
        r = urllib.request.Request(f"http://{ip}:{port}{path}", data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                data = resp.read()
                return resp.status, (json.loads(data) if "json" in resp.headers["Content-Type"] else data), resp.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (json.loads(data) if data[:1] == b"{" else data), e.headers

    def login(self):
        code, _, headers = self.req("/api/phone/login", {"passcode": "tulip-2026", "device": "iPhone"})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";")[0].split("=", 1)[1]

    def test_phone_address_needs_sign_in(self):
        self.assertEqual(self.app.phone_server.server_address[0], PHONE_IP)
        code, page, _ = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Enter the phone passcode", page)  # the login page, not the dashboard
        self.assertEqual(self.req("/api/state")[0], 401)
        self.assertEqual(self.req("/api/day/start", {})[0], 401)
        self.assertEqual(self.req("/assets/icon-180.png")[0], 200)
        self.assertEqual(self.req("/manifest.webmanifest")[1]["start_url"], "/#live")
        self.assertEqual(self.req("/api/phone/login", {"passcode": "wrong"})[0], 401)

    def test_signed_in_phone_can_use_dashboard(self):
        token = self.login()
        code, page, _ = self.req("/", cookie=token)
        self.assertIn(b"Trader HQ", page)
        self.assertNotIn(b"Enter the phone passcode", page)
        st = self.req("/api/state", cookie=token)[1]
        self.assertTrue(st["remote"])
        self.assertEqual(self.req("/api/settings", {"settings": {"max_order_usd": 900}}, cookie=token)[0], 200)
        self.assertEqual(self.req("/api/settings", {"settings": {"mode": "paper"}}, cookie=token)[0], 200)  # unchanged is fine
        self.assertFalse(self.req("/api/state", remote=False)[1]["remote"])

    def test_phone_cant_change_sensitive_things(self):
        token = self.login()
        for path, body in (("/api/keys", {"mode": "live", "key": "AK1", "secret": "x"}), ("/api/quit", {}),
                           ("/api/anthropic-key", {"key": "sk-ant-x"}), ("/api/phone/passcode", {"passcode": "hijacked-1"}),
                           ("/api/phone/revoke", {}), ("/api/settings", {"settings": {"mode": "live"}}),
                           ("/api/settings", {"settings": {"phone": {"enabled": False}}})):
            code, err, _ = self.req(path, body, cookie=token)
            self.assertEqual(code, 403, path)
            self.assertIn("only be changed on the Mac", err["error"])
        self.assertEqual(self.req("/api/phone", cookie=token)[0], 403)
        self.assertFalse(self.app.stopping.is_set())

    def test_wrong_host_and_missing_header_refused(self):
        token = self.login()
        self.assertEqual(self.req("/api/state", cookie=token, host="evil.example:8780")[0], 403)
        h = {"Host": f"{PHONE_IP}:{self.rport}", "Cookie": f"{P.COOKIE}={token}", "Content-Type": "application/json"}
        r = urllib.request.Request(f"http://{PHONE_IP}:{self.rport}/api/day/stop", data=b"{}", headers=h)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r, timeout=5)
        self.assertEqual(e.exception.code, 403)

    def test_logout_and_revoke_from_mac(self):
        token = self.login()
        code, _, headers = self.req("/api/phone/logout", {}, cookie=token)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.req("/api/state", cookie=token)[0], 401)
        token = self.login()
        st = self.req("/api/phone", remote=False)[1]
        self.assertEqual((st["running"], st["has_passcode"], len(st["sessions"])), (True, True, 1))
        self.assertTrue(st["url"].startswith(f"http://{PHONE_IP}:"))
        self.req("/api/phone/revoke", {}, remote=False)
        self.assertEqual(self.req("/api/state", cookie=token)[0], 401)

    def test_turning_off_stops_the_phone_address(self):
        D.save_settings({"phone": {"enabled": False}})
        self.app.phone_sync(info={"installed": True, "ip": PHONE_IP, "dns": None})
        self.assertIsNone(self.app.phone_server)
        with self.assertRaises(OSError):
            urllib.request.urlopen(f"http://{PHONE_IP}:{self.rport}/", timeout=2)

    def test_mac_side_phone_settings(self):
        self.assertEqual(self.req("/api/phone/passcode", {"passcode": "123"}, remote=False)[0], 400)
        topic = self.req("/api/phone/new-topic", {}, remote=False)[1]["settings"]["push_topic"]
        self.assertTrue(P.TOPIC_RE.fullmatch(topic))
        self.assertEqual(self.req("/api/settings", {"settings": {"phone": {"push_topic": "has spaces!"}}}, remote=False)[0], 400)

    def test_notifications_reach_the_phone(self):
        self.app.notify("AAPL alert", "AAPL rose to $210.", "alert")
        title, body, kind = self.push.call_args[0][1:4]
        self.assertEqual((title, kind), ("AAPL alert", "alert"))
        self.assertTrue(self.push.call_args[1]["click"].endswith("/#live"))

        class Plan(T.Plan):
            kind, label = "day", "Day trade"

            def check(self, t):
                return True, "", {"qty": 1, "limit_price": 10, "cost": 10}

            def question(self, t, o):
                return "?"
        plan = Plan(self.b, T.load_settings())
        plan.trades = [{"symbol": "AAPL", "side": "buy", "qty": 1, "reason": "", "risk": ""}]
        self.app.add_plan(plan, 180)
        title, body, kind = self.push.call_args[0][1:4]
        self.assertEqual((title, kind), ("Approval needed", "approval"))
        self.assertIn("BUY 1 AAPL", body)
        self.assertIn("3 min", body)


if __name__ == "__main__":
    unittest.main()
