"""
Phone access for Trader HQ.

- A second, passcode-protected address for the dashboard on your private Tailscale network, so your iPhone can
  reach it from anywhere. Nothing is opened to the public internet: only devices signed in to your own
  Tailscale account can even see it, and they still need the passcode.
- Push notifications to your phone through the free ntfy app (approvals waiting, trades, goals, alerts).

The Mac still does all the work; the phone is a remote control.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import trader as T

NTFY = "https://ntfy.sh"
COOKIE = "thq_session"
KIND_SETTING = {"approval": "push_approvals", "trade": "push_trades", "info": "push_trades",
                "goal": "push_goals_alerts", "alert": "push_goals_alerts", "summary": "push_summary", "error": "push_errors"}
KIND_TAGS = {"approval": "bell", "trade": "chart_with_upwards_trend", "info": "moneybag", "goal": "checkered_flag",
             "alert": "rotating_light", "summary": "memo", "error": "warning"}
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{12,64}")


def auth_file():
    return T.DATA / "phone.json"


def _hash_code(code, salt):
    return hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt), 200_000).hex()


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    """The phone passcode (stored only as a salted hash) and the phones that are signed in."""
    MAX_FAILS, WINDOW, MAX_SESSIONS = 5, 15 * 60, 10

    def __init__(self):
        self.lock = threading.Lock()
        self.fails = {}

    def _load(self):
        try:
            return json.loads(auth_file().read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        T.save_private(auth_file(), json.dumps(data))

    def has_passcode(self):
        return bool(self._load().get("hash"))

    def set_passcode(self, code):
        code = str(code or "")
        if not 6 <= len(code) <= 64:
            raise ValueError("Use a passcode of at least 6 characters.")
        if code.isdigit() and len(set(code)) == 1:
            raise ValueError("Pick a passcode that isn't the same digit repeated.")
        salt = secrets.token_hex(16)
        with self.lock:
            self._save({"salt": salt, "hash": _hash_code(code, salt), "sessions": []})  # signs every phone out

    def _locked_out(self, who):
        recent = [t for t in self.fails.get(who, []) if time.time() - t < self.WINDOW]
        self.fails[who] = recent
        return len(recent) >= self.MAX_FAILS

    def login(self, code, device, who):
        with self.lock:
            if self._locked_out(who):
                raise PermissionError("Too many wrong tries. Wait 15 minutes, then try again.")
            data = self._load()
            if not data.get("hash"):
                raise PermissionError("Set a passcode on the Mac first (Settings > Phone access).")
            if not hmac.compare_digest(_hash_code(str(code or ""), data["salt"]), data["hash"]):
                self.fails.setdefault(who, []).append(time.time())
                left = self.MAX_FAILS - len(self.fails[who])
                raise PermissionError("Wrong passcode." + (f" {left} tries left." if left <= 2 else ""))
            self.fails.pop(who, None)
            token = secrets.token_urlsafe(32)
            sessions = data.get("sessions", [])[-(self.MAX_SESSIONS - 1):]
            sessions.append({"id": secrets.token_hex(6), "hash": _hash_token(token), "name": str(device or "Phone")[:60],
                             "created": time.time(), "last": time.time()})
            data["sessions"] = sessions
            self._save(data)
            return token

    def valid(self, token):
        if not token:
            return False
        h = _hash_token(token)
        with self.lock:
            data = self._load()
            for s in data.get("sessions", []):
                if hmac.compare_digest(s["hash"], h):
                    if time.time() - s.get("last", 0) > 300:
                        s["last"] = time.time()
                        self._save(data)
                    return True
        return False

    def sessions(self):
        return [{k: v for k, v in s.items() if k != "hash"} for s in self._load().get("sessions", [])]

    def revoke(self, sid=None, token=None):
        """Sign out one phone (by id or by its own token), or every phone."""
        with self.lock:
            data = self._load()
            if sid is None and token is None:
                data["sessions"] = []
            else:
                th = _hash_token(token) if token else None
                data["sessions"] = [s for s in data.get("sessions", []) if s["id"] != sid and s["hash"] != th]
            self._save(data)


# ---------- finding this Mac on Tailscale ----------

def tailscale_exe():
    for c in (shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
              "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"):
        if c and os.path.isfile(c):
            return c
    return None


def tailscale_info():
    """This Mac's private Tailscale address (100.x.y.z) and name, if Tailscale is installed and connected."""
    exe, ip, dns = tailscale_exe(), None, None
    if exe:
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=8)
            me = (json.loads(out.stdout or "{}").get("Self") or {})
            ips = [x for x in me.get("TailscaleIPs") or [] if re.fullmatch(r"100\.\d+\.\d+\.\d+", x)]
            ip = ips[0] if ips else None
            dns = (me.get("DNSName") or "").rstrip(".") or None
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if not ip:  # the Tailscale interface shows up with an address in 100.64.0.0/10
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"inet (100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)", out)
            ip = m.group(1) if m else None
        except (OSError, subprocess.SubprocessError):
            pass
    return {"installed": bool(exe or ip), "ip": ip, "dns": dns}


# ---------- push notifications (ntfy) ----------

def new_topic():
    return "traderhq-" + re.sub(r"[^A-Za-z0-9]", "", secrets.token_urlsafe(24))[:20]


def push(settings, title, body, kind="info", click=None, opener=None, force=False):
    """Send one notification to the phone. Returns True if it was sent."""
    ph = settings.get("phone") or {}
    topic = ph.get("push_topic") or ""
    if not TOPIC_RE.fullmatch(topic):
        return False
    if not force and not ph.get(KIND_SETTING.get(kind, "push_trades"), True):
        return False
    msg = {"topic": topic, "title": str(title)[:120], "message": str(body)[:1000], "tags": [KIND_TAGS.get(kind, "bell")],
           "priority": 4 if kind in ("approval", "goal", "error") else 3}
    if click:
        msg["click"] = click
    req = urllib.request.Request(NTFY, data=json.dumps(msg).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "TraderHQ"})
    with (opener or urllib.request.urlopen)(req, timeout=10):
        pass
    return True


def push_async(settings, title, body, kind="info", click=None):
    def run():
        try:
            push(settings, title, body, kind, click)
        except (urllib.error.URLError, OSError, ValueError):
            pass  # a missed phone notification must never affect trading
    threading.Thread(target=run, daemon=True).start()


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Trader HQ">
<meta name="theme-color" content="#1d55b4"><link rel="apple-touch-icon" href="/assets/icon-180.png">
<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/assets/favicon.png">
<title>Trader HQ</title>
<style>
:root { color-scheme: light dark; --bg: #f9f9f7; --card: #fcfcfb; --ink: #0b0b0b; --muted: #6b6a66; --line: rgba(11,11,11,.12); --accent: #2a78d6; --bad: #d03b3b; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d0d0d; --card: #1a1a19; --ink: #f0efec; --muted: #a3a29b; --line: rgba(255,255,255,.12); --accent: #3987e5; --bad: #e5605f; } }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
  font: 16px/1.45 system-ui, -apple-system, sans-serif; padding: 24px 16px; }
main { width: 100%; max-width: 380px; background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 28px 22px; text-align: center; }
img { width: 72px; height: 72px; border-radius: 16px; }
h1 { font-size: 22px; margin: 12px 0 4px; } p { color: var(--muted); margin: 0 0 18px; font-size: 14px; }
input { width: 100%; font: inherit; font-size: 20px; letter-spacing: 2px; text-align: center; padding: 12px; border-radius: 12px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); }
button { width: 100%; margin-top: 12px; font: inherit; font-weight: 700; padding: 13px; border: 0; border-radius: 12px; background: var(--accent); color: #fff; }
.err { color: var(--bad); font-size: 14px; min-height: 20px; margin-top: 10px; }
</style></head><body><main>
<img src="/assets/icon-180.png" alt=""><h1>Trader HQ</h1>
<p>Enter the phone passcode you set on your Mac.</p>
<form id="f"><input id="code" type="password" inputmode="text" autocomplete="current-password" autofocus aria-label="Passcode">
<button type="submit">Sign in</button><div class="err" id="err" role="alert"></div></form>
</main><script>
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const err = document.getElementById("err"); err.textContent = "";
  const device = /iPhone/.test(navigator.userAgent) ? "iPhone" : /iPad/.test(navigator.userAgent) ? "iPad" : /Android/.test(navigator.userAgent) ? "Android phone" : "Browser";
  try {
    const r = await fetch("/api/phone/login", {method: "POST", headers: {"Content-Type": "application/json", "X-Trader-HQ": "1"},
      body: JSON.stringify({passcode: document.getElementById("code").value, device})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { err.textContent = d.error || "Couldn't sign in."; return; }
    location.replace("/?signed-in=1#live");  // a different address, so the browser really loads the dashboard
  } catch (_) { err.textContent = "Can't reach your Mac. Is Trader HQ open and is Tailscale on (on both)?"; }
};
</script></body></html>"""

MANIFEST = {
    "name": "Trader HQ", "short_name": "Trader HQ", "start_url": "/#live", "display": "standalone",
    "background_color": "#0d0d0d", "theme_color": "#1d55b4",
    "icons": [{"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}],
}
