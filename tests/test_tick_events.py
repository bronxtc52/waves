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
    """DONE: окно закрыто, цепочка стоит, следующая волна не запускается сама."""

    def setUp(self):
        super().setUp()
        self.sent = []
        for name, kw in (("send_keys", {"side_effect": lambda *a, **k: self.sent.append(a)}),
                         ("tmux_alive", {"return_value": True}),
                         ("launch", {"return_value": True})):
            pt = mock.patch.object(wab, name, **kw)
            setattr(self, "m_" + name, pt.start())
            self.addCleanup(pt.stop)

    def state(self, wave):
        return {"current": wave, "waves": {wave: {"tmux": f"wab-demo-{wave}", "phase": "running",
                                                  "restarts": 0, "notified": {}}}}

    def finish(self, wave, next_prompt=True):
        wdir = wab.wave_dir(self.cfg, wave)
        (wdir / "status").write_text("DONE\n", encoding="utf-8")
        if next_prompt:
            (wdir / "next-prompt.md").write_text("промпт следующей волны\n", encoding="utf-8")
        return wdir

    def run_tick(self, st, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, st, **kw)

    def assert_stopped(self, st, wave):
        self.m_launch.assert_not_called()
        self.assertIsNone(st["current"])
        self.assertEqual(st["waves"][wave]["phase"], "done")
        self.assertIn("finished", st["waves"][wave])
        saved = wab.load_state(self.cfg)
        self.assertIsNone(saved["current"])
        self.assertEqual(saved["waves"][wave]["phase"], "done")
        self.assertIn(("wab-demo-" + wave, "-l", "/exit"), self.sent)

    def test_not_last_with_next_prompt_waits_for_merge(self):
        wdir = self.finish("W1")
        st = self.state("W1")
        self.assertFalse(self.run_tick(st, waves_json=str(self.cfg_path)))
        self.assert_stopped(st, "W1")
        log = self.log()
        self.assertIn("жду мерджа PR и координатора", log)
        self.assertIn(f"wab.py launch {self.cfg_path} W2 {wdir / 'next-prompt.md'}", log)

    def test_not_last_without_waves_json_has_placeholder(self):
        wdir = self.finish("W1")
        st = self.state("W1")
        self.assertFalse(self.run_tick(st))
        self.assert_stopped(st, "W1")
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
        self.assertFalse(self.run_tick(st, waves_json=str(self.cfg_path)))
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
        self.assertFalse(self.run_tick(st, waves_json=str(self.cfg_path)))
        self.assert_stopped(st, "W1")
        log = self.log()
        self.assertIn("W1: готова; жду мерджа PR и координатора.", log)
        self.assertIn("Путь содержит перевод строки — команду не печатаю, "
                      "запустите следующую волну W2 вручную.", log)
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
        self.assertFalse(self.run_tick(st, waves_json="/x\n/waves.json"))
        self.assert_stopped(st, "W1")
        self.assertIn("запустите следующую волну W2 вручную", self.log())
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
        with mock.patch.object(wab, "load_state", return_value=st), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, rel)
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


if __name__ == "__main__":
    unittest.main()
