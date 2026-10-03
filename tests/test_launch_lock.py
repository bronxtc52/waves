"""launch под межпроцессной блокировкой прогона: два координатора не запускают две волны сразу.

tmux и git не трогаются: в дочерних процессах tmux_alive, prepare_worktree, sh, wait_ready и
send_text подменены, вызовы tmux new-session пишутся в общий журнал.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import good, write_json

import wab

# Дочерний координатор: launch одной волны с подменёнными побочными эффектами.
# argv: waves.json, волна, файл промпта, журнал new-session, каталог сигналов, точка остановки.
# Точка остановки ("prepare" или "tmux_alive"): дойдя до неё, процесс пишет <сигналы>/<волна>.in
# и ждёт файла <сигналы>/go. Пустая точка — без остановки.
CHILD = r"""
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import wab
cfg_path, wave, prompt, log, sig, stop = sys.argv[2:8]
sig = pathlib.Path(sig)

def pause(point):
    if stop == point:
        (sig / (wave + ".in")).write_text("1")
        deadline = time.monotonic() + 20
        while not (sig / "go").exists():
            if time.monotonic() > deadline:
                raise SystemExit("тест: не дождался go")
            time.sleep(0.02)

def tmux_alive(name):
    pause("tmux_alive")
    return False

def prepare_worktree(cfg, w):
    pause("prepare")
    return str(sig)

def sh(*args, **kw):
    with open(log, "a", encoding="utf-8") as f:
        f.write(wave + " " + " ".join(args[:3]) + "\n")

wab.tmux_alive = tmux_alive
wab.prepare_worktree = prepare_worktree
wab.sh = sh
wab.wait_ready = lambda *a, **k: True
wab.require_tmux = lambda: None
wab.ensure_roles = lambda cfg, refresh=False: (dict(cfg["roles"]), {})
wab.send_text = lambda *a, **k: None
(sig / (wave + ".started")).write_text("1")
try:
    ok = wab.launch(wab.load_waves(cfg_path), wave, prompt)
except SystemExit as e:
    print("REFUSED:", e)
    sys.exit(4)
print("OK" if ok else "NOT_READY")
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.cfg_path = write_json(self.dir, good())
        self.cfg = wab.load_waves(str(self.cfg_path))
        self.prompt = self.dir / "p.md"
        self.prompt.write_text("задача\n", encoding="utf-8")


class TestLaunchRace(_Base):
    """Две волны одновременно при пустом current: успешна ровно одна, new-session — один раз."""

    def setUp(self):
        super().setUp()
        self.sig = self.dir / "sig"
        self.sig.mkdir()
        self.log = self.dir / "new-session.log"
        self.procs = []

    def tearDown(self):
        (self.sig / "go").write_text("1")
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            p.stdout.close()

    def spawn(self, wave, stop):
        p = subprocess.Popen(
            [sys.executable, "-B", "-c", CHILD, str(helpers.ROOT / "scripts"), str(self.cfg_path), wave,
             str(self.prompt), str(self.log), str(self.sig), stop],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p

    def wait_file(self, name, timeout=20):
        deadline = time.monotonic() + timeout
        while not (self.sig / name).exists():
            self.assertLess(time.monotonic(), deadline, f"не дождался {name}")
            time.sleep(0.02)

    def finish(self, p):
        out = p.communicate(timeout=30)[0]
        return p.returncode, out

    def check_one_winner(self, a, b):
        rc_a, out_a = self.finish(a)
        rc_b, out_b = self.finish(b)
        self.assertEqual(rc_a, 0, out_a)
        self.assertEqual(rc_b, 4, out_b)
        self.assertIn("сейчас идёт волна W1", out_b)
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        self.assertEqual(lines, ["W1 tmux new-session -d"])
        st = wab.load_state(self.cfg)
        self.assertEqual(st["current"], "W1")
        self.assertNotIn("W2", st["waves"])
        self.assertEqual(st["waves"]["W1"]["phase"], "running")

    def test_reservation_before_worktree(self):
        # W1 прошёл проверки и готовит worktree; W2 стартует в этот момент и должен увидеть W1
        a = self.spawn("W1", "prepare")
        self.wait_file("W1.in")
        b = self.spawn("W2", "")
        try:
            b.wait(timeout=20)
        finally:
            (self.sig / "go").write_text("1")
        self.check_one_winner(a, b)

    def test_checks_under_lock(self):
        # W1 держит блокировку прогона посреди проверок; W2 обязан ждать её, а не проверять параллельно
        a = self.spawn("W1", "tmux_alive")
        self.wait_file("W1.in")
        b = self.spawn("W2", "")
        self.wait_file("W2.started")
        time.sleep(0.5)  # без блокировки W2 за это время успел бы пройти проверки и зарезервироваться
        (self.sig / "go").write_text("1")
        self.check_one_winner(a, b)


class _Real(_Base):
    """launch с настоящими load_state/save_state и подменёнными tmux/worktree."""

    def setUp(self):
        super().setUp()
        self.calls = []
        pt = helpers.stub_ensure_roles(wab)
        pt.start()
        self.addCleanup(pt.stop)
        for n, fn in {"tmux_alive": lambda *a, **k: False,
                      "sh": lambda *a, **k: self.calls.append("sh"),
                      "wait_ready": lambda *a, **k: True,
                      "send_text": lambda *a, **k: None,
                      "event": lambda *a, **k: None}.items():
            pt = mock.patch.object(wab, n, side_effect=fn)
            pt.start()
            self.addCleanup(pt.stop)


class TestLaunchRollback(_Real):
    """Сбой после резерва снимает его: цепочка не остаётся «занятой» неудачным launch."""

    def fail_prepare(self):
        def boom(*a, **k):
            raise SystemExit("git worktree add: сломалось")
        return mock.patch.object(wab, "prepare_worktree", side_effect=boom)

    def test_prepare_failure_releases_reservation(self):
        with self.fail_prepare(), self.assertRaises(SystemExit) as cm:
            wab.launch(self.cfg, "W1", str(self.prompt))
        self.assertIn("сломалось", str(cm.exception))
        st = wab.load_state(self.cfg)
        self.assertIsNone(st.get("current"))
        self.assertNotIn("W1", st["waves"])
        self.assertEqual(self.calls, [])

    def test_prepare_failure_restores_previous_record(self):
        prev = {"tmux": "wab-demo-w1", "cwd": "/x", "started": 1.0, "restarts": 2, "phase": "dead", "notified": {}}
        wab.save_state(self.cfg, {"current": "W1", "waves": {"W1": dict(prev), "W0": {"phase": "done"}}})
        with self.fail_prepare(), self.assertRaises(SystemExit):
            wab.launch(self.cfg, "W1", str(self.prompt))
        st = wab.load_state(self.cfg)
        self.assertEqual(st["current"], "W1")
        self.assertEqual(st["waves"]["W1"], prev)
        self.assertEqual(st["waves"]["W0"], {"phase": "done"})

    def test_new_session_failure_releases_reservation(self):
        def boom(*a, **k):
            raise RuntimeError("tmux упал")
        with mock.patch.object(wab, "prepare_worktree", return_value=str(self.dir)), \
                mock.patch.object(wab, "sh", side_effect=boom), self.assertRaises(RuntimeError):
            wab.launch(self.cfg, "W1", str(self.prompt))
        st = wab.load_state(self.cfg)
        self.assertIsNone(st.get("current"))
        self.assertNotIn("W1", st["waves"])

    def test_success_keeps_reservation(self):
        with mock.patch.object(wab, "prepare_worktree", return_value=str(self.dir)):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        st = wab.load_state(self.cfg)
        self.assertEqual(st["current"], "W1")
        self.assertEqual(st["waves"]["W1"]["phase"], "running")
        self.assertEqual(self.calls, ["sh"])

    def test_same_wave_starting_by_live_launcher_refused(self):
        # другой launch этой же волны ещё создаёт сессию: второй не должен создавать её же
        wab.save_state(self.cfg, {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-w1", "phase": "starting", "launcher_pid": os.getpid(), "notified": {}}}})
        with mock.patch.object(wab, "prepare_worktree", return_value=str(self.dir)), \
                self.assertRaises(SystemExit) as cm:
            wab.launch(self.cfg, "W1", str(self.prompt))
        self.assertIn("уже запускается", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_same_wave_starting_by_dead_launcher_relaunched(self):
        # launch убит посреди старта (kill -9): резерв без живого процесса не держит цепочку вечно
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        wab.save_state(self.cfg, {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-w1", "phase": "starting", "launcher_pid": dead.pid, "notified": {}}}})
        with mock.patch.object(wab, "prepare_worktree", return_value=str(self.dir)):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["phase"], "running")


class TestTickStarting(_Base):
    """watch во время launch: волна в фазе starting без сессии не объявляется мёртвой."""

    def st(self, pid):
        return {"current": "W1", "waves": {"W1": {"tmux": "wab-demo-w1", "cwd": None, "phase": "starting",
                                                  "launcher_pid": pid, "restarts": 0, "notified": {}}}}

    def test_live_launcher_waits(self):
        st = self.st(os.getpid())
        with mock.patch.object(wab, "tmux_alive", return_value=False), \
                mock.patch.object(wab, "event") as ev:
            self.assertTrue(wab.tick(self.cfg, st))
        self.assertEqual(st["waves"]["W1"]["phase"], "starting")
        ev.assert_not_called()

    def test_dead_launcher_is_dead(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        st = self.st(dead.pid)
        with mock.patch.object(wab, "tmux_alive", return_value=False), mock.patch.object(wab, "event"):
            self.assertFalse(wab.tick(self.cfg, st))
        self.assertEqual(st["waves"]["W1"]["phase"], "dead")


class TestRunLock(_Base):
    """run_lock: ожидание ограничено, ошибка понятная; файл блокировки в run_dir."""

    def test_timeout_is_clear_error(self):
        with wab.run_lock(self.cfg):
            t0 = time.monotonic()
            with self.assertRaises(SystemExit) as cm:
                with wab.run_lock(self.cfg, timeout=0.3):
                    pass
            self.assertLess(time.monotonic() - t0, 5)
        msg = str(cm.exception)
        self.assertIn("другой wab.py держит блокировку прогона", msg)
        self.assertIn(str(self.cfg["run_dir"]), msg)

    def test_released_after_exit(self):
        with wab.run_lock(self.cfg):
            pass
        with wab.run_lock(self.cfg, timeout=0.3):
            pass
        self.assertTrue((self.cfg["run_dir"] / "state.lock").is_file())

    def test_released_on_exception(self):
        with self.assertRaises(ValueError):
            with wab.run_lock(self.cfg):
                raise ValueError("x")
        with wab.run_lock(self.cfg, timeout=0.3):
            pass

    def test_symlink_lock_refused(self):
        self.cfg["run_dir"].mkdir(parents=True)
        target = self.dir / "elsewhere"
        target.write_text("", encoding="utf-8")
        (self.cfg["run_dir"] / "state.lock").symlink_to(target)
        with self.assertRaises(SystemExit) as cm:
            with wab.run_lock(self.cfg, timeout=0.3):
                pass
        self.assertIn("state.lock", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
