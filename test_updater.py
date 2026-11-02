"""Run with: python -m unittest test_updater.py  (no network needed)"""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import trader as T
import updater as U

SHA = "a" * 40


def make_zip(files, root="amharms11-Trader-HQ-abc1234/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(root, "")
        for name, data in files.items():
            z.writestr(name if name.startswith("/") or name.startswith("../") else root + name, data)
    return buf.getvalue()


GOOD = {"trader.py": "new trader", "desktop.py": "new desktop", "dashboard.html": "<html>", "market.js": "js",
        "requirements.txt": "anthropic\n", "vendor/lightweight-charts.js": "lib", "ninjatrader/GoldORB.cs": "cs",
        "test_x.py": "t", ".github/workflows/x.yml": "nope", "vendor/sub/deep.js": "nope", "notes.exe": "nope"}


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "app"
        self.app.mkdir()
        (self.app / "trader.py").write_text("old trader")
        (self.app / "requirements.txt").write_text("anthropic\n")
        self.patches = [mock.patch.object(T, "DATA", self.tmp / "data"), mock.patch.object(T, "CONFIG_FILE", self.tmp / "c.env"),
                        mock.patch.object(T, "SOFI_CONFIG", self.tmp / "n.env"), mock.patch.dict(os.environ, {}, clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_only_program_files_are_wanted(self):
        for ok in ("trader.py", "dashboard.html", "market.js", "requirements.txt", "README.md", "vendor/lightweight-charts.js", "ninjatrader/GoldORB.cs"):
            self.assertTrue(U.wanted(ok), ok)
        for bad in ("../evil.py", "vendor/../../x.py", ".github/workflows/x.yml", "vendor/sub/deep.js", "notes.exe", "/etc/passwd", "vendor/.hidden"):
            self.assertFalse(U.wanted(bad), bad)

    def test_install_writes_program_files_only(self):
        changed = U.install(make_zip(GOOD), SHA, self.app)
        self.assertEqual((self.app / "trader.py").read_text(), "new trader")
        self.assertTrue((self.app / "vendor" / "lightweight-charts.js").exists())
        self.assertFalse((self.app / ".github").exists())
        self.assertFalse((self.app / "notes.exe").exists())
        self.assertNotIn("requirements.txt", changed)  # unchanged content isn't rewritten
        self.assertEqual(U.installed()["sha"], SHA)
        self.assertFalse(list(self.app.rglob("*.updating")))

    def test_install_rejects_things_that_arent_trader_hq(self):
        with self.assertRaises(RuntimeError):
            U.install(make_zip({"README.md": "hi"}), SHA, self.app)
        self.assertEqual((self.app / "trader.py").read_text(), "old trader")
        self.assertEqual(U.installed(), {})

    def test_traversal_entries_are_ignored(self):
        U.install(make_zip({**GOOD, "../escape.py": "x"}), SHA, self.app)
        self.assertFalse((self.tmp / "escape.py").exists())

    def test_update_flow(self):
        with mock.patch.object(U, "latest_sha", return_value=SHA), mock.patch.object(U, "_get", return_value=make_zip(GOOD)) as get, \
             mock.patch.object(U.subprocess, "run") as pip:
            self.assertIn("trader.py", U.update(quiet=True, here=self.app))
            self.assertIn(f"/zipball/{SHA}", get.call_args[0][0])
            pip.assert_not_called()  # requirements didn't change
            self.assertEqual(U.update(quiet=True, here=self.app), [])  # already current
            self.assertEqual(get.call_count, 1)

    def test_new_requirements_are_installed(self):
        files = {**GOOD, "requirements.txt": "anthropic\nnewthing\n"}
        with mock.patch.object(U, "latest_sha", return_value=SHA), mock.patch.object(U, "_get", return_value=make_zip(files)), \
             mock.patch.object(U.subprocess, "run") as pip:
            U.update(quiet=True, here=self.app)
        self.assertIn("-r", pip.call_args[0][0])

    def test_git_checkout_is_left_alone(self):
        (self.app / ".git").mkdir()
        with mock.patch.object(U, "latest_sha") as latest:
            self.assertEqual(U.update(quiet=True, here=self.app), [])
        latest.assert_not_called()

    def test_check_and_token(self):
        with mock.patch.object(U, "latest_sha", return_value=SHA):
            self.assertTrue(U.check()["available"])
            U.version_file().parent.mkdir(parents=True, exist_ok=True)
            U.version_file().write_text(json.dumps({"sha": SHA}))
            self.assertFalse(U.check()["available"])
        T.CONFIG_FILE.write_text("TRADERHQ_GITHUB_TOKEN=github_pat_abc\n")
        self.assertEqual(U.token(), "github_pat_abc")


if __name__ == "__main__":
    unittest.main()
