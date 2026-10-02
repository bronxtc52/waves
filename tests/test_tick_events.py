"""tick() при DONE и вычистка секретов в event(): без tmux, всё внешнее подменено."""
import contextlib
import io
import pathlib
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

    def test_launch_command_survives_redact_on_long_paths(self):
        # длинный путь без дефисов и точек похож на «непрозрачную строку» вычистки
        deep = self.dir / "projectsdirectory" / "verylongprojectname" / "configurationfiles"
        deep.mkdir(parents=True)
        self.cfg_path = write_json(deep, good())
        self.cfg = wab.load_waves(str(self.cfg_path))
        wdir = self.finish("W1")
        st = self.state("W1")
        self.assertFalse(self.run_tick(st, waves_json=str(self.cfg_path)))
        self.assertIn(f"wab.py launch {self.cfg_path} W2 {wdir / 'next-prompt.md'}", self.log())

    def test_watch_passes_waves_json(self):
        seen = {}

        def fake_tick(cfg, st, waves_json=None):
            seen["waves_json"] = waves_json
            return False

        with mock.patch.object(wab, "tick", side_effect=fake_tick), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, str(self.cfg_path))
        self.assertEqual(seen["waves_json"], str(self.cfg_path))


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

    def test_secret_after_known_path_still_scrubbed(self):
        out = self.emit(f"W1: BLOCKED: {self.cfg['run_dir']}/{'Q' * 30}{TOKEN[4:]}")
        for where in (out, self.log()):
            self.assertNotIn(TOKEN[4:], where)
            self.assertIn("[скрыто]", where)

    def test_known_path_inside_secret_not_protected(self):
        root = str(self.cfg["run_dir"].parent.parent)
        secret = "Zx9" * 8 + root.replace("-", "").replace(".", "") + "Kq7" * 8
        if secret.count(root) == 0:
            self.skipTest("во временном пути есть дефис или точка")
        out = self.emit(f"W1: BLOCKED: {secret}")
        for where in (out, self.log()):
            self.assertNotIn("Zx9Zx9", where)
            self.assertNotIn("Kq7Kq7", where)

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
