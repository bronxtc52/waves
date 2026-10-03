"""Ctrl+\\ без привязки tmux = SIGQUIT процессу панели: watch и dash не должны от него умирать
(живой прогон 2026-10-03: watch завершился с EXIT=131 и унёс сессию tmux)."""
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import wab  # noqa: E402


@unittest.skipUnless(hasattr(signal, "SIGQUIT"), "нет SIGQUIT на этой платформе")
class IgnoreQuit(unittest.TestCase):
    def test_a_process_survives_sigquit_after_ignore_quit(self):
        code = ("import os, signal, sys; sys.path.insert(0, %r); import wab; wab.ignore_quit(); "
                "os.kill(os.getpid(), signal.SIGQUIT); print('alive')" % str(ROOT / "scripts"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "alive"), r.stderr)

    def test_wab_main_ignores_sigquit_first(self):
        with mock.patch.object(wab.signal, "signal") as sig:
            wab.main(["status", str(ROOT / "nonexistent-waves.json")])
        sig.assert_any_call(signal.SIGQUIT, signal.SIG_IGN)

    def test_dash_main_ignores_sigquit_first(self):
        try:
            import dash
        except SystemExit:  # нет rich: dash.py выходит на импорте — проверка не применима
            self.skipTest("dash.py требует rich")
        with mock.patch.object(wab.signal, "signal") as sig, mock.patch.object(sys, "argv", ["dash.py"]):
            with self.assertRaises(SystemExit):
                dash.main()
        sig.assert_any_call(signal.SIGQUIT, signal.SIG_IGN)


if __name__ == "__main__":
    unittest.main()
