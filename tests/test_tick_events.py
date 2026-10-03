"""tick() при DONE и вычистка секретов в event(): без tmux, всё внешнее подменено."""
import contextlib
import io
import os
import pathlib
import shlex
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)
from helpers import good, write_json

import wab
from test_w4_flow import Gh

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # 36 символов после префикса
MAIL = "someone.person@example.org"


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


class TestDone(_Base):
    """DONE и PR уже смержен (W4): окно закрыто, следующая волна — через pending_launch, tick её не запускает.

    Сам гейт подробно — в test_w4_flow; здесь gh/git подменены ответом «PR смержен, коммит в origin»."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.gh = Gh(self)
        self.gh.state = "MERGED"
        pt = mock.patch.object(wab, "gate_run", side_effect=lambda argv: self.gh(argv))
        pt.start()
        self.addCleanup(pt.stop)
        for name, kw in (("send_keys", {"side_effect": lambda *a, **k: self.sent.append(a)}),
                         ("tmux_alive", {"return_value": True}),
                         ("require_tmux", {"return_value": None}),   # watch() проверяет tmux; на CI-раннере его может не быть
                         ("launch", {"return_value": True})):
            pt = mock.patch.object(wab, name, **kw)
            setattr(self, "m_" + name, pt.start())
            self.addCleanup(pt.stop)

    def state(self, wave):
        return {"current": wave, "waves": {wave: {"tmux": f"wab-demo-{wave}", "phase": "running",
                                                  "restarts": 0, "notified": {}, "cwd": str(self.dir)}}}

    def finish(self, wave, next_prompt=True):
        wdir = wab.wave_dir(self.cfg, wave)
        (wdir / "status").write_text("DONE\n", encoding="utf-8")
        if next_prompt:
            (wdir / "next-prompt.md").write_text("промпт следующей волны\n", encoding="utf-8")
        return wdir

    def run_tick(self, st, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, st, **kw)

    def assert_stopped(self, st, wave, pending=None):
        self.m_launch.assert_not_called()   # запуск следующей волны — дело _watch_loop вне блокировки
        self.assertIsNone(st["current"])
        self.assertEqual(st["waves"][wave]["phase"], "merged")
        self.assertIn("finished", st["waves"][wave])
        saved = wab.load_state(self.cfg)
        self.assertIsNone(saved["current"])
        self.assertEqual(saved["waves"][wave]["phase"], "merged")
        self.assertEqual((saved.get("pending_launch") or {}).get("wave"), pending)
        self.assertIn(("wab-demo-" + wave, "-l", "/exit"), self.sent)

    def test_not_last_with_next_prompt_queues_next_wave(self):
        wdir = self.finish("W1")
        st = self.state("W1")
        self.assertTrue(self.run_tick(st, waves_json=str(self.cfg_path)))
        self.assert_stopped(st, "W1", pending="W2")
        log = self.log()
        self.assertIn("следующая волна W2 стартует автоматически", log)
        self.assertIn(f"wab.py launch {self.cfg_path} W2 {wdir / 'next-prompt.md'}", log)

    def test_not_last_without_waves_json_has_placeholder(self):
        wdir = self.finish("W1")
        st = self.state("W1")
        self.assertTrue(self.run_tick(st))
        self.assert_stopped(st, "W1", pending="W2")
        self.assertIn(f"wab.py launch <waves.json> W2 {wdir / 'next-prompt.md'}", self.log())

    def test_last_wave_chain_finished(self):
        self.finish("W2")
        st = self.state("W2")
        self.assertFalse(self.run_tick(st))
        self.assert_stopped(st, "W2")
        self.assertIn("цепочка завершена", self.log())
        self.assertNotIn("wab.py launch", self.log())

    def test_no_next_prompt_old_message(self):
        self.finish("W1", next_prompt=False)
        st = self.state("W1")
        self.assertFalse(self.run_tick(st))
        self.assert_stopped(st, "W1")
        self.assertIn("W1 готова, но нет next-prompt.md — следующую волну не запускаю", self.log())

    def launch_argv(self):
        """Команда launch из события DONE, разобранная как её разберёт shell."""
        line = next(l for l in self.log().splitlines() if "Следующая волна:" in l)
        return shlex.split(line.split("Следующая волна:", 1)[1])

    def check_launch_command(self, where):
        # пробел, апостроф, кириллица и длинный сегмент без дефисов (похож на «непрозрачную строку»)
        deep = self.dir / where / ("verylongprojectname" * 3)
        deep.mkdir(parents=True)
        self.cfg_path = write_json(deep, good())
        self.cfg = wab.load_waves(str(self.cfg_path))
        wdir = self.finish("W1")
        st = self.state("W1")
        self.assertTrue(self.run_tick(st, waves_json=str(self.cfg_path)))
        self.assertEqual(self.launch_argv(),
                         ["wab.py", "launch", str(self.cfg_path), "W2", str(wdir / "next-prompt.md")])

    def test_launch_command_path_with_space(self):
        self.check_launch_command("a b c")

    def test_launch_command_path_with_apostrophe(self):
        self.check_launch_command("q'uo te")

    def test_launch_command_path_cyrillic(self):
        self.check_launch_command("тест папка")

    def test_launch_command_path_with_dollar_backtick(self):
        self.check_launch_command("x$HOME `y`")

    def check_no_command_on_line_break(self, where):
        # перевод строки в пути: после склейки в одну строку команда указала бы на другой путь
        deep = self.dir / where
        deep.mkdir(parents=True)
        self.cfg_path = write_json(deep, good())
        self.cfg = wab.load_waves(str(self.cfg_path))
        self.finish("W1")
        st = self.state("W1")
        self.assertTrue(self.run_tick(st, waves_json=str(self.cfg_path)))
        self.assert_stopped(st, "W1", pending="W2")
        log = self.log()
        self.assertIn("W1: смержена; следующая волна W2 стартует автоматически.", log)
        self.assertIn("Путь содержит перевод строки — команду ручного запуска не печатаю.", log)
        self.assertNotIn("wab.py launch", log)
        self.assertNotIn("Следующая волна:", log)

    def test_no_command_path_with_lf(self):
        self.check_no_command_on_line_break("a\nb")

    def test_no_command_path_with_cr(self):
        self.check_no_command_on_line_break("a\rb")

    def test_no_command_path_with_unicode_line_separator(self):
        self.check_no_command_on_line_break("a b")

    def test_no_command_only_waves_json_with_line_break(self):
        # run_dir чистый, перевод строки только в переданном пути waves.json
        self.finish("W1")
        st = self.state("W1")
        self.assertTrue(self.run_tick(st, waves_json="/x\n/waves.json"))
        self.assert_stopped(st, "W1", pending="W2")
        self.assertIn("команду ручного запуска не печатаю", self.log())
        self.assertNotIn("wab.py launch", self.log())

    def test_watch_passes_absolute_waves_json(self):
        seen = {}

        def fake_tick(cfg, st, waves_json=None):
            seen["waves_json"] = waves_json
            return False

        with mock.patch.object(wab, "tick", side_effect=fake_tick), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, str(self.cfg_path))
        self.assertEqual(seen["waves_json"], str(self.cfg_path.resolve()))

    def test_watch_relative_waves_json_absolute_in_event(self):
        # относительный путь с длинным сегментом без дефисов: раньше становился «[скрыто].json»
        seg = "L" * 60
        (self.dir / seg).mkdir()
        rel_target = write_json(self.dir / seg, good())
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        rel = f"{seg}/waves.json"
        self.cfg = wab.load_waves(rel)
        wdir = self.finish("W1")
        st = self.state("W1")
        wab.save_state(self.cfg, st)
        with mock.patch.object(wab, "load_state", return_value=st), mock.patch.object(wab.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, rel)
        self.m_launch.assert_called_once_with(mock.ANY, "W2", str(wdir / "next-prompt.md"))
        self.assertNotIn("[скрыто].json", self.log())
        self.assertEqual(self.launch_argv(),
                         ["wab.py", "launch", str(rel_target.resolve()), "W2", str(wdir / "next-prompt.md")])


class TestEventRedact(_Base):
    """event(): секреты и почта не попадают ни в events.log, ни на экран."""

    def emit(self, text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.event(self.cfg, text)
        return out.getvalue()

    def test_token_scrubbed(self):
        out = self.emit(f"W1: BLOCKED: token={TOKEN}")
        for where in (out, self.log()):
            self.assertNotIn(TOKEN, where)
            self.assertNotIn("A1b2C3d4E5f6", where)
            self.assertIn("[скрыто]", where)

    def test_bare_github_token_scrubbed(self):
        out = self.emit(f"W1: BLOCKED: вот ключ {TOKEN} — что делать?")
        for where in (out, self.log()):
            self.assertNotIn(TOKEN, where)
            self.assertIn("[скрыто]", where)

    def test_mail_scrubbed(self):
        out = self.emit(f"W1: BLOCKED: написать на {MAIL}?")
        for where in (out, self.log()):
            self.assertNotIn(MAIL, where)
            self.assertIn("[скрыто]", where)

    def test_secrets_glued_to_paths_scrubbed(self):
        rd = str(self.cfg["run_dir"])
        jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".SflKxwRJSMeKKF2QT4fwpM"
        cases = {
            "sk": (f"{rd}/{rd}sk-abcdefghijklmnop1234567890", "abcdefghijklmnop1234567890"),
            "ghp": (f"{rd}/{rd}{TOKEN}", TOKEN[4:]),
            "jwt": (f"{rd}/{rd}{jwt}", "SflKxwRJSMeKKF2QT4fwpM"),
            "password": (f"{rd}/{rd}password=Hunter2Secret", "Hunter2Secret"),
            "token": (f"{rd}/{rd}token=Hunter3Secret", "Hunter3Secret"),
            "abc": ("abcsk-abcdefghijklmnop1234567890", "abcdefghijklmnop1234567890"),
        }
        for name, (text, secret) in cases.items():
            with self.subTest(name):
                out = self.emit(f"W1: BLOCKED: {text}")
                for where in (out, self.log()):
                    self.assertNotIn(secret, where)

    def test_nul_removed(self):
        out = self.emit("W1: BLOCKED: a\x00b \x000\x00 ghp_AAAA\x00" + "B" * 30)
        for where in (out, self.log()):
            self.assertNotIn("\x00", where)
            self.assertNotIn("B" * 30, where)

    def test_trusted_appended_without_redact(self):
        cmd = "/" + "verylongprojectname" * 4 + "/waves.json"
        out = self.emit_trusted(f"W1: {TOKEN}", f" команда: {cmd}\x00\nвторая")
        for where in (out, self.log()):
            self.assertNotIn(TOKEN, where)
            self.assertIn(cmd, where)
            self.assertNotIn("\x00", where)
        self.assertEqual(len(self.log().splitlines()), 1)

    def emit_trusted(self, text, trusted):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.event(self.cfg, text, trusted=trusted)
        return out.getvalue()

    def test_long_message_truncated(self):
        self.emit("слово " * 1000)
        line = self.log().splitlines()[-1]
        text = line.split("Z ", 1)[1]
        self.assertLessEqual(len(text), wab.REDACT_MESSAGE_LIMIT + 2)
        self.assertTrue(text.endswith("…"))

    def test_tick_blocked_secret_not_logged(self):
        wdir = wab.wave_dir(self.cfg, "W1")
        (wdir / "status").write_text(f"BLOCKED: нужен доступ, token={TOKEN}, пишите {MAIL}\n",
                                     encoding="utf-8")
        st = {"current": "W1", "waves": {"W1": {"tmux": "wab-demo-W1", "phase": "running",
                                                "restarts": 0, "notified": {}}}}
        with mock.patch.object(wab, "tmux_alive", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(wab.tick(self.cfg, st))
        log = self.log()
        self.assertIn("BLOCKED", log)
        for where in (out.getvalue(), log):
            self.assertNotIn(TOKEN, where)
            self.assertNotIn(MAIL, where)
            self.assertIn("[скрыто]", where)


class TestBlockedLongStatus(_Base):
    """Статус BLOCKED вычищается целиком до любого ограничения длины: срез сырого текста
    оставил бы от секрета на границе обрывок короче порога шаблона, и он ушёл бы открытым."""

    def leaks(self, where):
        body = TOKEN[4:]
        return [body[i:i + 9] for i in range(len(body) - 8) if body[i:i + 9] in where]

    def tick_with_status(self, status):
        wdir = wab.wave_dir(self.cfg, "W1")
        (wdir / "status").write_text(status + "\n", encoding="utf-8")
        st = {"current": "W1", "waves": {"W1": {"tmux": "wab-demo-W1", "phase": "running",
                                                "restarts": 0, "notified": {}}}}
        with mock.patch.object(wab, "tmux_alive", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(wab.tick(self.cfg, st))
        return out.getvalue()

    def check(self, status):
        out = self.tick_with_status(status)
        log = self.log()
        self.assertIn("BLOCKED", log)
        for where in (out, log):
            self.assertEqual(self.leaks(where), [])
            self.assertIn("[скрыто]", where)

    def test_token_crosses_200th_char(self):
        for start in (181, 185, 190, 196):
            with self.subTest(start=start):
                self.setUp()
                # пробелы в заполнителе: сплошной текст скрыл бы шаблон «длинная непрозрачная строка»
                pad = start - len("BLOCKED: ")
                head = "BLOCKED: " + "слово " * (pad // 6) + "y" * (pad % 6)
                self.check(head + TOKEN + " дальше текст")

    def test_token_crosses_message_limit(self):
        # «W1: » + статус: токен начинается чуть левее REDACT_MESSAGE_LIMIT всего сообщения
        for start in (wab.REDACT_MESSAGE_LIMIT - 20, wab.REDACT_MESSAGE_LIMIT - 10):
            with self.subTest(start=start):
                self.setUp()
                pad = start - len("W1: ") - len("BLOCKED: ")
                self.check("BLOCKED: " + "слово " * (pad // 6) + "y" * (pad % 6) + TOKEN)

    def test_status_cmd_redacts(self):
        wdir = wab.wave_dir(self.cfg, "W1")
        (wdir / "status").write_text(f"BLOCKED: {'слово ' * 30}{TOKEN}\n", encoding="utf-8")
        wab.save_state(self.cfg, {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-W1", "phase": "running", "restarts": 0, "notified": {}}}})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            wab.status_cmd(self.cfg)
        self.assertEqual(self.leaks(out.getvalue()), [])
        self.assertIn("[скрыто]", out.getvalue())


class TestHandoffResume(_Base):
    """HANDOFF_READY: машина фаз по тикам (checkpoint -> clearing -> resuming -> running);
    каждое состояние на диске ДО действия, внутри tick нет долгого sleep и нет wait_ready."""

    def setUp(self):
        super().setUp()
        self.calls = []
        rec = lambda kind: (lambda *a, **k: self.disk(kind, a))
        self.ready = True
        self.screen = "экран\n? for shortcuts"
        self.disk_at = []  # что лежало в state.json в момент каждого внешнего действия

        for name, kw in (("tmux_alive", {"return_value": True}),
                         ("pane_text", {"side_effect": lambda *a, **k: self.screen}),
                         ("send_command", {"side_effect": rec("command")}),
                         ("send_text", {"side_effect": rec("text")}),
                         ("send_keys", {"side_effect": rec("keys")}),
                         ("wait_ready", {"side_effect": rec("wait_ready")})):
            pt = mock.patch.object(wab, name, **kw)
            pt.start()
            self.addCleanup(pt.stop)
        sleep = mock.patch.object(wab.time, "sleep", side_effect=rec("sleep"))
        sleep.start()
        self.addCleanup(sleep.stop)

    def disk(self, kind, args):
        self.calls.append((kind,) + args)
        p = wab.state_path(self.cfg)
        self.disk_at.append((kind, wab.load_state(self.cfg)["waves"]["W1"].copy() if p.exists() else None))

    def state(self, phase="checkpoint", **extra):
        w = {"tmux": "wab-demo-W1", "cwd": str(self.dir), "phase": phase, "restarts": 2,
             "notified": {}, "checkpoint_at": 1.0, "checkpoint_sent": True, "sessions": ["s0"], **extra}
        return {"current": "W1", "waves": {"W1": w}}

    def tick(self, st, status="HANDOFF_READY"):
        wdir = wab.wave_dir(self.cfg, "W1")
        (wdir / "status").write_text(status + "\n", encoding="utf-8")
        wab.save_state(self.cfg, st)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wab.tick(self.cfg, st))
        return st["waves"]["W1"], wdir

    def kinds(self):
        return [c[0] for c in self.calls]

    def test_first_tick_saves_clearing_then_sends_clear_no_sleep(self):
        w, _ = self.tick(self.state())
        self.assertEqual(self.kinds(), ["command"])
        self.assertEqual(self.calls[0][1:], ("wab-demo-W1", "/clear"))
        before = self.disk_at[0][1]
        self.assertEqual((before["phase"], before["clear_sent"]), ("clearing", "sending"))
        self.assertIn("clear_at", before)
        self.assertEqual((w["phase"], w["clear_sent"]), ("clearing", True))
        saved = wab.load_state(self.cfg)["waves"]["W1"]
        self.assertEqual((saved["phase"], saved["clear_sent"]), ("clearing", True))
        self.assertEqual(w["restarts"], 2)

    def test_no_wait_ready_and_no_long_sleep_in_any_tick(self):
        import time as _t
        st = self.state()
        self.tick(st)
        st["waves"]["W1"]["clear_at"] = _t.time() - 10
        self.tick(st)
        self.assertNotIn("wait_ready", self.kinds())
        for c in self.calls:
            if c[0] == "sleep":
                self.assertLessEqual(c[1], 1.0)

    def test_before_settle_nothing_happens(self):
        import time as _t
        w, _ = self.tick(self.state("clearing", clear_sent=True, clear_at=_t.time()))
        self.assertEqual(self.calls, [])
        self.assertEqual(w["phase"], "clearing")

    def test_ready_after_settle_resumes_with_marker(self):
        import time as _t
        w, wdir = self.tick(self.state("clearing", clear_sent=True, clear_at=_t.time() - wab.CLEAR_SETTLE_SECONDS - 1))
        self.assertEqual(self.kinds(), ["text"])
        text = self.calls[0][2]
        self.assertTrue(text.startswith(wab.session_marker(self.cfg, "W1")), text)
        self.assertFalse(text.startswith("/"))
        self.assertIn(f"{wdir}/handoff.md", text)
        self.assertNotIn("/update", text)
        # на диске в момент отправки — resuming
        self.assertEqual(self.disk_at[0][1]["phase"], "resuming")
        self.assertEqual((w["phase"], w["restarts"], w["await_session"]), ("running", 3, True))
        self.assertIsNone(w["checkpoint_at"])
        self.assertEqual((wdir / "status").read_text(encoding="utf-8").strip(), "RESUMING")
        saved = wab.load_state(self.cfg)["waves"]["W1"]
        self.assertEqual((saved["phase"], saved["restarts"], saved["await_session"]), ("running", 3, True))
        self.assertIn("W1: handoff готов, /clear и продолжение (перезапуск №3)", self.log())

    def test_trust_dialog_accepted_without_loop(self):
        import time as _t
        self.screen = wab.TRUST_MARKERS[0]
        w, _ = self.tick(self.state("clearing", clear_sent=True, clear_at=_t.time() - 10))
        keys = [c[2] for c in self.calls if c[0] == "keys"]
        self.assertEqual(keys, ["Down", "Enter"])
        self.assertNotIn("text", self.kinds())
        self.assertEqual(w["phase"], "clearing")

    def test_restart_in_clearing_resends_clear_only_if_unsent(self):
        import time as _t
        self.tick(self.state("clearing", clear_sent=False, clear_at=_t.time() - 100))
        self.assertEqual(self.kinds(), ["command"])
        self.calls.clear()
        self.tick(self.state("clearing", clear_sent=True, clear_at=_t.time()))
        self.assertEqual(self.calls, [])

    def test_not_ready_after_90s_blocks_without_prompt(self):
        import time as _t
        self.screen = "пусто"
        w, wdir = self.tick(self.state("clearing", clear_sent=True, clear_at=_t.time() - 95))
        self.assertNotIn("text", self.kinds())
        self.assertEqual(w["phase"], "not_ready")
        status = (wdir / "status").read_text(encoding="utf-8").strip()
        self.assertTrue(status.startswith("BLOCKED: окно Claude не стало готовым после /clear, продолжение не отправлено"))
        self.assertEqual(w["restarts"], 2)
        self.assertIn("W1: BLOCKED", self.log())
        self.assertNotIn("перезапуск", self.log())

    def test_not_ready_yet_within_90s_waits(self):
        import time as _t
        self.screen = "пусто"
        w, _ = self.tick(self.state("clearing", clear_sent=True, clear_at=_t.time() - 30))
        self.assertEqual((w["phase"], self.calls), ("clearing", []))

    def test_restart_in_resuming_never_resends_blindly(self):
        w, wdir = self.tick(self.state("resuming", clear_sent=True, clear_at=1.0))
        self.assertEqual(self.kinds(), [])
        self.assertEqual(w["phase"], "not_ready")
        status = (wdir / "status").read_text(encoding="utf-8")
        self.assertIn("BLOCKED", status)
        self.assertIn("продолжение могло не дойти", status)
        self.assertIn("W1: BLOCKED", self.log())


class TestNotReadyRecovery(_Base):
    """not_ready: владелец вручную вернул волну в работу (RUNNING) — надзор и контрольные точки снова идут."""

    def setUp(self):
        super().setUp()
        self.texts = []
        self.tokens = 1000
        for name, kw in (("tmux_alive", {"return_value": True}),
                         ("pane_text", {"return_value": "экран\n? for shortcuts"}),
                         ("context_tokens", {"side_effect": lambda *a, **k: self.tokens}),
                         ("send_text", {"side_effect": lambda *a, **k: self.texts.append(a)}),
                         ("send_keys", {"return_value": None}),
                         ("send_command", {"return_value": None})):
            pt = mock.patch.object(wab, name, **kw)
            pt.start()
            self.addCleanup(pt.stop)
        self.wdir = wab.wave_dir(self.cfg, "W1")
        self.blocked = "BLOCKED: окно Claude не стало готовым, задача не отправлена"
        self.st = {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-W1", "cwd": str(self.dir), "phase": "not_ready", "restarts": 0,
            "notified": {"blocked": self.blocked}}}}

    def tick(self, status):
        (self.wdir / "status").write_text(status + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, self.st)

    def test_running_restores_phase_once_then_checkpoint(self):
        self.assertTrue(self.tick("RUNNING"))
        w = self.st["waves"]["W1"]
        self.assertEqual(w["phase"], "running")
        self.assertNotIn("blocked", w["notified"])
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["phase"], "running")
        self.assertTrue(self.tick("RUNNING"))
        self.assertEqual(self.log().count("W1: восстановлена вручную, слежу дальше"), 1)
        self.assertEqual(self.texts, [])
        # переполнение контекста на следующем тике — запрошена контрольная точка
        self.tokens = self.cfg["ctx_limit"]
        self.assertTrue(self.tick("RUNNING"))
        self.assertEqual(w["phase"], "checkpoint")
        self.assertEqual(len(self.texts), 1)
        self.assertIn("WAB-CHECKPOINT", self.texts[0][1])
        self.assertEqual(self.log().count("восстановлена вручную"), 1)

    def test_blocked_again_is_reported_after_recovery(self):
        self.tick("RUNNING")
        self.tick(self.blocked)
        self.assertEqual(self.log().count(self.blocked), 1)

    def test_other_statuses_keep_not_ready(self):
        for status in ("BLOCKED: вопрос", self.blocked, "RESUMING", "STARTING", ""):
            with self.subTest(status=status):
                self.tokens = self.cfg["ctx_limit"] + 1
                self.tick(status)
                w = self.st["waves"]["W1"]
                self.assertEqual(w["phase"], "not_ready")
                self.assertEqual(self.texts, [])
                self.assertNotIn("восстановлена вручную", self.log())

    def test_done_and_dead_win_over_recovery(self):
        gh = Gh(self)
        gh.state = "MERGED"
        with mock.patch.object(wab, "gate_run", side_effect=gh):
            self.tick("DONE")
        self.assertEqual(self.st["waves"]["W1"]["phase"], "merged")
        self.assertNotIn("восстановлена вручную", self.log())

    def test_dead_session_not_recovered(self):
        wab.tmux_alive.return_value = False
        self.assertFalse(self.tick("RUNNING"))
        self.assertEqual(self.st["waves"]["W1"]["phase"], "dead")
        self.assertNotIn("восстановлена вручную", self.log())


if __name__ == "__main__":
    unittest.main()
