"""W4, гейт мерджа: сбор фактов GitHub/git через подменённый run и чистое решение wait/fail/pass.

Реальные gh и git не вызываются: run подменяется таблицей ответов по первым аргументам argv.
Каждый тест, который при регрессии мог бы повиснуть (FIFO на месте плана), ограничен по времени."""
import hashlib
import json
import os
import pathlib
import tempfile
import unittest

from helpers import deadline, good, write_json

import gate
import wab

REPO = "owner/name"
SHA = "a" * 40
OTHER = "b" * 40


def pr(number=7, state="OPEN", head=SHA, owner="owner", name="name", merged_at=None, draft=False):
    return {"number": number, "state": state, "headRefOid": head, "isDraft": draft,
            "url": f"https://github.com/{REPO}/pull/{number}", "mergeCommit": None,
            "headRepository": {"name": name}, "headRepositoryOwner": {"login": owner},
            "mergedAt": merged_at}


def run_(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion, "id": 1}


def page(runs, total=None):
    return json.dumps({"total_count": len(runs) if total is None else total, "check_runs": runs})


class FakeRun:
    """run(argv) -> (rc, out, err) по таблице: ключ — кортеж первых аргументов argv."""

    def __init__(self, prs=None, pages=None, head=SHA, porcelain="", overrides=None):
        self.calls = []
        self.table = {
            ("gh", "pr", "list"): (0, json.dumps(prs if prs is not None else [pr()]), ""),
            ("gh", "api"): (0, "\n".join(pages) if pages is not None else page([run_("ci")]), ""),
            ("git", "-C"): None,   # rev-parse / status различаются дальше
            ("gh", "pr", "view"): (0, json.dumps({"headRefOid": head, "state": "OPEN", "isDraft": False}), ""),
        }
        self.head, self.porcelain = head, porcelain
        self.table.update(overrides or {})

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:1] == ["git"]:
            if "rev-parse" in argv:
                return self.table.get("rev-parse", (0, self.head + "\n", ""))
            if "status" in argv:
                return self.table.get("status", (0, self.porcelain, ""))
        for n in (3, 2):
            key = tuple(argv[:n])
            if key in self.table and self.table[key] is not None:
                val = self.table[key]
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"неожиданный вызов {argv}")


class _Cfg(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._td.name)
        self.data = good()
        self.cfg = wab.load_waves(str(write_json(self.dir, self.data)))

    def tearDown(self):
        self._td.cleanup()

    def pin(self, text=b"plan v1\n", file_text=None):
        self.data["plan_sha256"] = hashlib.sha256(text).hexdigest()
        self.cfg = wab.load_waves(str(write_json(self.dir, self.data)))
        (self.dir / "waves.md").write_bytes(text if file_text is None else file_text)

    def gate(self, fake):
        facts = gate.collect_facts(self.cfg, "wab/W4", str(self.dir), run=fake)
        return gate.decide(facts)


class TestDecideChecks(_Cfg):
    def test_all_green_pass(self):
        verdict, reasons = self.gate(FakeRun(pages=[page([run_("ci"), run_("lint")])]))
        self.assertEqual(verdict, "pass", reasons)

    def test_no_check_runs_wait(self):
        verdict, reasons = self.gate(FakeRun(pages=[page([])]))
        self.assertEqual(verdict, "wait")
        self.assertIn("нет check-runs", " ".join(reasons))
        self.assertIn(SHA[:12], " ".join(reasons))

    def test_pending_wait(self):
        for status in ("in_progress", "queued"):
            verdict, reasons = self.gate(FakeRun(pages=[page([run_("ci"), run_("slow", status, None)])]))
            self.assertEqual(verdict, "wait", status)
            self.assertIn("slow", " ".join(reasons))

    def test_failure_fail(self):
        verdict, reasons = self.gate(FakeRun(pages=[page([run_("ci"), run_("tests", conclusion="failure")])]))
        self.assertEqual(verdict, "fail")
        self.assertIn("tests=failure", " ".join(reasons))

    def test_strict_success_only(self):
        for concl in ("skipped", "neutral", "cancelled", "timed_out", "action_required"):
            verdict, reasons = self.gate(FakeRun(pages=[page([run_("ci"), run_("x", conclusion=concl)])]))
            self.assertEqual(verdict, "fail", concl)
            self.assertIn(f"x={concl}", " ".join(reasons))

    def test_fail_beats_pending(self):
        verdict, _ = self.gate(FakeRun(pages=[page([run_("a", "in_progress", None),
                                                    run_("b", conclusion="failure")])]))
        self.assertEqual(verdict, "fail")

    def test_pending_names_capped_at_five(self):
        runs = [run_(f"job{i}", "queued", None) for i in range(8)]
        verdict, reasons = self.gate(FakeRun(pages=[page(runs)]))
        self.assertEqual(verdict, "wait")
        text = " ".join(reasons)
        self.assertIn("job4", text)
        self.assertNotIn("job5", text)

    def test_head_changed_wait(self):
        verdict, reasons = self.gate(FakeRun(head=OTHER))
        self.assertEqual(verdict, "wait")
        self.assertIn("headRefOid", " ".join(reasons))

    def test_dirty_tree_fail(self):
        verdict, reasons = self.gate(FakeRun(porcelain="?? new.txt\0"))
        self.assertEqual(verdict, "fail")
        self.assertIn("незакоммиченные изменения", " ".join(reasons))

    def test_dirty_tree_beats_head_wait(self):
        verdict, _ = self.gate(FakeRun(head=OTHER, porcelain=" M a.py\0"))
        self.assertEqual(verdict, "fail")

    def test_checks_requested_for_pr_head(self):
        fake = FakeRun()
        self.gate(fake)
        api = [c for c in fake.calls if c[:2] == ["gh", "api"]]
        self.assertEqual(len(api), 1)
        self.assertIn("--paginate", api[0])
        self.assertIn(f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100", api[0])
        self.assertNotIn("--jq", api[0])


class TestPagination(_Cfg):
    def test_two_pages_concatenated(self):
        p1 = page([run_(f"a{i}") for i in range(3)], total=5)
        p2 = page([run_("b0"), run_("b1", "in_progress", None)], total=5)
        fake = FakeRun(pages=[p1 + p2])   # gh --paginate склеивает объекты без разделителя
        runs = gate.check_runs(REPO, SHA, fake)
        self.assertEqual([r["name"] for r in runs], ["a0", "a1", "a2", "b0", "b1"])
        verdict, reasons = self.gate(FakeRun(pages=[p1, p2]))
        self.assertEqual(verdict, "wait")
        self.assertIn("b1", " ".join(reasons))

    def test_total_count_mismatch(self):
        fake = FakeRun(pages=[page([run_("a")], total=3)])
        with self.assertRaises(gate.GateError):
            gate.check_runs(REPO, SHA, fake)
        verdict, reasons = self.gate(FakeRun(pages=[page([run_("a")], total=3)]))
        self.assertEqual(verdict, "wait")
        self.assertIn("total_count", " ".join(reasons))

    def test_garbage_output_wait(self):
        verdict, _ = self.gate(FakeRun(pages=['{"total_count": 1, "check_runs": [] } мусор']))
        self.assertEqual(verdict, "wait")

    def test_gh_api_error_wait(self):
        verdict, reasons = self.gate(FakeRun(overrides={("gh", "api"): (1, "", "HTTP 502 " + "x" * 2000)}))
        self.assertEqual(verdict, "wait")
        # gate не усекает: обрывок токена на границе среза ушёл бы мимо redact; режет вызывающий после redact
        self.assertTrue(any(len(r) > 2000 for r in reasons), reasons)
        self.assertTrue(all("\n" not in r for r in reasons))


class TestFindPr(_Cfg):
    def test_fork_with_same_branch_dropped(self):
        fork = pr(number=9, owner="stranger", head=OTHER)
        found = gate.find_pr(REPO, "wab/W4", "main", FakeRun(prs=[fork, pr()]))
        self.assertEqual(found["number"], 7)
        self.assertIsNone(gate.find_pr(REPO, "wab/W4", "main", FakeRun(prs=[fork])))

    def test_owner_case_insensitive(self):
        found = gate.find_pr("Owner/Name", "wab/W4", "main", FakeRun(prs=[pr(owner="OWNER", name="NAME")]))
        self.assertEqual(found["number"], 7)

    def test_list_args(self):
        fake = FakeRun()
        gate.find_pr(REPO, "wab/W4", "main", fake)
        argv = fake.calls[0]
        for a in ("--repo", REPO, "--head", "wab/W4", "--base", "main", "--state", "all"):
            self.assertIn(a, argv)

    def test_two_open_fail(self):
        verdict, reasons = self.gate(FakeRun(prs=[pr(7), pr(8)]))
        self.assertEqual(verdict, "fail")
        self.assertIn("два открытых PR", " ".join(reasons))

    def test_fork_open_not_counted_as_duplicate(self):
        verdict, _ = self.gate(FakeRun(prs=[pr(7), pr(8, owner="stranger")]))
        self.assertEqual(verdict, "pass")

    def test_open_preferred_over_merged(self):
        found = gate.find_pr(REPO, "b", "main", FakeRun(prs=[pr(3, "MERGED", merged_at="2026-10-01T00:00:00Z"),
                                                             pr(7)]))
        self.assertEqual(found["number"], 7)

    def test_merged_latest(self):
        prs = [pr(3, "MERGED", merged_at="2026-10-01T00:00:00Z"),
               pr(5, "MERGED", merged_at="2026-10-02T00:00:00Z"), pr(4, "CLOSED")]
        fake = FakeRun(prs=prs)
        verdict, _ = self.gate(fake)
        self.assertEqual(verdict, "merged")
        self.assertEqual(gate.find_pr(REPO, "b", "main", FakeRun(prs=prs))["number"], 5)
        # для MERGED check-runs собираются на финальном headRefOid смерженного PR
        api = [c for c in fake.calls if c[:2] == ["gh", "api"]]
        self.assertEqual(len(api), 1)
        self.assertIn(f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100", api[0])

    def test_merged_ignores_local_git_errors(self):
        # для MERGED локальные факты не нужны: сломанный git рабочей копии не превращает merged в wait
        fake = FakeRun(prs=[pr(5, "MERGED", merged_at="2026-10-02T00:00:00Z")],
                       overrides={"rev-parse": (128, "", "fatal: not a git repository"),
                                  "status": (128, "", "fatal")})
        verdict, reasons = self.gate(fake)
        self.assertEqual(verdict, "merged", reasons)
        self.assertFalse([c for c in fake.calls if c[:1] == ["git"]], "git для MERGED не вызывается")

    def merged(self, pages=None, overrides=None, head=SHA):
        return self.gate(FakeRun(prs=[pr(5, "MERGED", head=head, merged_at="2026-10-02T00:00:00Z")],
                                 pages=pages, overrides=overrides))

    def test_merged_red_checks_fail(self):
        verdict, reasons = self.merged(pages=[page([run_("ci"), run_("tests", conclusion="failure")])], head=OTHER)
        self.assertEqual(verdict, "fail")
        self.assertIn(f"PR #5 смержен, но check-runs на {OTHER[:12]} не зелёные: tests=failure", reasons)

    def test_merged_skipped_is_not_green(self):
        verdict, _ = self.merged(pages=[page([run_("x", conclusion="skipped")])])
        self.assertEqual(verdict, "fail")

    def test_merged_pending_or_none_wait(self):
        self.assertEqual(self.merged(pages=[page([run_("ci", "in_progress", None)])])[0], "wait")
        verdict, reasons = self.merged(pages=[page([])])
        self.assertEqual(verdict, "wait")
        self.assertIn("нет check-runs", " ".join(reasons))

    def test_merged_checks_error_wait(self):
        verdict, _ = self.merged(overrides={("gh", "api"): (1, "", "HTTP 502")})
        self.assertEqual(verdict, "wait")
        verdict, _ = self.merged(pages=[page([run_("a")], total=3)])
        self.assertEqual(verdict, "wait")

    def test_merged_pure_decide_requires_checks(self):
        merged_pr = pr(5, "MERGED", merged_at="2026-10-02T00:00:00Z")
        base = {"pr": merged_pr, "local_head": None, "tree_clean": None, "plan": None, "errors": [],
                "duplicate": None}
        self.assertEqual(gate.decide({**base, "checks": [run_("ci")]})[0], "merged")
        self.assertEqual(gate.decide({**base, "checks": None})[0], "wait")

    def test_merged_plan_still_fails(self):
        self.pin(b"plan v1\n", file_text=b"x")
        fake = FakeRun(prs=[pr(5, "MERGED", merged_at="2026-10-02T00:00:00Z")],
                       overrides={"rev-parse": (128, "", "fatal")})
        self.assertEqual(self.gate(fake)[0], "fail")

    def test_closed_fail(self):
        verdict, reasons = self.gate(FakeRun(prs=[pr(4, "CLOSED")]))
        self.assertEqual(verdict, "fail")
        self.assertIn("закрыт", " ".join(reasons))

    def test_none_wait(self):
        verdict, reasons = self.gate(FakeRun(prs=[]))
        self.assertEqual(verdict, "wait")
        self.assertIn("PR ветки не найден", " ".join(reasons))

    def test_invalid_json_wait(self):
        with self.assertRaises(gate.GateError):
            gate.find_pr(REPO, "b", "main", FakeRun(overrides={("gh", "pr", "list"): (0, "not json", "")}))
        verdict, _ = self.gate(FakeRun(overrides={("gh", "pr", "list"): (0, "not json", "")}))
        self.assertEqual(verdict, "wait")

    def test_gh_failure_and_timeout_wait(self):
        import subprocess
        for val in ((1, "", "gh: auth required"), subprocess.TimeoutExpired(["gh"], 60), OSError("нет gh")):
            verdict, reasons = self.gate(FakeRun(overrides={("gh", "pr", "list"): val}))
            self.assertEqual(verdict, "wait", val)
            self.assertTrue(reasons)


class TestPlan(_Cfg):
    def test_plan_changed_fail(self):
        self.pin(b"plan v1\n", file_text=b"plan v2\n")
        verdict, reasons = self.gate(FakeRun())
        self.assertEqual(verdict, "fail")
        self.assertTrue(reasons[0].startswith("plan changed since approval: "), reasons)

    def test_plan_checked_every_collect(self):
        self.pin(b"plan v1\n")
        self.assertEqual(self.gate(FakeRun())[0], "pass")
        (self.dir / "waves.md").write_bytes(b"tampered\n")
        self.assertEqual(self.gate(FakeRun())[0], "fail")

    def test_plan_fail_beats_merged(self):
        self.pin(b"plan v1\n", file_text=b"x")
        verdict, _ = self.gate(FakeRun(prs=[pr(5, "MERGED", merged_at="2026-10-02T00:00:00Z")]))
        self.assertEqual(verdict, "fail")

    def test_plan_problem_values(self):
        self.assertIsNone(gate.plan_problem(self.cfg))   # не задан — без проверки
        self.pin(b"plan v1\n")
        self.assertIsNone(gate.plan_problem(self.cfg))
        (self.dir / "waves.md").unlink()
        self.assertIn("нет", gate.plan_problem(self.cfg))
        os.symlink(self.dir / "elsewhere", self.dir / "waves.md")
        (self.dir / "elsewhere").write_bytes(b"plan v1\n")
        self.assertIn("симлинк", gate.plan_problem(self.cfg))
        (self.dir / "waves.md").unlink()
        (self.dir / "waves.md").write_bytes(b"x" * (gate.PLAN_MAX_BYTES + 1))
        self.assertIn("больше", gate.plan_problem(self.cfg))

    def test_fifo_not_hung(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("нет mkfifo")
        self.pin(b"plan v1\n")
        (self.dir / "waves.md").unlink()
        os.mkfifo(self.dir / "waves.md")
        with deadline(10):
            self.assertIn("не обычный файл", gate.plan_problem(self.cfg))
            verdict, reasons = self.gate(FakeRun())
        self.assertEqual(verdict, "fail")
        self.assertTrue(reasons[0].startswith("plan changed since approval: "))


class TestLocalAndHead(unittest.TestCase):
    def test_local_facts(self):
        self.assertEqual(gate.local_facts("/w", FakeRun()), (SHA, True))
        self.assertEqual(gate.local_facts("/w", FakeRun(porcelain=" M x\0")), (SHA, False))

    def test_local_facts_git_error(self):
        with self.assertRaises(gate.GateError):
            gate.local_facts("/w", FakeRun(overrides={"rev-parse": (128, "", "fatal: not a git repository")}))

    def test_head_still(self):
        fresh = gate.head_still(REPO, 7, SHA, FakeRun(head=OTHER))
        self.assertEqual(fresh["headRefOid"], OTHER)
        self.assertEqual(fresh["state"], "OPEN")
        with self.assertRaises(gate.GateError):
            gate.head_still(REPO, 7, SHA, FakeRun(overrides={("gh", "pr", "view"): (0, "[]", "")}))

    def test_decide_is_pure_on_dict(self):
        facts = {"pr": pr(), "local_head": SHA, "tree_clean": True, "checks": [run_("ci")],
                 "plan": None, "errors": [], "duplicate": None}
        self.assertEqual(gate.decide(facts), ("pass", []))


class TestCheckPlanUnchanged(_Cfg):
    """wab.check_plan переиспользует gate.plan_problem и не меняет поведение."""

    def test_check_plan_uses_gate(self):
        self.pin(b"plan v1\n", file_text=b"v2")
        with self.assertRaises(SystemExit) as cm:
            wab.check_plan(self.cfg)
        self.assertTrue(str(cm.exception).startswith("BLOCKED: plan changed since approval: sha256 waves.md"))
        self.assertIs(wab.PLAN_MAX_BYTES, gate.PLAN_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
