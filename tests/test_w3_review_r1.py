"""W3, круг исправлений 1: инвариант ожидания новой сессии, кеш журнала, FIFO на месте плана.

Каждый тест, который при регрессии мог бы повиснуть, ограничен по времени и падает, а не висит."""
import contextlib
import hashlib
import io
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import helpers
from helpers import deadline, good, write_json
from test_w3_sessions import _Tmp, assistant, clear_scaffold, user

import wab

MARK = "[wab:demo/2026-10-02/W1]"


class TestAwaitInvariant(_Tmp):
    """Волна, отправившая /clear, ждёт новую сессию: флаг ставится до /clear и снимается только привязкой."""

    def setUp(self):
        super().setUp()
        self.sent, self.cmds = [], []
        self.screen = "экран\n? for shortcuts"
        for name, kw in (("tmux_alive", {"return_value": True}),
                         ("pane_text", {"side_effect": lambda *a, **k: self.screen}),
                         ("send_text", {"side_effect": lambda *a, **k: self.sent.append(a)}),
                         ("send_keys", {"return_value": None}),
                         ("send_command", {"side_effect": lambda *a, **k: self.cmds.append(a)})):
            pt = mock.patch.object(wab, name, **kw)
            pt.start()
            self.addCleanup(pt.stop)
        self.journal("old", [assistant(0, 0, self.cfg["ctx_limit"] + 5)], mtime=1000)
        self.wdir = wab.wave_dir(self.cfg, "W1")

    def st(self, phase, **extra):
        w = {"tmux": "wab-demo-W1", "cwd": self.cwd, "phase": phase, "restarts": 1, "notified": {},
             "sessions": ["old"], "tokens": 123, "peak": 123, **extra}
        return {"current": "W1", "waves": {"W1": w}}

    def tick(self, st, status):
        (self.wdir / "status").write_text(status + "\n", encoding="utf-8")
        wab.save_state(self.cfg, st)
        with contextlib.redirect_stdout(io.StringIO()):
            wab.tick(self.cfg, st)
        return st["waves"]["W1"]

    def checkpoints(self):
        return [a for a in self.sent if "WAB-CHECKPOINT" in a[1]]

    def test_flag_saved_to_disk_before_clear_is_sent(self):
        seen = []
        wab.send_command.side_effect = lambda *a, **k: seen.append(
            wab.load_state(self.cfg)["waves"]["W1"].get("await_session"))
        self.tick(self.st("checkpoint", checkpoint_sent=True), "HANDOFF_READY")
        self.assertEqual(seen, [True])

    def test_resuming_blocked_manual_running_waits_for_new_session(self):
        st = self.st("resuming", clear_sent=True, clear_at=1.0)
        w = self.tick(st, "RUNNING")
        self.assertEqual(w["phase"], "not_ready")
        w = self.tick(st, "RUNNING")                      # владелец вручную продолжил
        self.assertEqual(w["phase"], "running")
        self.assertTrue(w.get("await_session"))
        self.tick(st, "RUNNING")
        self.assertEqual(self.checkpoints(), [])          # старый журнал выше порога не меряется
        self.journal("new", clear_scaffold() + [user(f"{MARK} продолжаем"), assistant(0, 0, 50)], mtime=2000)
        w = self.tick(st, "RUNNING")
        self.assertEqual((w["sessions"], w["await_session"], w["tokens"]), (["old", "new"], False, 50))
        self.assertEqual(self.checkpoints(), [])

    def test_clearing_not_ready_manual_running_waits_for_new_session(self):
        self.screen = "пусто"
        st = self.st("checkpoint", checkpoint_sent=True)
        self.tick(st, "HANDOFF_READY")                    # clearing, /clear отправлен
        st["waves"]["W1"]["clear_at"] = time.time() - wab.READY_AFTER_CLEAR_SECONDS - 5
        w = self.tick(st, "HANDOFF_READY")
        self.assertEqual(w["phase"], "not_ready")
        w = self.tick(st, "RUNNING")
        self.assertEqual(w["phase"], "running")
        self.tick(st, "RUNNING")
        self.assertEqual(self.checkpoints(), [])
        self.journal("new", [user(f"{MARK} продолжаем"), assistant(0, 0, 50)], mtime=2000)
        w = self.tick(st, "RUNNING")
        self.assertEqual((w["await_session"], w["tokens"], w["phase"]), (False, 50, "running"))
        self.assertEqual(self.checkpoints(), [])

    def test_state_from_older_version_in_clearing_or_resuming_gets_flag(self):
        for phase in ("clearing", "resuming"):
            with self.subTest(phase=phase):
                st = self.st(phase, clear_sent=True, clear_at=time.time())
                w = self.tick(st, "RUNNING")
                self.assertTrue(w.get("await_session"))
                self.assertTrue(wab.load_state(self.cfg)["waves"]["W1"].get("await_session"))

    def test_watch_restart_in_each_phase_keeps_flag(self):
        for phase, extra in (("clearing", {"clear_sent": False}), ("clearing", {"clear_sent": True}),
                             ("resuming", {"clear_sent": True}), ("not_ready", {"await_session": True})):
            with self.subTest(phase=phase, extra=extra):
                st = self.st(phase, clear_at=time.time(), **extra)
                w = self.tick(st, "RUNNING")
                self.assertTrue(w.get("await_session"))


class TestContinuePromptFile(TestAwaitInvariant):
    """Текст продолжения лежит в файле волны до /clear; BLOCKED и событие таймаута называют файл."""

    def events(self):
        f = self.cfg["run_dir"] / "events.log"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def check_blocked(self, w):
        path = self.wdir / wab.CONTINUE_FILE
        status = (self.wdir / "status").read_text(encoding="utf-8")
        self.assertEqual(len(status.strip().splitlines()), 1)
        self.assertIn(wab.CONTINUE_FILE, status)
        self.assertIn(MARK, status)
        self.assertIn(str(path), self.events())

    def test_file_written_before_clear_and_starts_with_marker(self):
        seen = []
        wab.send_command.side_effect = lambda *a, **k: seen.append(
            (self.wdir / wab.CONTINUE_FILE).read_text(encoding="utf-8"))
        self.tick(self.st("checkpoint", checkpoint_sent=True), "HANDOFF_READY")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith(MARK), seen[0])
        self.assertIn(f"{self.wdir}/handoff.md", seen[0])

    def test_sent_text_equals_file(self):
        st = self.st("checkpoint", checkpoint_sent=True)
        self.tick(st, "HANDOFF_READY")
        st["waves"]["W1"]["clear_at"] = time.time() - wab.CLEAR_SETTLE_SECONDS - 1
        self.tick(st, "HANDOFF_READY")
        text = (self.wdir / wab.CONTINUE_FILE).read_text(encoding="utf-8").strip()
        self.assertEqual(self.sent[0][1], text)

    def test_blocked_from_resuming_names_file(self):
        w = self.tick(self.st("resuming", clear_sent=True, clear_at=1.0), "RUNNING")
        self.assertEqual(w["phase"], "not_ready")
        self.check_blocked(w)

    def test_blocked_from_clearing_timeout_names_file(self):
        self.screen = "пусто"
        st = self.st("checkpoint", checkpoint_sent=True)
        self.tick(st, "HANDOFF_READY")
        st["waves"]["W1"]["clear_at"] = time.time() - wab.READY_AFTER_CLEAR_SECONDS - 5
        w = self.tick(st, "HANDOFF_READY")
        self.assertEqual(w["phase"], "not_ready")
        self.check_blocked(w)

    def test_await_timeout_event_names_file(self):
        st = self.st("running", await_session=True, await_at=time.time() - 3600)
        self.tick(st, "RUNNING")
        ev = self.events()
        self.assertIn("не найдена по метке", ev)
        self.assertIn(str(self.wdir / wab.CONTINUE_FILE), ev)
        self.assertIn(MARK, ev)
        self.assertIn("без метки новая сессия не привяжется", ev)


class TestCacheReseek(_Tmp):
    def test_two_same_size_rewrites_of_one_inode_both_detected(self):
        a, b, c3 = assistant(0, 0, 111), assistant(0, 0, 222), assistant(0, 0, 333)
        self.assertEqual(len(a), len(c3))
        p = self.journal("s", [a, a])
        cache = wab.TranscriptCache()
        self.assertEqual(cache.read(p)["ctx"], 111)
        ino = p.stat().st_ino
        with open(p, "r+b") as f:
            f.write((b + "\n" + b + "\n").encode())
        self.assertEqual(cache.read(p)["ctx"], 222)
        with open(p, "r+b") as f:
            f.write((c3 + "\n" + c3 + "\n").encode())
        self.assertEqual(cache.read(p)["ctx"], 333)
        self.assertEqual(p.stat().st_ino, ino)

    def test_unchanged_file_after_reset_is_not_reread(self):
        a, b = assistant(0, 0, 111), assistant(0, 0, 222)
        p = self.journal("s", [a, a])
        cache = wab.TranscriptCache()
        cache.read(p)
        with open(p, "r+b") as f:
            f.write((b + "\n" + b + "\n").encode())
        self.assertEqual(cache.read(p)["ctx"], 222)
        before = cache.bytes_read
        self.assertEqual(cache.read(p)["ctx"], 222)
        self.assertEqual(cache.bytes_read, before)


class TestCacheHeadGrowth(_Tmp):
    """Проверяемое начало растёт вместе с файлом: пустой или короткий первый снимок не слепит проверку."""

    def _rewrite_same_size(self, first_bytes):
        a, b = assistant(0, 0, 111), assistant(0, 0, 222)
        self.assertEqual(len(a), len(b))
        p = self.tdir / "s.jsonl"
        p.write_bytes(b"x" * (first_bytes - 1) + b"\n" if first_bytes else b"")
        cache = wab.TranscriptCache()
        self.assertEqual(cache.read(p)["ctx"], 0)
        with open(p, "ab") as f:
            f.write((a + "\n").encode())
        self.assertEqual(cache.read(p)["ctx"], 111)
        ino, size = p.stat().st_ino, p.stat().st_size
        with open(p, "r+b") as f:
            f.seek(first_bytes)
            f.write((b + "\n").encode())
        self.assertEqual((p.stat().st_ino, p.stat().st_size), (ino, size))
        self.assertEqual(cache.read(p)["ctx"], 222)

    def test_rewrite_after_empty_first_read_detected(self):
        self._rewrite_same_size(0)

    def test_rewrite_after_100_byte_first_read_detected(self):
        self._rewrite_same_size(100)

    def test_unchanged_file_after_head_extension_is_not_reread(self):
        p = self.tdir / "s.jsonl"
        p.write_bytes(b"")
        cache = wab.TranscriptCache()
        cache.read(p)
        with open(p, "ab") as f:
            f.write((assistant(0, 0, 111) + "\n").encode())
        self.assertEqual(cache.read(p)["ctx"], 111)
        before = cache.bytes_read
        self.assertEqual(cache.read(p)["ctx"], 111)
        self.assertEqual(cache.bytes_read, before)


class TestPlanFifo(unittest.TestCase):
    def test_fifo_in_place_of_plan_is_refused_not_hung(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("нет mkfifo")
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            data = good()
            data["plan_sha256"] = hashlib.sha256(b"x").hexdigest()
            cfg = wab.load_waves(str(write_json(d, data)))
            os.mkfifo(d / "waves.md")
            with deadline(10), self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
                wab.check_plan(cfg)
            self.assertTrue(str(cm.exception).startswith("BLOCKED: plan changed since approval:"))


if __name__ == "__main__":
    unittest.main()


class TestPreClearSnapshot(TestAwaitInvariant):
    """Журнал, лежавший до /clear (непривязанный, с меткой прошлой попытки), не снимает ожидание."""

    def test_unbound_marked_journal_before_clear_is_not_new_session(self):
        self.journal("j1", clear_scaffold() + [user(f"{MARK} продолжаем"), assistant(0, 0, 10)], mtime=1500)
        st = self.st("checkpoint", checkpoint_sent=True)
        self.tick(st, "HANDOFF_READY")                    # checkpoint -> clearing, снимок
        st["waves"]["W1"]["clear_at"] = time.time() - wab.CLEAR_SETTLE_SECONDS - 5
        self.tick(st, "HANDOFF_READY")                    # clearing -> resuming -> running
        self.tick(st, "RUNNING")                          # ожидание новой сессии
        w = st["waves"]["W1"]
        self.assertTrue(w["await_session"])
        self.assertEqual(w["sessions"], ["old"])
        self.assertEqual(w["pre_clear"], ["j1", "old"])
        self.journal("new", clear_scaffold() + [user(f"{MARK} продолжаем"), assistant(0, 0, 50)], mtime=2000)
        self.tick(st, "RUNNING")
        w = st["waves"]["W1"]
        self.assertEqual((w["sessions"], w["await_session"], w["tokens"]), (["old", "new"], False, 50))
        self.assertNotIn("pre_clear", w)

    def test_snapshot_on_disk_before_clear_is_sent(self):
        seen = []
        wab.send_command.side_effect = lambda *a, **k: seen.append(
            wab.load_state(self.cfg)["waves"]["W1"].get("pre_clear"))
        self.tick(self.st("checkpoint", checkpoint_sent=True), "HANDOFF_READY")
        self.assertEqual(seen, [["old"]])

    def test_record_without_pre_clear_still_binds_marked_journal(self):
        self.journal("new", clear_scaffold() + [user(f"{MARK} продолжаем"), assistant(0, 0, 50)], mtime=2000)
        st = self.st("running", await_session=True)
        w = self.tick(st, "RUNNING")
        self.assertNotIn("pre_clear", w)
        self.assertEqual((w["sessions"], w["await_session"]), (["old", "new"], False))


class TestMarkerAtStart(TestAwaitInvariant):
    """Привязка только по метке в начале первого сообщения (или <command-args>); цитата в середине — нет."""

    def bound(self, first_text):
        self.journal("new", [user(first_text), assistant(0, 0, 50)], mtime=2000)
        return wab.find_new_session(self.cfg, self.st("running", await_session=True), "W1")

    def test_quoted_marker_in_middle_not_bound(self):
        self.assertIsNone(self.bound(f"прочитай continue-prompt.md: {MARK} продолжаем"))

    def test_marker_at_start_bound(self):
        self.assertEqual(self.bound(f"  {MARK} продолжаем"), "new")

    def test_slash_command_marker_at_start_of_args_bound(self):
        t = ("<command-message>update</command-message>\n<command-name>/update</command-name>\n"
             f"<command-args>  {MARK} продолжаем</command-args>")
        self.assertEqual(self.bound(t), "new")

    def test_slash_command_marker_not_at_start_of_args_not_bound(self):
        t = ("<command-message>update</command-message>\n<command-name>/update</command-name>\n"
             f"<command-args>см. {MARK} продолжаем</command-args>")
        self.assertIsNone(self.bound(t))


class TestResumingConfirmedByJournal(TestAwaitInvariant):
    def resuming(self):
        return self.st("resuming", clear_sent=True, clear_at=1.0, await_session=True, pre_clear=["old"])

    def test_delivered_continue_found_in_journal_goes_running(self):
        self.journal("new", [user(f"{MARK} продолжаем"), assistant(0, 0, 50)], mtime=2000)
        st = self.resuming()
        w = self.tick(st, "RUNNING")
        self.assertEqual((w["phase"], w["sessions"], w["await_session"], w["restarts"]),
                         ("running", ["old", "new"], False, 2))
        self.assertNotIn("pre_clear", w)
        self.assertNotIn("clear_at", w)
        self.assertEqual((self.wdir / "status").read_text(encoding="utf-8").strip(), "RUNNING")
        self.assertIn("доставлено (подтверждено журналом", (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))

    def test_resuming_without_journal_still_blocked(self):
        w = self.tick(self.resuming(), "RUNNING")
        self.assertEqual(w["phase"], "not_ready")
        self.assertTrue((self.wdir / "status").read_text(encoding="utf-8").startswith("BLOCKED"))
