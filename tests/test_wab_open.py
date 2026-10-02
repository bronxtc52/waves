"""scripts/wab-open и keys.tmux: путь установки с апострофом/пробелом, ошибки Python, экранирование."""
import json
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
from helpers import ROOT, good, write_json


def _exe(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class TestWabOpen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name)
        self.install = base / "O'Brien $x \"q\" dir" / "waves"
        shutil.copytree(ROOT / "scripts", self.install / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = base / "fake bin"
        self.bin.mkdir()
        self.log = base / "tmux.log"
        _exe(self.bin / "tmux", f'#!/bin/sh\necho "$@" >> "{self.log}"\n'
                                'case "$1" in has-session) exit 0;; esac\nexit 0\n')
        self.cfgdir = base / "cfg"
        self.cfgdir.mkdir()
        self.waves = write_json(self.cfgdir, good())

    def run_open(self, extra_bin=None, cfg=None):
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["PATH"] = f"{extra_bin or self.bin}{os.pathsep}{self.bin}{os.pathsep}{env['PATH']}"
        return subprocess.run(["bash", str(self.install / "scripts" / "wab-open"),
                               str(cfg or self.waves)], capture_output=True, text=True,
                              env=env, input="\n", timeout=30)

    def _state(self, current="W1"):
        run = self.cfgdir / "runs" / "2026-10-02"
        run.mkdir(parents=True)
        (run / "state.json").write_text(json.dumps(
            {"current": current, "waves": {"W1": {"tmux": "wab-demo-w1"}}}), encoding="utf-8")

    def test_odd_install_path_attaches(self):
        self._state()
        r = self.run_open()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Нет активной волны", r.stdout)
        self.assertIn("attach -f ignore-size -t =wab-demo-w1", self.log.read_text())

    def test_no_current_wave_message(self):
        r = self.run_open()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Нет активной волны", r.stdout)

    def test_python_failure_is_reported_not_masked(self):
        fake = self.bin.parent / "badpy"
        fake.mkdir()
        _exe(fake / "python3", "#!/bin/sh\necho boom >&2\nexit 1\n")
        r = self.run_open(extra_bin=fake)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Нет активной волны", r.stdout)
        self.assertIn("Ошибка", r.stderr)

    def test_bad_config_is_error_not_idle(self):
        bad = write_json(self.cfgdir, "{ not json", name="bad.json")
        r = self.run_open(cfg=bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Нет активной волны", r.stdout)
        self.assertIn("Ошибка", r.stderr)

    def test_no_path_interpolated_into_python_source(self):
        text = (ROOT / "scripts" / "wab-open").read_text(encoding="utf-8")
        self.assertNotIn("'$here'", text)


def _tmux_version():
    try:
        out = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout
        m = re.search(r"(\d+)\.(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except OSError:
        return (0, 0)


def _gnu_script():
    try:
        out = subprocess.run(["script", "--version"], capture_output=True, text=True)
        return "util-linux" in (out.stdout + out.stderr)
    except OSError:
        return False


@unittest.skipUnless(sys.platform.startswith("linux") and _tmux_version() >= (3, 2)
                     and _gnu_script() and shutil.which("sed"),
                     "нужны Linux, tmux >= 3.2 и script из util-linux")
class TestKeysTmuxLive(unittest.TestCase):
    """Живая проверка keys.tmux на приватном сокете (сервер по умолчанию не трогаем)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.sock = f"wabkeys{os.getpid()}"
        self.addCleanup(self.tmux, "kill-server")

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, "-f", os.devnull, *args],
                              capture_output=True, text=True,
                              env={k: v for k, v in os.environ.items() if k != "TMUX"})

    def test_popup_runs_wab_open_with_odd_path(self):
        d = self.base / "O'Brien $x \"q\" dir"
        d.mkdir()
        _exe(d / "wab-open", '#!/bin/sh\necho "$@" > "$(dirname "$0")/marker"\n')
        # тот же ключ, но F12: send-keys -K для C-\ в разных версиях tmux ведёт себя по-разному
        keys = (ROOT / "keys.tmux").read_text(encoding="utf-8")
        self.assertIn('"C-\\\\"', keys)
        conf = self.base / "k.tmux"
        conf.write_text(keys.replace('"C-\\\\" if-shell', '"F12" if-shell'), encoding="utf-8")
        self.assertEqual(self.tmux("new-session", "-d", "-s", "k", "-x", "100", "-y", "30").returncode, 0)
        r = self.tmux("source-file", str(ROOT / "keys.tmux"))
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.tmux("source-file", str(conf))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.tmux("set", "-g", "@wab_open", shlex.quote(str(d / "wab-open")) + " " + shlex.quote("a b")
                 + " " + shlex.quote("x\\$y"))
        client = subprocess.Popen(["script", "-qfc", f"env -u TMUX tmux -L {self.sock} attach -t =k",
                                   os.devnull], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (client.kill(), client.wait()))
        name = ""
        for _ in range(50):
            name = self.tmux("list-clients", "-F", "#{client_name}").stdout.strip()
            if name:
                break
            time.sleep(0.1)
        self.assertTrue(name, "клиент не подключился")
        self.tmux("send-keys", "-K", "-c", name.splitlines()[0], "F12")
        marker = d / "marker"
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "попап не запустил @wab_open")
        self.assertEqual(marker.read_text().strip(), "a b x\\$y")


if __name__ == "__main__":
    unittest.main()
