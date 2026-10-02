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

    def test_validate_bad_ref_component(self):
        for field in ("chain", "run_id"):
            for bad in (".hid", "a.lock", "A.LOCK", "a.", "a..b", "-x"):
                with self.subTest(field=field, value=bad):
                    d = good()
                    d[field] = bad
                    r = run("validate", str(write_json(self.dir, d)))
                    self.assertEqual(r.returncode, 2, r.stdout)
                    self.assertIn(field, r.stderr)

    def test_validate_missing_file(self):
        r = run("validate", str(self.dir / "nope.json"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("nope.json", r.stderr)

    def test_validate_not_utf8(self):
        p = self.dir / "waves.json"
        p.write_bytes(b"\xff\xfe{}")
        r = run("validate", str(p))
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_validate_deeply_nested(self):
        p = write_json(self.dir, "[" * 100000)
        r = run("validate", str(p))
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_import_has_no_side_effects(self):
        code = ("import sys, os; sys.path.insert(0, %r); before=set(os.listdir('.')); import wab; "
                "assert set(os.listdir('.'))==before; print('ok')" % str(ROOT / "scripts"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dash_compiles(self):
        import py_compile
        py_compile.compile(str(ROOT / "scripts" / "dash.py"), doraise=True,
                           cfile=str(self.dir / "dash.pyc"))

class LaunchMocks:
    """Моки побочных эффектов launch (tmux, worktree, state); вызовы пишутся в self.calls."""

    def setUp(self):
        from unittest import mock
        import wab
        self.wab = wab
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.cfg_path = write_json(self.dir, good())
        self.calls = []
        names = ["tmux_alive", "prepare_worktree", "sh", "save_state", "event", "send_text", "wait_ready"]
        self.m = {}
        for n in names:
            pt = mock.patch.object(wab, n, side_effect=lambda *a, _n=n, **k: self.calls.append(_n))
            self.m[n] = pt.start()
            self.addCleanup(pt.stop)
        self.m["tmux_alive"].side_effect = lambda *a, **k: False
        self.m["prepare_worktree"].side_effect = lambda *a, **k: self.calls.append("prepare_worktree") or str(self.dir)
        self.m["wait_ready"].side_effect = lambda *a, **k: False
        pt = mock.patch.object(wab, "load_state", return_value={"current": None, "waves": {}})
        pt.start()
        self.addCleanup(pt.stop)
        pt = mock.patch.object(wab, "wave_dir", return_value=self.dir)
        pt.start()
        self.addCleanup(pt.stop)


class TestLaunchGuards(LaunchMocks, unittest.TestCase):
    """launch: плохой промпт не оставляет следов; не готовое окно даёт ненулевой код."""

    def _bad_prompt(self, path):
        cfg = self.wab.load_waves(str(self.cfg_path))
        with self.assertRaises(SystemExit) as cm:
            self.wab.launch(cfg, "W1", str(path))
        self.assertIn(str(path), str(cm.exception))
        self.assertEqual([c for c in self.calls if c != "tmux_alive"], [])

    def test_missing_prompt_creates_nothing(self):
        self._bad_prompt(self.dir / "nope.md")

    def test_directory_prompt_creates_nothing(self):
        self._bad_prompt(self.dir)

    def test_non_utf8_prompt_creates_nothing(self):
        f = self.dir / "p.md"
        f.write_bytes(b"\xff\xfe\x80")
        self._bad_prompt(f)

    def test_blank_prompt_creates_nothing(self):
        f = self.dir / "p.md"
        f.write_text("  \n\t\n", encoding="utf-8")
        self._bad_prompt(f)

    def test_cli_exit_code_when_not_ready(self):
        import contextlib
        import io
        f = self.dir / "p.md"
        f.write_text("задача\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.wab.main(["launch", str(self.cfg_path), "W1", str(f)])
        self.assertEqual(rc, 3)
        self.assertIn("не отправлена", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestLaunchCurrentWave(LaunchMocks, unittest.TestCase):
    """launch: пока идёт другая волна (state.current), новая не запускается и ничего не создаётся."""

    SIDE_EFFECTS = ("prepare_worktree", "sh", "save_state", "send_text", "wait_ready", "event")

    def setUp(self):
        import copy
        from unittest import mock
        super().setUp()
        self.st = {"current": None, "waves": {}}
        pt = mock.patch.object(self.wab, "load_state", side_effect=lambda *a, **k: self.st)
        pt.start()
        self.addCleanup(pt.stop)
        self.copy = copy.deepcopy
        self.cfg = self.wab.load_waves(str(self.cfg_path))
        self.prompt = self.dir / "p.md"
        self.prompt.write_text("задача\n", encoding="utf-8")
        self.m["wait_ready"].side_effect = lambda *a, **k: True
        self.wave_dir = self.wab.wave_dir

    def _w1_running(self, phase):
        name = f"{self.cfg['tmux_prefix']}w1"
        self.st = {"current": "W1", "waves": {"W1": {"tmux": name, "cwd": str(self.dir), "started": 1.0,
                                                     "restarts": 0, "phase": phase, "notified": {}}}}
        return name

    def _refused(self):
        before = self.copy(self.st)
        with self.assertRaises(SystemExit) as cm:
            self.wab.launch(self.cfg, "W2", str(self.prompt))
        self.assertEqual([c for c in self.calls if c in self.SIDE_EFFECTS], [])
        self.wave_dir.assert_not_called()
        self.assertEqual(self.st, before)
        return str(cm.exception)

    def test_other_wave_alive_refused(self):
        name = self._w1_running("running")
        self.m["tmux_alive"].side_effect = lambda n, *a, **k: n == name
        msg = self._refused()
        self.assertIn("W1", msg)
        self.assertIn(name, msg)
        self.assertIn("DONE", msg)

    def test_other_wave_dead_refused_with_hint(self):
        name = self._w1_running("dead")
        msg = self._refused()
        self.assertIn("W1", msg)
        self.assertIn(name, msg)
        self.assertIn("launch", msg)

    def test_other_wave_not_ready_refused(self):
        self._w1_running("not_ready")
        msg = self._refused()
        self.assertIn("launch", msg)

    def test_no_current_launches(self):
        self.assertTrue(self.wab.launch(self.cfg, "W2", str(self.prompt)))
        self.assertIn("prepare_worktree", self.calls)
        self.assertIn("sh", self.calls)
        self.assertEqual(self.st["current"], "W2")

    def test_same_wave_without_session_relaunched(self):
        name = f"{self.cfg['tmux_prefix']}w2"
        self.st = {"current": "W2", "waves": {"W2": {"tmux": name, "cwd": str(self.dir), "started": 1.0,
                                                     "restarts": 0, "phase": "dead", "notified": {}}}}
        self.assertTrue(self.wab.launch(self.cfg, "W2", str(self.prompt)))
        self.assertIn("prepare_worktree", self.calls)
        self.assertEqual(self.st["current"], "W2")
        self.assertEqual(self.st["waves"]["W2"]["phase"], "running")

    def test_same_wave_alive_refused(self):
        name = f"{self.cfg['tmux_prefix']}w2"
        self.st = {"current": "W2", "waves": {"W2": {"tmux": name, "phase": "running", "notified": {}}}}
        self.m["tmux_alive"].side_effect = lambda n, *a, **k: n == name
        with self.assertRaises(SystemExit) as cm:
            self.wab.launch(self.cfg, "W2", str(self.prompt))
        self.assertIn("уже существует", str(cm.exception))
        self.assertNotIn("prepare_worktree", self.calls)


class TestSurrogateCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def test_validate_surrogate_rc2_no_traceback(self):
        d = good()
        d["waves"][0]["title"] = "a\ud800b"
        r = run("validate", str(write_json(self.dir, json.dumps(d))))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_ascii_io_with_cyrillic_ok(self):
        p = write_json(self.dir, good())
        r = subprocess.run([sys.executable, str(WAB), "validate", str(p)], capture_output=True,
                           text=True, env=dict(os.environ, PYTHONIOENCODING="ascii"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_ascii_stderr_error_message_ok(self):
        d = good()
        d["waves"][0]["title"] = "Каркас\ud800"
        p = write_json(self.dir, json.dumps(d))
        r = subprocess.run([sys.executable, str(WAB), "validate", str(p)], capture_output=True,
                           text=True, env=dict(os.environ, PYTHONIOENCODING="ascii"))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

