"""CLI wab.py: справка, validate, импорт без побочных эффектов."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import helpers
from helpers import ROOT, good, write_json

WAB = ROOT / "scripts" / "wab.py"


def run(*args):
    return subprocess.run([sys.executable, str(WAB), *args], capture_output=True, text=True)


class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def test_help(self):
        r = run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in ("launch", "watch", "status", "validate"):
            self.assertIn(cmd, r.stdout)

    def test_no_args_is_error(self):
        r = run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("usage", (r.stdout + r.stderr).lower())

    def test_validate_good(self):
        p = write_json(self.dir, good())
        r = run("validate", str(p))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["chain"], "demo")
        self.assertEqual(out["tmux_prefix"], "wab-demo-")
        self.assertEqual(out["base_branch"], "main")
        self.assertFalse((self.dir / "runs").exists())

    def test_validate_bad(self):
        d = good()
        d["bogus"] = 1
        p = write_json(self.dir, d)
        r = run("validate", str(p))
        self.assertEqual(r.returncode, 2)
        self.assertIn("лишний ключ bogus", r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_validate_missing_file(self):
        r = run("validate", str(self.dir / "nope.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("nope.json", r.stderr)

    def test_import_has_no_side_effects(self):
        code = ("import sys, os; sys.path.insert(0, %r); before=set(os.listdir('.')); import wab; "
                "assert set(os.listdir('.'))==before; print('ok')" % str(ROOT / "scripts"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dash_compiles(self):
        import py_compile
        py_compile.compile(str(ROOT / "scripts" / "dash.py"), doraise=True,
                           cfile=str(self.dir / "dash.pyc"))


if __name__ == "__main__":
    unittest.main()
