"""W4, поток после DONE: гейт мерджа, ожидание или автомердж, проверка merge-коммита, следующая волна.

tmux, gh, git и claude не вызываются: gh/git идут через подменённый wab.gate_run (таблица ответов
и счётчик вызовов), окно волны — через подменённые send_text/send_keys/tmux_alive, launch — мок.
"""
import contextlib
import fcntl
import hashlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)
from helpers import good, write_json

import wab

SHA = "a" * 40
OTHER = "b" * 40
MOID = "c" * 40


class Gh:
    """Поддельные gh/git: состояние PR меняется тестом, каждый вызов записывается."""

    def __init__(self, test):
        self.t = test
        self.calls = []
        self.state, self.number, self.head, self.local = "OPEN", 7, SHA, SHA
        self.draft = False
        self.runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.oid, self.ancestor = MOID, 0
        self.merge_rc, self.merge_err = 0, ""
        self.on_merge = None
        self.dirty = ""

    def pr(self):
        return {"number": self.number, "state": self.state, "headRefOid": self.head, "isDraft": self.draft,
                "url": f"https://github.com/owner/name/pull/{self.number}",
                "mergeCommit": {"oid": self.oid} if self.state == "MERGED" else None,
                "headRepository": {"name": "name"}, "headRepositoryOwner": {"login": "owner"},
                "mergedAt": "2026-10-02T10:00:00Z" if self.state == "MERGED" else None}

    def merges(self):
        return [c for c in self.calls if c[:3] == ["gh", "pr", "merge"]]

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps([self.pr()]), ""
        if argv[:2] == ["gh", "api"]:
            return 0, json.dumps({"total_count": len(self.runs), "check_runs": self.runs}), ""
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"headRefOid": self.head, "state": self.state, "isDraft": self.draft}), ""
        if argv[:3] == ["gh", "pr", "ready"]:
            self.draft = False
            return 0, "", ""
        if argv[:3] == ["gh", "pr", "merge"]:
            if self.on_merge:
                self.on_merge(argv)
            return self.merge_rc, "", self.merge_err
        if argv[:1] == ["git"]:
            if "rev-parse" in argv:
                return 0, self.local + "\n", ""
            if "status" in argv:
                return 0, self.dirty, ""
            if "fetch" in argv:
                return 0, "", ""
            if "merge-base" in argv:
                return self.ancestor, "", ""
        raise AssertionError(f"неожиданный вызов {argv}")


class _Flow(unittest.TestCase):
    automerge = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.data = good()
        self.data["automerge"] = self.automerge
        self.load()
        self.gh = Gh(self)
        self.texts, self.keys = [], []
        self.alive = True
        for name, kw in (("gate_run", {"side_effect": lambda argv: self.gh(argv)}),
                         ("tmux_alive", {"side_effect": lambda n: self.alive}),
                         ("send_text", {"side_effect": lambda n, t: self.texts.append((n, t))}),
                         ("send_keys", {"side_effect": lambda *a, **k: self.keys.append(a)}),
                         ("pane_text", {"return_value": "? for shortcuts"}),
                         ("context_tokens", {"return_value": 0}),
                         ("require_tmux", {"return_value": None}),
                         ("launch", {"return_value": True})):
            pt = mock.patch.object(wab, name, **kw)
            setattr(self, "m_" + name, pt.start())
            self.addCleanup(pt.stop)
        self.st = self.state("W1")

    def load(self):
        self.cfg_path = write_json(self.dir, self.data)
        self.cfg = wab.load_waves(str(self.cfg_path))

    def pin_plan(self, text=b"plan v1\n"):
        self.data["plan_sha256"] = hashlib.sha256(text).hexdigest()
        self.load()
        (self.dir / "waves.md").write_bytes(text)

    def state(self, wave, **extra):
        return {"current": wave, "waves": {wave: {
            "tmux": f"wab-demo-{wave.lower()}", "cwd": str(self.dir / "wt"), "phase": "running",
            "restarts": 0, "notified": {}, "started": 1000.0, "sessions": ["s1"], **extra}}}

    def status(self, wave="W1"):
        return wab.read(wab.wave_path(self.cfg, wave) / "status")

    def tick(self, status=None, wave="W1", next_prompt=True):
        wdir = wab.wave_dir(self.cfg, wave)
        if status is not None:
            (wdir / "status").write_text(status + "\n", encoding="utf-8")
        if next_prompt and not (wdir / "next-prompt.md").exists():
            (wdir / "next-prompt.md").write_text("промпт следующей волны\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, self.st, str(self.cfg_path))

    def log(self):
        p = self.cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def w(self, wave="W1"):
        return self.st["waves"][wave]


class TestPlanChanged(_Flow):
    automerge = True

    def test_plan_changed_after_launch_blocks_no_merge_no_next(self):
        self.pin_plan()
        self.assertTrue(self.tick("RUNNING"))
        (self.dir / "waves.md").write_bytes(b"plan v2\n")
        self.tick("DONE")
        self.assertTrue(self.status().startswith("BLOCKED: plan changed since approval:"), self.status())
        self.assertNotIn("merge gate", self.status())
        self.assertEqual(self.gh.merges(), [])
        self.assertNotIn("pending_launch", self.st)
        self.assertEqual(self.st["current"], "W1")
        self.m_launch.assert_not_called()
        # в окно волны ушла причина
        self.assertTrue(any("Гейт мерджа не пройден" in t for _, t in self.texts))

    def test_plan_changed_right_before_merge(self):
        self.pin_plan()

        def change_plan(argv):   # план меняется между сбором фактов и мерджем: pr view — последний шаг
            (self.dir / "waves.md").write_bytes(b"plan v2\n")
            return 0, json.dumps({"headRefOid": SHA, "state": "OPEN", "isDraft": False}), ""
        orig = self.gh.__call__

        def gh(argv):
            if argv[:3] == ["gh", "pr", "view"]:
                self.gh.calls.append(list(argv))
                return change_plan(argv)
            return orig(argv)
        self.m_gate_run.side_effect = gh
        self.tick("DONE")
        self.assertEqual(self.gh.merges(), [])
        self.assertTrue(self.status().startswith("BLOCKED: plan changed since approval:"))


class TestNoAutomerge(_Flow):
    def test_pass_waits_for_human_merge(self):
        self.assertTrue(self.tick("DONE"))
        w = self.w()
        self.assertEqual(w["phase"], "awaiting_merge")
        self.assertEqual(w["pr"], {"number": 7, "url": "https://github.com/owner/name/pull/7", "sha": SHA})
        self.assertEqual(w["gate"]["verdict"], "pass")
        self.assertIn("W1: ждёт мерджа PR #7", self.log())
        self.assertTrue(self.tick())
        self.assertEqual(self.log().count("ждёт мерджа PR #7"), 1)
        self.assertEqual(self.gh.merges(), [])
        self.assertEqual(self.keys, [])            # окно не закрыто до MERGED
        self.m_launch.assert_not_called()

    def test_human_merge_then_next_wave_pending(self):
        self.tick("DONE")
        self.gh.state = "MERGED"
        self.assertTrue(self.tick())
        w = self.w()
        self.assertEqual(w["phase"], "merged")
        self.assertEqual(w["merged"]["oid"], MOID)
        self.assertIsNone(self.st["current"])
        self.assertEqual(self.st["pending_launch"]["wave"], "W2")
        self.assertEqual(self.st["pending_launch"]["after"], "W1")
        self.assertTrue(self.st["pending_launch"]["prompt"].endswith("next-prompt.md"))
        self.assertIn(("wab-demo-w1", "-l", "/exit"), self.keys)
        self.assertEqual(wab.load_state(self.cfg)["pending_launch"]["wave"], "W2")
        self.assertEqual(self.gh.merges(), [])
        fetch = [c for c in self.gh.calls if "fetch" in c]
        self.assertTrue(fetch and fetch[0][:3] == ["git", "-C", "/tmp/some/checkout"])
        mb = [c for c in self.gh.calls if "merge-base" in c][0]
        self.assertEqual(mb[-3:], ["--is-ancestor", MOID, "origin/main"])

    def test_repeat_fail_in_awaiting_merge_blocks(self):
        self.pin_plan()
        self.tick("DONE")
        self.assertEqual(self.w()["phase"], "awaiting_merge")
        (self.dir / "waves.md").write_bytes(b"other\n")
        self.tick()
        self.assertTrue(self.status().startswith("BLOCKED: plan changed since approval:"))
        self.assertEqual(self.w()["phase"], "running")

    def test_wait_keeps_phase_and_dedups_event(self):
        self.gh.runs = [{"name": "ci", "status": "in_progress", "conclusion": None}]
        self.tick("DONE")
        self.assertEqual(self.w()["phase"], "gate")
        self.assertEqual(self.w()["gate"]["verdict"], "wait")
        self.assertEqual(self.w()["gate"]["pr"], 7)
        self.tick()
        self.assertEqual(self.log().count("check-runs не завершены"), 1)
        self.assertEqual(self.status(), "DONE")

    def test_dead_window_done_runs_gate(self):
        self.alive = False
        self.tick("DONE")
        self.assertEqual(self.w()["phase"], "awaiting_merge")
        self.assertNotIn("закрыта", self.log())


class TestAutomerge(_Flow):
    automerge = True

    def test_exactly_one_merge_saved_before_call(self):
        seen = {}

        def on_merge(argv):
            seen["disk"] = wab.load_state(self.cfg)["waves"]["W1"].get("merge")
        self.gh.on_merge = on_merge
        self.tick("DONE")
        merges = self.gh.merges()
        self.assertEqual(len(merges), 1)
        self.assertIn("--squash", merges[0])
        i = merges[0].index("--match-head-commit")
        self.assertEqual(merges[0][i + 1], SHA)
        self.assertEqual(merges[0][3], "7")
        self.assertEqual(seen["disk"]["sha"], SHA)
        self.assertIsNone(seen["disk"]["rc"])
        self.assertEqual(self.w()["merge"]["rc"], 0)
        # второй такт: PR ещё OPEN — повторного мерджа нет
        self.tick()
        self.tick()
        self.assertEqual(len(self.gh.merges()), 1)
        self.gh.state = "MERGED"
        self.tick()
        self.assertEqual(self.w()["phase"], "merged")
        self.assertEqual(len(self.gh.merges()), 1)

    def test_head_changed_before_merge_waits(self):
        orig = Gh.__call__

        def gh(argv):
            if argv[:3] == ["gh", "pr", "view"]:
                self.gh.calls.append(list(argv))
                return 0, json.dumps({"headRefOid": OTHER, "state": "OPEN", "isDraft": False}), ""
            return orig(self.gh, argv)
        self.m_gate_run.side_effect = gh
        self.tick("DONE")
        self.assertEqual(self.gh.merges(), [])
        self.assertEqual(self.w()["gate"]["verdict"], "wait")
        self.assertNotIn("merge", self.w())

    def test_draft_is_readied_first(self):
        self.gh.draft = True
        self.tick("DONE")
        kinds = [c[:3] for c in self.gh.calls if c[:2] == ["gh", "pr"] and c[2] in ("ready", "merge")]
        self.assertEqual(kinds, [["gh", "pr", "ready"], ["gh", "pr", "merge"]])

    def test_merge_refused_blocks_and_never_repeats(self):
        self.gh.merge_rc, self.gh.merge_err = 1, "Pull request is not mergeable"
        self.tick("DONE")
        self.assertTrue(self.status().startswith("BLOCKED: merge gate: gh pr merge отказал:"), self.status())
        self.assertIn("not mergeable", self.status())
        self.tick("DONE")   # агент снова пишет DONE на тот же sha
        self.assertEqual(len(self.gh.merges()), 1)
        self.assertTrue(self.status().startswith("BLOCKED: merge gate: gh pr merge отказал"))

    def test_merge_record_survives_status_change(self):
        self.gh.merge_rc = 0
        self.tick("DONE")
        self.tick("RUNNING")
        self.assertEqual(self.w()["phase"], "running")
        self.assertNotIn("gate", self.w())
        self.assertEqual(self.w()["merge"]["sha"], SHA)
        self.tick("DONE")
        self.assertEqual(len(self.gh.merges()), 1)


class TestMergeVerify(_Flow):
    def test_not_ancestor_blocks_and_retries(self):
        self.gh.state, self.gh.ancestor = "MERGED", 1
        self.assertTrue(self.tick("DONE"))
        self.assertEqual(self.status(), f"BLOCKED: merge gate: merge-коммит {MOID[:12]} не в origin/main")
        self.assertEqual(self.w()["phase"], "merge_unverified")
        self.assertEqual(self.st["current"], "W1")
        self.assertNotIn("pending_launch", self.st)
        self.m_launch.assert_not_called()
        self.assertEqual(self.keys, [])
        self.tick()
        self.assertEqual(self.log().count("не в origin/main"), 1)
        self.gh.ancestor = 0     # fetch догнал
        self.assertTrue(self.tick())
        self.assertEqual(self.w()["phase"], "merged")
        self.assertEqual(self.st["pending_launch"]["wave"], "W2")

    def test_no_next_prompt_stops_with_partial_result(self):
        self.gh.state = "MERGED"
        self.assertFalse(self.tick("DONE", next_prompt=False))
        self.assertIn("нет next-prompt.md", self.log())
        self.assertNotIn("pending_launch", self.st)
        self.assertTrue((self.cfg["run_dir"] / "chain-result.md").exists())


class TestChainResult(_Flow):
    def test_last_wave_writes_chain_result(self):
        self.st = self.state("W2")
        self.st["waves"]["W1"] = {"tmux": "wab-demo-w1", "phase": "merged", "restarts": 2, "notified": {},
                                  "started": 1000.0, "finished": 4600.0, "attempts": [{"sessions": []}],
                                  "questions": ["BLOCKED: как быть с ```кодом```?\n# не заголовок"],
                                  "pr": {"number": 5, "url": "https://github.com/owner/name/pull/5", "sha": OTHER},
                                  "merged": {"pr": 5, "oid": "d" * 40, "at": 4600.0}}
        self.gh.state, self.gh.number = "MERGED", 8
        self.assertFalse(self.tick("DONE", wave="W2"))
        self.assertIn("цепочка завершена", self.log())
        text = (self.cfg["run_dir"] / "chain-result.md").read_text(encoding="utf-8")
        self.assertIn("#5", text)
        self.assertIn("#8", text)
        self.assertIn("pull/8", text)
        self.assertIn("d" * 12, text)
        self.assertIn("Каркас", text)
        # недоверенный вопрос не ломает разметку: строка «# не заголовок» внутри блока кода
        lines = text.splitlines()
        # ограда длиннее ``` внутри вопроса, и «# не заголовок» лежит между открывающей и закрывающей
        opening = next(i for i, l in enumerate(lines) if l.startswith("````"))
        fence = lines[opening].rstrip("text")
        closing = next(i for i in range(opening + 1, len(lines)) if lines[i] == fence)
        self.assertIn("# не заголовок", lines[opening + 1:closing])
        self.assertFalse(list((self.cfg["run_dir"]).glob("chain-result.md.*")))


class TestGateFail(_Flow):
    def test_fail_text_to_window_and_running_after(self):
        self.gh.runs = [{"name": "tests", "status": "completed", "conclusion": "failure"}]
        self.assertTrue(self.tick("DONE"))
        self.assertEqual(self.status(), "BLOCKED: merge gate: check-runs не успешны: tests=failure")
        self.assertEqual(len(self.texts), 1)
        name, text = self.texts[0]
        self.assertEqual(name, "wab-demo-w1")
        self.assertTrue(text.startswith("[wab] Гейт мерджа не пройден: check-runs не успешны: tests=failure."))
        self.assertIn("снова запиши DONE", text)
        self.assertEqual(self.w()["phase"], "running")
        self.assertEqual(self.gh.merges(), [])
        self.m_launch.assert_not_called()
        self.tick()   # ветка BLOCKED: событие не дублируется
        self.assertEqual(self.log().count("tests=failure"), 1)
        self.tick("RUNNING")
        self.assertEqual(self.w()["phase"], "running")

    def test_fail_dead_window_no_send(self):
        self.alive = False
        self.gh.dirty = " M a.py\0"
        self.tick("DONE")
        self.assertEqual(self.texts, [])
        self.assertTrue(self.status().startswith("BLOCKED: merge gate:"))

    def test_status_change_in_gate_returns_running(self):
        self.gh.runs = []
        self.tick("DONE")
        self.assertEqual(self.w()["phase"], "gate")
        self.tick("RUNNING")
        self.assertEqual(self.w()["phase"], "running")
        self.assertNotIn("gate", self.w())

    def test_questions_accumulate_without_duplicates(self):
        self.tick("BLOCKED: вопрос 1")
        self.tick("BLOCKED: вопрос 1")
        self.tick("BLOCKED: вопрос 2")
        self.assertEqual(self.w()["questions"], ["BLOCKED: вопрос 1", "BLOCKED: вопрос 2"])
        for i in range(30):
            self.tick(f"BLOCKED: q{i}")
        self.assertEqual(len(self.w()["questions"]), 20)


class TestPendingLaunch(_Flow):
    def lock_free(self):
        fd = os.open(self.cfg["run_dir"] / "state.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def run_watch(self):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(wab.time, "sleep"):
            wab._watch_loop(self.cfg, str(self.cfg_path))

    def test_launch_outside_run_lock_after_merge(self):
        seen = []
        self.m_launch.side_effect = lambda cfg, wave, prompt: (seen.append((wave, prompt, self.lock_free(),
                                                                          wab.load_state(cfg).get("pending_launch")))
                                                              or False)
        self.gh.state = "MERGED"
        wdir = wab.wave_dir(self.cfg, "W1")
        (wdir / "status").write_text("DONE\n", encoding="utf-8")
        (wdir / "next-prompt.md").write_text("дальше\n", encoding="utf-8")
        wab.save_state(self.cfg, self.st)
        self.run_watch()
        self.assertEqual(len(seen), 1)
        wave, prompt, free, pending = seen[0]
        self.assertEqual((wave, prompt), ("W2", str(wdir / "next-prompt.md")))
        self.assertTrue(free, "launch вызван под run_lock")
        self.assertIsNone(pending, "pending_launch снят до вызова launch")
        self.assertIn("запускаю W2", self.log())

    def test_restart_with_pending_launches(self):
        st = {"current": None, "waves": {}, "pending_launch": {"wave": "W2", "prompt": "/x/next.md", "after": "W1"}}
        wab.save_state(self.cfg, st)
        self.m_launch.return_value = False
        self.run_watch()
        self.m_launch.assert_called_once_with(mock.ANY, "W2", "/x/next.md")
        self.assertNotIn("pending_launch", wab.load_state(self.cfg))

    def test_launch_systemexit_stops_watch(self):
        st = {"current": None, "waves": {}, "pending_launch": {"wave": "W2", "prompt": "/x/next.md", "after": "W1"}}
        wab.save_state(self.cfg, st)
        self.m_launch.side_effect = SystemExit("BLOCKED: plan changed since approval: sha256 …")
        self.run_watch()
        self.assertIn("BLOCKED: plan changed since approval", self.log())
        self.assertIn("watch остановлен", self.log())

    def test_tick_without_current_and_pending_false(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(wab.tick(self.cfg, {"current": None, "waves": {}}))
            self.assertTrue(wab.tick(self.cfg, {"current": None, "waves": {},
                                                "pending_launch": {"wave": "W2", "prompt": "p"}}))


if __name__ == "__main__":
    unittest.main()
