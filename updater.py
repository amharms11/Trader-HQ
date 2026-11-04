"""
Keeps Trader HQ up to date: when the app starts it checks GitHub for a newer version of the `main`
branch and, if there is one, installs it before opening.

Only Trader HQ's own program files are replaced. Your keys, settings and history live in
~/.amh-trader/ and are never touched.

If the GitHub repository is private, add a read-only GitHub token in Settings > Updates
(saved as TRADERHQ_GITHUB_TOKEN in ~/.amh-trader/config.env).
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import trader as T

REPO = "amharms11/Trader-HQ"
BRANCH = "main"
HERE = Path(__file__).resolve().parent
TOP_LEVEL = re.compile(r"^[\w.-]+\.(py|html|js|txt|md|sh)$|^\.gitignore$")
FOLDERS = ("vendor", "ninjatrader", "assets", "knowledge")
MAX_ZIP = 30 * 1024 * 1024


def version_file():
    return T.DATA / "version.json"


def installed():
    try:
        return json.loads(version_file().read_text())
    except (OSError, ValueError):
        return {}


def token():
    T.load_config()
    return os.environ.get("TRADERHQ_GITHUB_TOKEN", "").strip()


def _get(url, accept="application/vnd.github+json", timeout=15):
    headers = {"Accept": accept, "User-Agent": "TraderHQ-updater", "X-GitHub-Api-Version": "2022-11-28"}
    if token():
        headers["Authorization"] = f"Bearer {token()}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(MAX_ZIP + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise RuntimeError("GitHub didn't allow the update check. If the Trader-HQ repository is private, add a "
                               "GitHub token in Settings > Updates.") from None
        raise RuntimeError(f"GitHub returned {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"couldn't reach GitHub ({getattr(e, 'reason', e)})") from None
    if len(data) > MAX_ZIP:
        raise RuntimeError("the update download was unexpectedly large")
    return data


def latest_sha():
    data = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    sha = data.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("GitHub sent an unexpected answer")
    return sha


def check():
    """Returns {'current', 'latest', 'available'} without changing anything."""
    latest = latest_sha()
    current = installed().get("sha")
    return {"current": current, "latest": latest, "available": current != latest}


def wanted(name):
    """Which paths from the download are Trader HQ program files (never anything with '..')."""
    parts = PurePosixPath(name).parts
    if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
        return False
    if len(parts) == 1:
        return bool(TOP_LEVEL.match(parts[0]))
    return parts[0] in FOLDERS and len(parts) == 2 and not parts[1].startswith(".")


def install(zip_bytes, sha, here=HERE):
    """Write the program files from a GitHub zip into `here`. Returns the list of changed files."""
    changed = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            raise RuntimeError("the update download was empty")
        root = names[0].split("/", 1)[0] + "/"  # GitHub wraps everything in one folder
        files = {n[len(root):]: n for n in names if n.startswith(root) and wanted(n[len(root):])}
        if "trader.py" not in files or "desktop.py" not in files:
            raise RuntimeError("the update download doesn't look like Trader HQ")
        for rel, member in sorted(files.items()):
            data = z.read(member)
            target = here / rel
            if target.exists() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".updating")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            changed.append(rel)
    T.DATA.mkdir(mode=0o700, exist_ok=True)
    version_file().write_text(json.dumps({"sha": sha, "installed_at": time.strftime("%Y-%m-%d %H:%M")}))
    return changed


def update(quiet=False, here=HERE):
    """Install the newest version if there is one. Returns the list of changed files ([] if none)."""
    if (here / ".git").exists():  # a developer checkout: use git instead
        if not quiet:
            print("This folder is a git checkout, so it isn't auto-updated. Use git pull instead.")
        return []
    sha = latest_sha()
    if installed().get("sha") == sha:
        if not quiet:
            print("Trader HQ is up to date.")
        return []
    req_before = (here / "requirements.txt").read_bytes() if (here / "requirements.txt").exists() else b""
    changed = install(_get(f"https://api.github.com/repos/{REPO}/zipball/{sha}", accept="application/vnd.github+json",
                           timeout=60), sha, here)
    if "requirements.txt" in changed and (here / "requirements.txt").read_bytes() != req_before:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(here / "requirements.txt")],
                       timeout=300, check=False)
    if not quiet:
        print(f"Updated to the latest version ({sha[:7]}): {len(changed)} file(s) changed." if changed
              else f"Trader HQ is up to date ({sha[:7]}).")
    return changed
