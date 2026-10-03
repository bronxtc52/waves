"""tmux адресуется точным именем (`=имя`): has-session -t wab-c-w1 иначе совпал бы с wab-c-w10. tmux не запускается."""
import pathlib
import re
import subprocess
import unittest
from unittest import mock

import helpers
from helpers import ROOT

import wab


class Recorder:
    def __init__(self, rc=0, stdout=""):
        self.calls = []
        self.rc, self.stdout = rc, stdout

    def __call__(self, *args, check=True, **kw):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.rc, self.stdout, "")


def targets(calls):
    out = []
    for c in calls:
        if "-t" in c:
            sub = [a for a in c[1:] if a != "-u"][0]  # -u (UTF-8) стоит до подкоманды
            out.append((sub, c[c.index("-t") + 1]))
    return out


class TestExactTargets(unittest.TestCase):
    def run_with(self, fn, *args, rc=0, stdout=""):
        rec = Recorder(rc, stdout)
        with mock.patch.object(wab, "sh", rec), mock.patch.object(wab.time, "sleep"):
            fn(*args)
        return rec.calls

    def test_has_session_exact(self):
        calls = self.run_with(wab.tmux_alive, "wab-c-w1")
        self.assertEqual(calls, [["tmux", "has-session", "-t", "=wab-c-w1"]])

    def test_pane_targets_exact(self):
        self.assertEqual(targets(self.run_with(wab.pane_text, "wab-c-w1")),
                         [("capture-pane", "=wab-c-w1:")])
        self.assertEqual(targets(self.run_with(wab.send_text, "wab-c-w1", "x")),
                         [("paste-buffer", "=wab-c-w1:"), ("send-keys", "=wab-c-w1:")])
        self.assertEqual(targets(self.run_with(wab.send_command, "wab-c-w1", "x")),
                         [("send-keys", "=wab-c-w1:")] * 2)

    def test_wait_ready_trust_dialog_exact(self):
        rec = Recorder()
        seq = iter([wab.TRUST_MARKERS[0], wab.READY_MARKERS[0]])
        with mock.patch.object(wab, "sh", rec), mock.patch.object(wab.time, "sleep"), \
                mock.patch.object(wab, "pane_text", lambda n: next(seq)):
            wab.wait_ready("wab-c-w1", timeout=5)
        self.assertEqual(targets(rec.calls), [("send-keys", "=wab-c-w1:")] * 2)

    def test_no_bare_name_target_in_sources(self):
        """Ни один вызов tmux в scripts/ не адресует сессию голым именем."""
        for f in (ROOT / "scripts").iterdir():
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r'"-t",\s*([^,)\n]+)|-t\s+("?\$name"?)', text):
                tgt = m.group(1) or m.group(2)
                if f.name == "dash.py":
                    continue
                self.assertTrue("=" in tgt or "target" in tgt or "pane_" in tgt or "sess_" in tgt,
                                f"{f.name}: {m.group(0)}")

    def test_wab_open_exact(self):
        text = (ROOT / "scripts" / "wab-open").read_text(encoding="utf-8")
        self.assertIn('has-session -t "=$name"', text)
        self.assertIn('attach -f ignore-size -t "=$name"', text)


class TestTickDoneExit(unittest.TestCase):
    def test_exit_keys_exact(self):
        rec = Recorder()
        with mock.patch.object(wab, "sh", rec):
            wab.send_keys("wab-c-w1", "-l", "/exit", check=False)
            wab.send_keys("wab-c-w1", "Enter", check=False)
        self.assertEqual(targets(rec.calls), [("send-keys", "=wab-c-w1:")] * 2)
        self.assertEqual(rec.calls[0][-2:], ["-l", "/exit"])


if __name__ == "__main__":
    unittest.main()
