"""W3: привязка журнала к волне (session-id, метка, TranscriptCache, поиск новой сессии после /clear)."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
import uuid
from unittest import mock

import helpers
from helpers import good, write_json

import wab

MARK = "[wab:demo/2026-10-02/W1]"


def assistant(inp=0, cc=0, cr=0, sidechain=False):
    d = {"type": "assistant", "message": {"usage": {"input_tokens": inp, "cache_creation_input_tokens": cc,
                                                    "cache_read_input_tokens": cr, "output_tokens": 5}}}
    if sidechain:
        d["isSidechain"] = True
    return json.dumps(d)


def user(content, **extra):
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}, **extra},
                      ensure_ascii=False)


CLEAR_ECHO = ("<command-name>/clear</command-name>\n            <command-message>clear</command-message>\n"
              "            <command-args></command-args>")


def clear_scaffold():
    return [user("<local-command-caveat>Caveat: сообщения ниже созданы пользователем локально</local-command-caveat>",
                 isMeta=True),
            user(CLEAR_ECHO),
            user("<local-command-stdout></local-command-stdout>")]


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.projects = self.dir / "projects"
        self.projects.mkdir()
        pt = mock.patch.object(wab, "PROJECTS", self.projects)
        pt.start()
        self.addCleanup(pt.stop)
        self.cwd = str(self.dir / "wt")
        self.tdir = wab.transcript_dir(self.cwd)
        self.tdir.mkdir(parents=True)
        self.cfg = wab.load_waves(str(write_json(self.dir, good())))

    def journal(self, sid, lines, mtime=None):
        p = self.tdir / f"{sid}.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p


class TestContextTokens(_Tmp):
    def test_measures_only_own_journal_not_freshest(self):
        self.journal("own", [assistant(10, 20, 30)], mtime=1000)
        self.journal("other", [assistant(1, 1, 999_000)], mtime=2000)  # свежее и больше
        self.assertEqual(wab.context_tokens({"cwd": self.cwd, "sessions": ["own"]}), 60)

    def test_uses_last_session(self):
        self.journal("a", [assistant(0, 0, 500)])
        self.journal("b", [assistant(0, 0, 7)])
        self.assertEqual(wab.context_tokens({"cwd": self.cwd, "sessions": ["a", "b"]}), 7)

    def test_no_sessions_zero(self):
        self.journal("x", [assistant(0, 0, 500)])
        self.assertEqual(wab.context_tokens({"cwd": self.cwd}), 0)
        self.assertEqual(wab.context_tokens({"cwd": self.cwd, "sessions": []}), 0)

    def test_missing_journal_and_no_cwd_zero(self):
        self.assertEqual(wab.context_tokens({"cwd": self.cwd, "sessions": ["nope"]}), 0)
        self.assertEqual(wab.context_tokens({"cwd": None, "sessions": ["nope"]}), 0)

    def test_sidechain_ignored(self):
        self.journal("s", [assistant(0, 0, 100), assistant(0, 0, 900, sidechain=True)])
        self.assertEqual(wab.context_tokens({"cwd": self.cwd, "sessions": ["s"]}), 100)


class TestTranscriptCache(_Tmp):
    def test_incremental_reads_only_appended(self):
        p = self.journal("s", [assistant(0, 0, 100)])
        c = wab.TranscriptCache()
        self.assertEqual(c.read(p)["ctx"], 100)
        first = c.bytes_read
        with open(p, "a", encoding="utf-8") as f:
            f.write(assistant(0, 0, 250) + "\n")
        self.assertEqual(c.read(p)["ctx"], 250)
        self.assertEqual(c.bytes_read - first, len(assistant(0, 0, 250)) + 1)
        self.assertEqual(c.read(p)["ctx"], 250)
        self.assertEqual(c.bytes_read - first, len(assistant(0, 0, 250)) + 1)  # ничего нового — ничего не читали

    def test_partial_last_line_waits_for_newline(self):
        p = self.journal("s", [assistant(0, 0, 100)])
        c = wab.TranscriptCache()
        c.read(p)
        full = assistant(0, 0, 300)
        with open(p, "a", encoding="utf-8") as f:
            f.write(full[:20])
        self.assertEqual(c.read(p)["ctx"], 100)
        with open(p, "a", encoding="utf-8") as f:
            f.write(full[20:] + "\n")
        self.assertEqual(c.read(p)["ctx"], 300)

    def test_tail_on_first_read_discards_cut_line(self):
        lines = [assistant(0, 0, 11)] * 400 + [assistant(0, 0, 77)]
        p = self.journal("s", lines)
        size = p.stat().st_size
        c = wab.TranscriptCache(tail_bytes=2000)
        self.assertEqual(c.read(p)["ctx"], 77)
        self.assertLessEqual(c.bytes_read, 2000)
        self.assertLess(c.bytes_read, size)

    def test_tail_cut_line_is_dropped_even_if_it_parses(self):
        # обрезанный кусок строки сам по себе валидный JSON с ctx=999: его считать нельзя
        tail_piece = assistant(0, 0, 999)
        head = "x" * 50
        p = self.dir / "t.jsonl"
        p.write_text(head + tail_piece + "\n" + assistant(0, 0, 5) + "\n", encoding="utf-8")
        c = wab.TranscriptCache(tail_bytes=len(tail_piece) + 1 + len(assistant(0, 0, 5)) + 1)
        self.assertEqual(c.read(p)["ctx"], 5)

    def test_truncated_file_restarts(self):
        p = self.journal("s", [assistant(0, 0, 100), assistant(0, 0, 200)])
        c = wab.TranscriptCache()
        self.assertEqual(c.read(p)["ctx"], 200)
        p.write_text(assistant(0, 0, 9) + "\n", encoding="utf-8")
        self.assertEqual(c.read(p)["ctx"], 9)

    def test_replaced_file_restarts(self):
        p = self.journal("s", [assistant(0, 0, 100)] * 3)
        c = wab.TranscriptCache()
        c.read(p)
        q = self.dir / "new.jsonl"
        q.write_text("\n".join([assistant(0, 0, 42)] * 4) + "\n", encoding="utf-8")
        os.replace(q, p)
        self.assertEqual(c.read(p)["ctx"], 42)

    def test_rewritten_head_same_size_restarts(self):
        a, b = assistant(0, 0, 111), assistant(0, 0, 222)
        self.assertEqual(len(a), len(b))
        p = self.journal("s", [a, a])
        c = wab.TranscriptCache()
        self.assertEqual(c.read(p)["ctx"], 111)
        st = p.stat()
        with open(p, "r+b") as f:  # переписано на месте, inode и размер те же; хвост дописан
            f.write((b + "\n" + b + "\n").encode())
            f.write((assistant(0, 0, 333) + "\n").encode())
        self.assertEqual(c.read(p)["ctx"], 333)
        self.assertEqual(p.stat().st_ino, st.st_ino)

    def test_vanished_file_zero(self):
        c = wab.TranscriptCache()
        self.assertEqual(c.read(self.dir / "nope.jsonl")["ctx"], 0)


class TestFirstUserText(_Tmp):
    def text(self, lines):
        return wab._first_user_text(self.journal("f", lines))

    def test_plain_string(self):
        self.assertEqual(self.text([user("привет")]), "привет")

    def test_text_blocks_and_tool_result_skipped(self):
        c = [{"type": "tool_result", "content": "x"}, {"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
        self.assertEqual(self.text([user(c)]), "A\nB")

    def test_meta_and_sidechain_skipped(self):
        self.assertEqual(self.text([user("мета", isMeta=True), user("боковая", isSidechain=True), user("настоящее")]),
                         "настоящее")

    def test_clear_scaffold_skipped(self):
        self.assertEqual(self.text(clear_scaffold() + [user(f"{MARK} Продолжаем")]), f"{MARK} Продолжаем")

    def test_only_scaffold_gives_empty(self):
        self.assertEqual(self.text(clear_scaffold()), "")

    def test_only_first_256k_and_missing_file(self):
        big = user("x" * 300_000)
        self.assertEqual(self.text([big, user("поздно")]), "")
        self.assertEqual(wab._first_user_text(self.dir / "nope.jsonl"), "")


class TestFindNewSession(_Tmp):
    def st(self, **extra):
        return {"current": "W1", "waves": {"W1": {"cwd": self.cwd, "sessions": ["old"], **extra}}}

    def test_owned_sessions_include_attempts(self):
        st = {"waves": {"W1": {"sessions": ["a"], "attempts": [{"sessions": ["b"]}]}, "W2": {"sessions": ["c"]}}}
        self.assertEqual(wab.owned_sessions(st), {"a", "b", "c"})

    def test_marked_after_clear_is_bound(self):
        self.journal("old", [user(f"{MARK} первое")], mtime=1000)
        self.journal("new", clear_scaffold() + [user(f"{MARK} Продолжаем волну W1")], mtime=2000)
        self.assertEqual(wab.find_new_session(self.cfg, self.st(), "W1"), "new")

    def test_marker_only_in_meta_line_not_bound(self):
        self.journal("old", [user(f"{MARK} первое")])
        self.journal("new", [user(f"{MARK} вот так", isMeta=True), user("обычное сообщение без метки")])
        self.assertIsNone(wab.find_new_session(self.cfg, self.st(), "W1"))

    def test_marker_after_ordinary_message_not_bound(self):
        self.journal("new", [user("первое сообщение"), user(f"{MARK} второе")])
        self.assertIsNone(wab.find_new_session(self.cfg, self.st(), "W1"))

    def test_foreign_wave_marker_not_bound(self):
        self.journal("new", [user("[wab:demo/2026-10-02/W2] чужая")])
        self.assertIsNone(wab.find_new_session(self.cfg, self.st(), "W1"))

    def test_owned_by_other_wave_skipped_and_freshest_first(self):
        st = self.st()
        st["waves"]["W0"] = {"cwd": self.cwd, "sessions": ["mine0"]}
        self.journal("mine0", [user(f"{MARK} от прошлой попытки")], mtime=3000)
        self.journal("a", [user(f"{MARK} 1")], mtime=1000)
        self.journal("b", [user(f"{MARK} 2")], mtime=2000)
        self.assertEqual(wab.find_new_session(self.cfg, st, "W1"), "b")

    def test_no_dir_none(self):
        st = self.st()
        st["waves"]["W1"]["cwd"] = str(self.dir / "absent")
        self.assertIsNone(wab.find_new_session(self.cfg, st, "W1"))


class _TickBase(_Tmp):
    def setUp(self):
        super().setUp()
        self.sent = []
        for name, kw in (("tmux_alive", {"return_value": True}),
                         ("pane_text", {"return_value": "экран\n? for shortcuts"}),
                         ("send_text", {"side_effect": lambda *a, **k: self.sent.append(a)}),
                         ("send_keys", {"return_value": None}),
                         ("send_command", {"return_value": None})):
            pt = mock.patch.object(wab, name, **kw)
            pt.start()
            self.addCleanup(pt.stop)
        self.journal("old", [assistant(0, 0, self.cfg["ctx_limit"] + 5)], mtime=1000)

    def st(self, **extra):
        w = {"tmux": "wab-demo-W1", "cwd": self.cwd, "phase": "running", "restarts": 1, "notified": {},
             "sessions": ["old"], "await_session": True, "tokens": 123, "peak": 123, **extra}
        return {"current": "W1", "waves": {"W1": w}}

    def tick(self, st):
        (wab.wave_dir(self.cfg, "W1") / "status").write_text("RUNNING\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wab.tick(self.cfg, st))
        return st["waves"]["W1"]

    def events(self):
        f = self.cfg["run_dir"] / "events.log"
        return f.read_text(encoding="utf-8") if f.exists() else ""


class TestTickAwaitSession(_TickBase):
    def test_binds_new_session_and_measures_it(self):
        self.journal("new", clear_scaffold() + [user(f"{MARK} продолжаем"), assistant(0, 0, 4000)], mtime=2000)
        w = self.tick(self.st())
        self.assertEqual(w["sessions"], ["old", "new"])
        self.assertFalse(w["await_session"])
        self.assertEqual((w["tokens"], w["phase"]), (4000, "running"))
        self.assertIn("W1: новая сессия привязана по метке", (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8"))
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["sessions"], ["old", "new"])

    def test_waiting_keeps_tokens_and_requests_no_checkpoint(self):
        w = self.tick(self.st())   # старый журнал >= ctx_limit, новой сессии ещё нет
        self.assertTrue(w["await_session"])
        self.assertEqual((w["tokens"], w["phase"]), (123, "running"))
        self.assertEqual(self.sent, [])
        self.assertNotIn("контрольная точка", (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
                         if (self.cfg["run_dir"] / "events.log").exists() else "")

    def test_after_binding_checkpoint_only_by_new_journal(self):
        self.journal("new", [user(f"{MARK} продолжаем"), assistant(0, 0, 10)], mtime=2000)
        w = self.tick(self.st())
        self.assertEqual(w["phase"], "running")   # старый огромный журнал больше не меряется
        self.assertEqual(self.sent, [])

    # --- G: у ожидания новой сессии есть срок (AWAIT_SESSION_MINUTES) ---
    def test_await_timeout_one_event_no_phase_change(self):
        st = self.st(await_at=time.time() - (wab.AWAIT_SESSION_MINUTES + 1) * 60)
        w = self.tick(st)
        self.assertIn(f"новая сессия не найдена по метке за {wab.AWAIT_SESSION_MINUTES} мин", self.events())
        self.assertIn("контекст не меряется, контрольная точка не запрашивается", self.events())
        self.assertIn("tmux attach -t =wab-demo-W1", self.events())
        self.assertEqual((w["phase"], w["tokens"], w["await_session"]), ("running", 123, True))
        self.assertEqual(self.sent, [])
        self.tick(st)
        self.assertEqual(self.events().count("не найдена по метке"), 1)   # once_per: без повтора

    def test_await_within_deadline_silent(self):
        self.tick(self.st(await_at=time.time() - 60))
        self.assertNotIn("не найдена по метке", self.events())

    def test_await_timeout_then_found_binds_as_usual(self):
        st = self.st(await_at=time.time() - 3600)
        self.tick(st)
        self.journal("new", [user(f"{MARK} продолжаем"), assistant(0, 0, 4000)], mtime=2000)
        w = self.tick(st)
        self.assertEqual((w["sessions"], w["await_session"], w["tokens"]), (["old", "new"], False, 4000))
        self.assertIn("новая сессия привязана по метке", self.events())

    def test_await_without_await_at_starts_clock(self):
        w = self.tick(self.st())   # старое состояние без await_at: срок отсчитывается с этого тика
        self.assertIsInstance(w.get("await_at"), float)
        self.assertNotIn("не найдена по метке", self.events())


class TestMigrateNoSessions(_TickBase):
    """Запись волны до W3 (есть cwd, нет sessions): первый такт привязывает свободный журнал."""

    def st(self, **extra):
        st = super().st(**extra)
        w = st["waves"]["W1"]
        for k in ("sessions", "await_session", "tokens", "peak"):
            w.pop(k, None)
        return st

    def test_binds_freshest_unowned_and_requests_checkpoint(self):
        self.journal("older", [assistant(0, 0, 5)], mtime=500)
        self.journal("fresh", [assistant(0, 0, self.cfg["ctx_limit"] + 7)], mtime=3000)   # старый "old" тоже свободен, но свежее этот
        st = self.st()
        st["waves"]["W2"] = {"tmux": "x", "cwd": self.cwd, "phase": "done", "notified": {}, "sessions": ["older"]}
        w = self.tick(st)
        self.assertEqual(w["sessions"], ["fresh"])
        self.assertEqual(w["phase"], "checkpoint")
        self.assertTrue(any("WAB-CHECKPOINT" in a[1] for a in self.sent), self.sent)
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["sessions"], ["fresh"])
        self.assertEqual(self.events().count("запись без sessions (до W3): привязан журнал fresh как текущий"), 1)

    def test_no_journals_one_event_and_keeps_trying(self):
        for f in self.tdir.glob("*.jsonl"):
            f.unlink()
        st = self.st()
        w = self.tick(st)
        self.tick(st)
        self.assertNotIn("sessions", w)
        self.assertEqual(self.events().count("контекст не меряется: нет журнала"), 1)
        self.journal("late", [assistant(0, 0, 9)], mtime=4000)
        w = self.tick(st)
        self.assertEqual(w["sessions"], ["late"])
        self.assertEqual(w["tokens"], 9)


class TestLaunchSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.cfg = wab.load_waves(str(write_json(self.dir, good())))
        self.prompt = self.dir / "p.md"
        self.prompt.write_text("задача\n", encoding="utf-8")
        self.sh_calls, self.texts = [], []
        pt = helpers.stub_ensure_roles(wab)
        pt.start()
        self.addCleanup(pt.stop)
        for name, kw in {"tmux_alive": {"return_value": False},
                         "prepare_worktree": {"return_value": str(self.dir / "wt")},
                         "sh": {"side_effect": lambda *a, **k: self.sh_calls.append(a)},
                         "wait_ready": {"return_value": True},
                         "send_text": {"side_effect": lambda n, t: self.texts.append(t)}}.items():
            p = mock.patch.object(wab, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_session_id_in_state_argv_and_marker_in_first_message(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        w = wab.load_state(self.cfg)["waves"]["W1"]
        self.assertEqual(len(w["sessions"]), 1)
        sid = w["sessions"][0]
        self.assertEqual(str(uuid.UUID(sid)), sid)
        argv = list(self.sh_calls[0])
        self.assertEqual(argv[argv.index("--session-id") + 1], sid)
        marker = wab.session_marker(self.cfg, "W1")
        self.assertEqual(marker, "[wab:demo/2026-10-02/W1]")
        self.assertTrue(self.texts[0].startswith(marker), self.texts[0])
        first = (wab.wave_path(self.cfg, "W1") / "first-prompt.md").read_text(encoding="utf-8")
        self.assertTrue(first.startswith(marker), first)
        self.assertIn("задача", first)

    def test_relaunch_keeps_old_sessions_as_attempt(self):
        wab.save_state(self.cfg, {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-w1", "cwd": "/x", "started": 1.0, "restarts": 1, "phase": "dead",
            "notified": {}, "sessions": ["old1", "old2"]}}})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        st = wab.load_state(self.cfg)
        self.assertEqual(wab.owned_sessions(st) & {"old1", "old2"}, {"old1", "old2"})
        self.assertNotIn("old1", st["waves"]["W1"]["sessions"])

    def test_wave_argv_without_session_id_has_none(self):
        argv = wab.wave_argv(self.cfg, "W1", dict(self.cfg["roles"]), pathlib.Path("/x"), "abc")
        self.assertEqual(argv[argv.index("--session-id") + 1], "abc")


class TestDashStatsBySession(_Tmp):
    def test_stats_only_wave_sessions(self):
        from test_dash import import_dash
        dash = import_dash()
        self.journal("mine", [assistant(0, 0, 1), assistant(0, 0, 1)])
        self.journal("foreign", [assistant(0, 0, 1)] * 5)
        s = dash.transcript_stats(self.cwd, ["mine"])
        self.assertEqual(s["turns"], 2)
        s = dash.transcript_stats(self.cwd, None)   # старая запись без sessions — как раньше, по каталогу
        self.assertEqual(s["turns"], 7)


if __name__ == "__main__":
    unittest.main()
