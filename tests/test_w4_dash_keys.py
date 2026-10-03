"""W4: дашборд (новые фазы, строка гейта, status-bar tmux), порядок Ctrl+\\ в keys.tmux, остаток W3
(probe_model под C-локалью). tmux и claude не вызываются, кроме подставного `claude` во временном PATH
и необязательной проверки синтаксиса keys.tmux на приватном сокете."""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import helpers
from helpers import good, write_json
from test_dash import FakeRich, dash, flatten

import wab

ROOT = helpers.ROOT
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class _Dash(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.cfg = wab.load_waves(str(write_json(self.dir, good())))
        self.alive = True
        pt = mock.patch.object(dash.wab, "tmux_alive", side_effect=lambda n: self.alive)
        pt.start()
        self.addCleanup(pt.stop)

    def st(self, phase="running", status="RUNNING", wave="W1", current=True, **extra):
        d = wab.wave_dir(self.cfg, wave)
        (d / "status").write_text(status + "\n", encoding="utf-8")
        return {"current": wave if current else None,
                "waves": {wave: {"tmux": f"wab-demo-{wave.lower()}", "phase": phase, "cwd": None,
                                 "started": 1000.0, "restarts": 0, "notified": {}, **extra}}}

    def key(self, st, wave="W1"):
        return dash.wave_state(self.cfg, st, wave)[0]


class TestWaveStateW4(_Dash):
    def test_new_phases(self):
        self.assertEqual(self.key(self.st("gate", "DONE")), "gate")
        self.assertEqual(self.key(self.st("awaiting_merge", "DONE", pr={"number": 7})), "awaiting_merge")
        self.assertEqual(self.key(self.st("merge_unverified", "BLOCKED: merge gate: merge-коммит x")),
                         "merge_unverified")
        self.assertEqual(self.key(self.st("merged", "DONE", current=False)), "merged")
        for k in ("gate", "awaiting_merge", "merge_unverified", "merged"):
            self.assertIn(k, dash.STYLE)
        self.assertIn("green", dash.STYLE["merged"][1])
        self.assertIn("bold", dash.STYLE["awaiting_merge"][1])
        self.assertIn("bold", dash.STYLE["BLOCKED"][1])

    def test_dead_window_not_dead_in_gate_or_merged(self):
        self.alive = False
        self.assertEqual(self.key(self.st("running", "DONE")), "DONE")
        self.assertEqual(self.key(self.st("gate", "DONE")), "gate")
        self.assertEqual(self.key(self.st("merged", "DONE")), "merged")
        self.assertEqual(self.key(self.st("running", "RUNNING")), "dead")

    def test_labels(self):
        st = self.st("gate", "DONE", gate={"verdict": "wait", "reasons": ["нет check-runs на abc", "другое"]})
        self.assertEqual(dash.style_for(self.cfg, st, "W1")[3], "гейт: нет check-runs на abc")
        st = self.st("awaiting_merge", "DONE", pr={"number": 7, "url": "u", "sha": "s"})
        self.assertEqual(dash.style_for(self.cfg, st, "W1")[3], "ждёт мерджа PR #7")

    def test_gate_line_redacted(self):
        st = self.st("gate", "DONE", gate={"verdict": "wait", "reasons": [f"gh: token {TOKEN}"]})
        line = dash.gate_line(st["waves"]["W1"])
        self.assertIn("wait", line)
        self.assertNotIn(TOKEN[:12], line)
        self.assertEqual(dash.gate_line({}), "")


class TestStatusLine(_Dash):
    def test_blocked(self):
        self.assertEqual(dash.status_line(self.cfg, self.st(status="BLOCKED: вопрос")), "wab W1: ✋ BLOCKED")

    def test_awaiting_merge(self):
        st = self.st("awaiting_merge", "DONE", pr={"number": 7})
        self.assertEqual(dash.status_line(self.cfg, st), "wab W1: ждёт мерджа PR ##7")

    def test_running(self):
        self.assertEqual(dash.status_line(self.cfg, self.st()), "wab W1: работает")

    def test_no_current(self):
        line = dash.status_line(self.cfg, {"current": None, "waves": {}})
        self.assertTrue(line.startswith("wab"))

    def test_hash_escaped_length_and_redact(self):
        st = self.st("gate", "DONE", gate={"verdict": "wait",
                                           "reasons": ["#{pane_title} #[fg=red] " + TOKEN + " x" * 80]})
        line = dash.status_line(self.cfg, st)
        self.assertLessEqual(len(line), dash.STATUS_LINE_MAX)
        self.assertEqual(dash.STATUS_LINE_MAX, 40)   # = status-right-length tmux по умолчанию
        self.assertNotIn("\n", line)
        self.assertNotIn(TOKEN[:12], line)
        # каждая «#» удвоена: после снятия пар одиночных не остаётся
        self.assertNotIn("#", line.replace("##", ""))
        self.assertIn("##{", line)

    def test_percent_escaped(self):
        st = self.st("gate", "DONE", gate={"verdict": "wait", "reasons": ["100% %H:%M"]})
        line = dash.status_line(self.cfg, st)
        self.assertIn("100%%", line)
        self.assertNotIn("%", line.replace("%%", ""))
        self.assertLessEqual(len(line), 40)

    def test_no_line_breaks_from_status(self):
        st = self.st(status="BLOCKED: a\nb\rc")
        self.assertNotIn("\n", dash.status_line(self.cfg, st))


class TestTmuxStatusPush(_Dash):
    def test_set_option_once_per_change(self):
        calls = []
        run = lambda argv, **kw: (calls.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1]
        pusher = dash.TmuxStatus(run=run)
        st = self.st()
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/x,1,0"}):
            pusher.update(self.cfg, st)
            pusher.update(self.cfg, st)
            self.assertEqual(calls, [["tmux", "set-option", "status-right", "wab W1: работает"]])
            st = self.st(status="BLOCKED: q")
            pusher.update(self.cfg, st)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("-g", calls[1])
        self.assertNotIn("-t", calls[1])

    def test_outside_tmux_nothing(self):
        calls = []
        pusher = dash.TmuxStatus(run=lambda argv, **kw: calls.append(argv))
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        with mock.patch.dict(os.environ, env, clear=True):
            pusher.update(self.cfg, self.st())
        self.assertEqual(calls, [])

    def test_tmux_errors_swallowed(self):
        def boom(argv, **kw):
            raise FileNotFoundError("tmux")
        pusher = dash.TmuxStatus(run=boom)
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/x,1,0"}):
            pusher.update(self.cfg, self.st())          # не падает
        pusher = dash.TmuxStatus(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "err"))
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/x,1,0"}):
            pusher.update(self.cfg, self.st())


class TestCurrentPanelGate(_Dash):
    def test_gate_line_in_panel(self):
        st = self.st("gate", "DONE", gate={"verdict": "wait", "reasons": ["нет check-runs на abc"]},
                     tokens=1, ctx_hist=[1])
        patches = [mock.patch.object(dash, n, FakeRich) for n in ("Text", "Panel", "Group", "Align")]
        patches += [mock.patch.object(dash.wab, "pane_text", return_value="экран"),
                    mock.patch.object(dash, "box", mock.Mock(ROUNDED=None))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        text = flatten(dash.current_panel(self.cfg, st))
        self.assertIn("нет check-runs на abc", text)


class TestKeysOrder(unittest.TestCase):
    def bind(self):
        t = (ROOT / "keys.tmux").read_text(encoding="utf-8")
        return next(l for l in t.splitlines() if l.startswith('bind-key -n "C-\\\\"'))

    def test_ignore_size_checked_before_popup(self):
        line = self.bind()
        i_size, i_open = line.index("ignore-size"), line.index("#{@wab_open}")
        self.assertLess(i_size, i_open, "внутри попапа (клиент ignore-size) сначала detach-client")
        self.assertLess(line.index("detach-client"), line.index("display-popup"))
        self.assertLess(line.index("display-popup"), line.index('send-keys "C-\\\\"'))

    @unittest.skipUnless(shutil.which("tmux"), "нет tmux")
    def test_keys_parse_on_private_server(self):
        sock = f"wabkeysorder{os.getpid()}"
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        tm = lambda *a: subprocess.run(["tmux", "-L", sock, "-f", os.devnull, *a], capture_output=True,
                                       text=True, env=env)
        self.addCleanup(tm, "kill-server")
        r = tm("new-session", "-d", "-s", "k", "-x", "80", "-y", "20")
        if r.returncode != 0:
            self.skipTest(f"tmux не стартовал: {r.stderr}")
        r = tm("source-file", str(ROOT / "keys.tmux"))
        self.assertEqual(r.returncode, 0, r.stderr)
        keys = tm("list-keys", "-T", "root").stdout
        line = next(l for l in keys.splitlines() if "C-\\" in l)
        self.assertLess(line.index("detach-client"), line.index("display-popup"))


class TestProbeModelCLocale(unittest.TestCase):
    def test_utf8_output_under_c_locale(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            claude = d / "claude"
            claude.write_text("#!/bin/sh\nprintf 'Привет, ок \\342\\234\\224\\n'\nprintf 'ошибка\\n' >&2\n",
                              encoding="utf-8")
            claude.chmod(0o755)
            code = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(ROOT / 'scripts')!r})
                import wab
                print(wab.probe_model("m", timeout=20))
            """)
            env = {"PATH": f"{d}:{os.environ.get('PATH', '')}", "LC_ALL": "C", "LANG": "C",
                   "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "HOME": str(d)}
            r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env, timeout=60)
            self.assertNotIn("Traceback", r.stderr, r.stderr)
            self.assertEqual(r.stdout.strip(), "(True, 'rc=0')")


if __name__ == "__main__":
    unittest.main()
