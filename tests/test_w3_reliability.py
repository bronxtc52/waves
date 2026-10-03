"""W3: тесты на уже существовавшее поведение, которое переносилось из прототипа (грабли запуска и тика):
wait_ready по сроку, неготовое окно при launch, DONE/dead, дедупликация BLOCKED, перезапуск watch
в фазах running/checkpoint, многострочное событие, дашборд при гонке чтения. Без реального tmux и claude."""
import contextlib
import io
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import good, write_json

import wab
from test_w4_flow import Gh
from test_dash import dash, FakeRich, flatten


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.cfg_path = write_json(self.dir, good())
        self.cfg = wab.load_waves(str(self.cfg_path))

    def log(self):
        p = self.cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def patch(self, name, **kw):
        pt = mock.patch.object(wab, name, **kw)
        m = pt.start()
        self.addCleanup(pt.stop)
        return m

    def set_status(self, text, wave="W1"):
        d = wab.wave_dir(self.cfg, wave)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(text + "\n", encoding="utf-8")

    def tick(self, st):
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, st)


class TestWaitReady(_Base):
    def test_trust_dialog_down_then_enter_then_ready(self):
        seq = iter([wab.TRUST_MARKERS[0], wab.READY_MARKERS[0]])
        keys = []
        self.patch("pane_text", side_effect=lambda n: next(seq))
        self.patch("send_keys", side_effect=lambda n, *k, **kw: keys.append(k))
        with mock.patch.object(wab.time, "sleep"):
            self.assertTrue(wab.wait_ready("wab-demo-w1", timeout=90))
        self.assertEqual(keys, [("Down",), ("Enter",)])

    def test_not_ready_in_90s_returns_false(self):
        t = [1000.0]
        pt = mock.patch.object(wab.time, "time", side_effect=lambda: t[0])
        pt.start()
        self.addCleanup(pt.stop)
        pt = mock.patch.object(wab.time, "sleep", side_effect=lambda s: t.__setitem__(0, t[0] + s))
        pt.start()
        self.addCleanup(pt.stop)
        self.patch("pane_text", return_value="загрузка…")
        self.patch("send_keys", return_value=None)
        self.assertFalse(wab.wait_ready("wab-demo-w1", timeout=90))
        self.assertGreaterEqual(t[0] - 1000.0, 90)   # ждали весь срок, а не сдались раньше


class TestLaunchNotReady(_Base):
    def test_task_not_sent_status_blocked_phase_not_ready(self):
        prompt = self.dir / "p.md"
        prompt.write_text("задача\n", encoding="utf-8")
        sent = []
        pt = helpers.stub_ensure_roles(wab)
        pt.start()
        self.addCleanup(pt.stop)
        self.patch("tmux_alive", return_value=False)
        self.patch("prepare_worktree", return_value=str(self.dir / "wt"))
        self.patch("sh", return_value=None)
        self.patch("wait_ready", return_value=False)
        self.patch("send_text", side_effect=lambda n, t: sent.append(t))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(wab.launch(self.cfg, "W1", str(prompt)))
        self.assertEqual(sent, [])
        self.assertTrue(wab.read(wab.wave_path(self.cfg, "W1") / "status").startswith("BLOCKED"))
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["phase"], "not_ready")
        self.assertFalse((wab.wave_path(self.cfg, "W1") / "first-prompt.md").exists())
        self.assertIn("промпт НЕ отправлен", self.log())


class TestDoneAndDead(_Base):
    def state(self, **extra):
        return {"current": "W1", "waves": {"W1": {"tmux": "wab-demo-w1", "phase": "running", "restarts": 0,
                                                  "notified": {}, **extra}}}

    def test_done_with_dead_session_is_done_not_dead(self):
        # W4: DONE с мёртвым окном идёт в гейт мерджа, а не в dead; PR смержен — волна merged
        self.patch("tmux_alive", return_value=False)
        self.patch("send_keys", return_value=None)
        gh = Gh(self)
        gh.state = "MERGED"
        self.patch("gate_run", side_effect=gh)
        self.set_status("DONE")
        st = self.state(cwd=str(self.dir))
        self.assertFalse(self.tick(st))   # у W1 нет next-prompt.md: цепочка стоит
        self.assertEqual(st["waves"]["W1"]["phase"], "merged")
        self.assertNotIn("закрыта", self.log())
        self.assertIn("W1: DONE", self.log())

    def test_dead_window_without_done_is_dead_and_watch_exits(self):
        self.patch("tmux_alive", return_value=False)
        self.set_status("RUNNING")
        st = self.state()
        self.assertFalse(self.tick(st))
        self.assertEqual(st["waves"]["W1"]["phase"], "dead")
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["phase"], "dead")
        self.assertIn("закрыта", self.log())
        self.assertFalse(self.tick(st))
        self.assertEqual(self.log().count("закрыта"), 1)   # повторный тик не дублирует событие

    def test_watch_loop_returns_when_dead(self):
        self.patch("tmux_alive", return_value=False)
        self.set_status("RUNNING")
        wab.save_state(self.cfg, self.state())
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(wab.time, "sleep") as sl:
            wab._watch_loop(self.cfg, str(self.cfg_path))
        sl.assert_not_called()
        self.assertIn("watch остановлен", self.log())


class TestBlockedOnce(_Base):
    def setUp(self):
        super().setUp()
        self.patch("tmux_alive", return_value=True)
        self.patch("pane_text", return_value="экран")
        self.patch("send_text", return_value=None)
        self.st = {"current": "W1", "waves": {"W1": {"tmux": "wab-demo-w1", "phase": "running", "restarts": 0,
                                                     "notified": {}}}}

    def test_same_text_one_event_other_text_second_event(self):
        self.set_status("BLOCKED: нужен ключ")
        self.assertTrue(self.tick(self.st))
        self.assertTrue(self.tick(self.st))
        self.assertEqual(self.log().count("BLOCKED: нужен ключ"), 1)
        self.set_status("BLOCKED: нужен другой ключ")
        self.tick(self.st)
        self.assertEqual(self.log().count("BLOCKED"), 2)

    def test_no_repeat_after_state_reload(self):
        self.set_status("BLOCKED: нужен ключ")
        self.tick(self.st)
        wab.save_state(self.cfg, self.st)
        reloaded = wab.load_state(self.cfg)
        self.tick(reloaded)
        self.assertEqual(self.log().count("BLOCKED: нужен ключ"), 1)


class TestWatchRestartPhases(_Base):
    """Перезапущенный watch читает state с диска; launch при этом не вызывается."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.launch = self.patch("launch")
        self.patch("tmux_alive", return_value=True)
        self.patch("pane_text", return_value="экран")
        self.patch("send_text", side_effect=lambda n, t: self.sent.append(t))
        self.patch("context_tokens", return_value=10)
        self.set_status("RUNNING")

    def disk(self, **extra):
        wab.save_state(self.cfg, {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-w1", "cwd": "/x", "started": 1.0, "restarts": 0, "notified": {}, **extra}}})
        return wab.load_state(self.cfg)

    def test_running_after_restart_requests_nothing_below_limit(self):
        st = self.disk(phase="running", sessions=["s"])
        self.assertTrue(self.tick(st))
        self.assertEqual(self.sent, [])
        self.launch.assert_not_called()
        self.assertEqual(st["waves"]["W1"]["phase"], "running")

    def test_running_after_restart_above_limit_requests_checkpoint_once(self):
        self.patch("context_tokens", return_value=self.cfg["ctx_limit"] + 1)
        st = self.disk(phase="running", sessions=["s"])
        self.tick(st)
        self.tick(wab.load_state(self.cfg))
        self.assertEqual(len([s for s in self.sent if s.startswith("WAB-CHECKPOINT")]), 1)
        self.launch.assert_not_called()

    def test_checkpoint_unsent_is_sent_once_after_restart(self):
        st = self.disk(phase="checkpoint", checkpoint_at=time.time(), checkpoint_sent=False, sessions=["s"])
        self.tick(st)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("WAB-CHECKPOINT"))
        self.tick(wab.load_state(self.cfg))   # ещё одна перезагрузка state с диска
        self.assertEqual(len(self.sent), 1)
        self.launch.assert_not_called()

    def test_checkpoint_already_sent_is_not_resent(self):
        st = self.disk(phase="checkpoint", checkpoint_at=time.time(), checkpoint_sent=True, sessions=["s"])
        self.tick(st)
        self.assertEqual(self.sent, [])
        self.launch.assert_not_called()


class TestEventOneLine(_Base):
    def test_multiline_event_is_one_log_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.event(self.cfg, "W1: BLOCKED: первая\nвторая\r\nтретья четвёртая")
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 1)
        for part in ("первая", "вторая", "третья", "четвёртая"):
            self.assertIn(part, lines[0])
        self.assertEqual(len(out.getvalue().splitlines()), 1)


class FakeLayout(FakeRich):
    def __init__(self, *args, **kw):
        super().__init__(*args)
        self.children = {}

    def split_column(self, *parts):
        self.parts.extend(parts)

    split_row = split_column

    def __getitem__(self, key):
        if key not in self.children:
            self.children[key] = FakeLayout()
            self.parts.append(self.children[key])
        return self.children[key]


class FakeTable(FakeRich):
    def add_column(self, *a, **k):
        pass

    def add_row(self, *a, **k):
        self.parts.extend(x for x in a if isinstance(x, (str, FakeRich)))


class TestDashboardRace(_Base):
    """Дашборд рисует кадр, пока диспетчер переписывает state.json/events.log: ни исключения, ни пустого окна."""

    def setUp(self):
        super().setUp()
        for name, cls in (("Text", FakeRich), ("Panel", FakeRich), ("Group", FakeRich), ("Align", FakeRich),
                          ("Layout", FakeLayout), ("Table", FakeTable)):
            pt = mock.patch.object(dash, name, cls)
            pt.start()
            self.addCleanup(pt.stop)
        self.patch("tmux_alive", return_value=True)
        self.patch("pane_text", return_value="экран волны")
        self.set_status("RUNNING")
        self.st = {"current": "W1", "waves": {"W1": {"tmux": "wab-demo-w1", "cwd": None, "started": time.time(),
                                                      "phase": "running", "restarts": 0, "notified": {}}}}

    def frame(self):
        return flatten(dash.safe_render(self.cfg))

    def test_normal_frame(self):
        wab.save_state(self.cfg, self.st)
        (self.cfg["run_dir"] / "events.log").write_text("2026-10-03 10:00:00Z W1: запущена\n", encoding="utf-8")
        text = self.frame()
        self.assertNotIn("кадр не отрисован", text)
        self.assertIn("запущена", text)

    def test_state_json_missing(self):
        text = self.frame()   # state.json нет совсем
        self.assertNotIn("кадр не отрисован", text)

    def test_state_json_half_written(self):
        self.cfg["run_dir"].mkdir(parents=True, exist_ok=True)
        (self.cfg["run_dir"] / "state.json").write_text('{"current": "W1", "waves": {"W1": {"tm', encoding="utf-8")
        text = self.frame()   # кадр стоит одного повтора, исключение наружу не уходит
        self.assertIn("кадр не отрисован", text)
        wab.save_state(self.cfg, self.st)
        self.assertNotIn("кадр не отрисован", self.frame())   # следующий кадр рисуется

    def test_events_log_vanishes_between_frames(self):
        wab.save_state(self.cfg, self.st)
        log = self.cfg["run_dir"] / "events.log"
        log.write_text("2026-10-03 10:00:00Z W1: запущена\n", encoding="utf-8")
        self.assertIn("запущена", self.frame())
        log.unlink()
        text = self.frame()
        self.assertNotIn("кадр не отрисован", text)
        self.assertNotIn("запущена", text)

    def test_status_file_vanishes(self):
        wab.save_state(self.cfg, self.st)
        (wab.wave_dir(self.cfg, "W1") / "status").unlink()
        self.assertNotIn("кадр не отрисован", self.frame())


if __name__ == "__main__":
    unittest.main()
