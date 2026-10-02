"""W2: роли → claude --agents, проверка моделей с fallback, argv волны, дашборд, PROTOCOL.md и SKILL.md.

tmux, git и реальный claude не вызываются: побочные эффекты launch и probe_model подменены.
"""
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import helpers
from helpers import ROOT, good, write_json

import wab
from test_dash import dash


def cfg_for(directory, roles=None, fallback=None):
    d = good()
    if roles is not None:
        d["roles"] = roles
    if fallback is not None:
        d["fallback_model"] = fallback
    return wab.load_waves(str(write_json(directory, d)))


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.cfg = cfg_for(self.dir)
        ev = mock.patch.object(wab, "event", side_effect=lambda cfg, text, **k: self.events.append(text))
        self.events = []
        ev.start()
        self.addCleanup(ev.stop)


class TestAgentsJson(Tmp):
    def test_three_agents_with_role_models(self):
        roles = {"architect": "fable", "coder": "opus", "tester": "sonnet", "reviewer": "fable", "reader": "haiku"}
        agents = json.loads(wab.agents_json(roles))
        self.assertEqual(set(agents), {"wave-tester", "wave-reviewer", "wave-reader"})
        self.assertEqual(agents["wave-tester"]["model"], "sonnet")
        self.assertEqual(agents["wave-reviewer"]["model"], "fable")
        self.assertEqual(agents["wave-reader"]["model"], "haiku")
        self.assertEqual(agents["wave-tester"]["tools"], ["Read", "Grep", "Glob", "Bash"])
        self.assertEqual(agents["wave-reviewer"]["tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(agents["wave-reader"]["tools"], ["Read", "Grep", "Glob"])
        for a in agents.values():
            self.assertTrue(a["description"].strip())
            self.assertTrue(a["prompt"].strip())

    def test_models_follow_effective_roles(self):
        roles = dict(self.cfg["roles"], tester="opus")
        self.assertEqual(json.loads(wab.agents_json(roles))["wave-tester"]["model"], "opus")


class TestResolveRoles(Tmp):
    def test_unavailable_model_replaced_event_once(self):
        probe = mock.Mock(side_effect=lambda m, *a, **k: (m != "fable", "rc=1" if m == "fable" else "rc=0"))
        st = {"waves": {}}
        roles = wab.resolve_roles(self.cfg, st, probe=probe)
        self.assertEqual(roles["architect"], "opus")
        self.assertEqual(roles["reviewer"], "opus")
        self.assertEqual(roles["tester"], "sonnet")
        self.assertEqual(st["roles_effective"], roles)
        self.assertEqual(st["role_fallbacks"], {"architect": {"from": "fable", "to": "opus"},
                                                "reviewer": {"from": "fable", "to": "opus"}})
        self.assertEqual(len(self.events), 1)
        self.assertIn("fable", self.events[0])
        self.assertIn("opus", self.events[0])
        n = probe.call_count
        wab.resolve_roles(self.cfg, st, probe=probe)
        self.assertEqual(probe.call_count, n)       # кэш: claude -p не вызывается
        self.assertEqual(len(self.events), 1)       # событие записано один раз

    def test_available_models_no_event(self):
        st = {"waves": {}}
        roles = wab.resolve_roles(self.cfg, st, probe=lambda m, *a, **k: (True, "rc=0"))
        self.assertEqual(roles, self.cfg["roles"])
        self.assertEqual(self.events, [])
        self.assertEqual(st["role_fallbacks"], {})
        self.assertTrue(all(v["ok"] for v in st["models"].values()))

    def test_fallback_unavailable_exits_without_cache(self):
        st = {"waves": {}}
        with self.assertRaises(SystemExit) as cm:
            wab.resolve_roles(self.cfg, st, probe=lambda m, *a, **k: (False, "rc=1"))
        self.assertIn("opus", str(cm.exception))
        self.assertNotIn("models", st)
        self.assertNotIn("roles_effective", st)

    def test_probe_model_details(self):
        with mock.patch.object(wab.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(wab.probe_model("x"), (False, "claude не найден"))
        with mock.patch.object(wab.subprocess, "run",
                               side_effect=wab.subprocess.TimeoutExpired("claude", 1)):
            self.assertEqual(wab.probe_model("x"), (False, "timeout"))
        r = mock.Mock(returncode=0, stdout="секрет", stderr="секрет")
        with mock.patch.object(wab.subprocess, "run", return_value=r) as run:
            self.assertEqual(wab.probe_model("x"), (True, "rc=0"))
        argv = run.call_args[0][0]
        self.assertEqual(argv[:5], ["claude", "-p", "--model", "x", "--no-session-persistence"])


class LaunchEnv(Tmp):
    """launch с настоящими state и PROTOCOL.md, но без tmux/git/claude."""

    def setUp(self):
        super().setUp()
        self.sh_calls = []
        self.probe = mock.Mock(side_effect=lambda m, *a, **k: (m != "fable", "rc=1" if m == "fable" else "rc=0"))
        for name, kw in {"probe_model": {"side_effect": self.probe},
                         "tmux_alive": {"return_value": False},
                         "prepare_worktree": {"return_value": str(self.dir / "wt")},
                         "sh": {"side_effect": lambda *a, **k: self.sh_calls.append(a)},
                         "wait_ready": {"return_value": True},
                         "send_text": {"return_value": None}}.items():
            p = mock.patch.object(wab, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        self.prompt = self.dir / "p.md"
        self.prompt.write_text("задача\n", encoding="utf-8")

    def argv_of(self, call):
        return list(call[call.index("claude"):])


class TestLaunchArgv(LaunchEnv):
    def test_argv_and_system_prompt(self):
        self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        argv = self.argv_of(self.sh_calls[0])
        self.assertEqual(argv[:3], ["claude", "--model", "opus"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        sp = pathlib.Path(argv[argv.index("--append-system-prompt-file") + 1])
        self.assertEqual(sp.parent, wab.wave_path(self.cfg, "W1"))
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "AskUserQuestion")
        agents = json.loads(argv[argv.index("--agents") + 1])
        self.assertEqual(agents["wave-reviewer"]["model"], "opus")   # fable недоступна -> fallback
        text = sp.read_text(encoding="utf-8")
        self.assertIn(wab.PROTOCOL.read_text(encoding="utf-8").strip(), text)
        self.assertIn("## Контекст волны", text)
        for s in ("W1", "Каркас", "Сделать каркас", "CI зелёный", "python3 -m unittest", "owner/name", "main"):
            self.assertIn(s, text)

    def test_second_launch_uses_cache(self):
        self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        n = self.probe.call_count
        self.assertGreater(n, 0)
        st = wab.load_state(self.cfg)
        st["current"] = None
        wab.save_state(self.cfg, st)
        self.assertTrue(wab.launch(self.cfg, "W2", str(self.prompt)))
        self.assertEqual(self.probe.call_count, n)
        st = wab.load_state(self.cfg)
        self.assertEqual(st["role_fallbacks"]["reviewer"], {"from": "fable", "to": "opus"})
        self.assertEqual(st["roles_effective"]["coder"], "opus")

    def test_failed_models_check_leaves_no_reservation(self):
        self.probe.side_effect = lambda m, *a, **k: (False, "rc=1")
        with self.assertRaises(SystemExit):
            wab.launch(self.cfg, "W1", str(self.prompt))
        st = wab.load_state(self.cfg)
        self.assertIsNone(st.get("current"))
        self.assertEqual(self.sh_calls, [])


class TestRefusalDropsCache(LaunchEnv):
    """После отказа в state нет кэша проверок и эффективных ролей: следующий запуск проверяет заново."""

    KEYS = ("models", "roles_effective", "role_fallbacks")

    def _ok_then_all_down(self):
        self.probe.side_effect = lambda m, *a, **k: (True, "rc=0")
        wab.ensure_roles(self.cfg, refresh=True)
        self.assertIn("models", wab.load_state(self.cfg))
        self.probe.side_effect = lambda m, *a, **k: (False, "rc=1")

    def test_ensure_roles_refusal_drops_cache_and_launch_rechecks(self):
        self._ok_then_all_down()
        with self.assertRaises(SystemExit):
            wab.ensure_roles(self.cfg, refresh=True)
        st = wab.load_state(self.cfg)
        for k in self.KEYS:
            self.assertNotIn(k, st)
        n = self.probe.call_count
        with mock.patch.object(wab, "prepare_worktree") as pw:
            with self.assertRaises(SystemExit):
                wab.launch(self.cfg, "W1", str(self.prompt))
        self.assertGreater(self.probe.call_count, n)
        pw.assert_not_called()
        self.assertEqual(self.sh_calls, [])

    def test_resolve_roles_refusal_drops_keys_in_memory(self):
        st = {"waves": {}}
        wab.resolve_roles(self.cfg, st, probe=lambda m, *a, **k: (True, "rc=0"))
        self.assertIn("models", st)
        for info in st["models"].values():
            info["ok"] = False       # кэш устарел: модели пропали
        with self.assertRaises(SystemExit):
            wab.resolve_roles(self.cfg, st, probe=lambda m, *a, **k: (False, "rc=1"))
        for k in self.KEYS:
            self.assertNotIn(k, st)

    def test_event_text_when_fallback_model_itself_down(self):
        self.probe.side_effect = lambda m, *a, **k: (False, "rc=1")
        with self.assertRaises(SystemExit):
            wab.ensure_roles(self.cfg)
        ev = [e for e in self.events if e.startswith("модель opus")]
        self.assertEqual(len(ev), 1)
        self.assertIn("fallback_model совпадает", ev[0])
        self.assertIn("запуск невозможен", ev[0])
        self.assertNotIn("→ fallback opus", ev[0])


class TestModelsCommand(LaunchEnv):
    def run_models(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = wab.main(["models", str(self.dir / "waves.json"), *extra])
        return rc, out.getvalue()

    def test_prints_roles_and_fallback(self):
        rc, out = self.run_models()
        self.assertEqual(rc, 0)
        self.assertIn("tester", out)
        self.assertIn("sonnet", out)
        self.assertIn("fallback", out)

    def test_refresh_rechecks(self):
        self.run_models()
        n = self.probe.call_count
        self.run_models()
        self.assertEqual(self.probe.call_count, n)
        self.run_models("--refresh")
        self.assertEqual(self.probe.call_count, 2 * n)


class TestNoticesOnce(Tmp):
    """Событие о недоступной модели — одно, пока она не станет доступной (отметка model_notices)."""

    def setUp(self):
        super().setUp()
        self.avail = {"fable": False}
        self.probe = mock.Mock(side_effect=lambda m, *a, **k: (self.avail.get(m, True), "rc=0" if self.avail.get(m, True) else "rc=1"))
        p = mock.patch.object(wab, "probe_model", self.probe)
        p.start()
        self.addCleanup(p.stop)

    def test_refresh_does_not_repeat_event(self):
        wab.ensure_roles(self.cfg)
        n = self.probe.call_count
        wab.ensure_roles(self.cfg, refresh=True)
        wab.ensure_roles(self.cfg, refresh=True)
        self.assertGreater(self.probe.call_count, n)   # refresh реально перепроверяет
        self.assertEqual(len(self.events), 1)

    def test_refusal_twice_one_event_no_cache(self):
        self.avail.update({"opus": False, "sonnet": False, "haiku": False})
        for _ in range(2):
            with self.assertRaises(SystemExit):
                wab.ensure_roles(self.cfg)
        st = wab.load_state(self.cfg)
        self.assertNotIn("models", st)
        self.assertEqual(len([e for e in self.events if "fable" in e]), 1)

    def test_recovered_then_unavailable_again_new_event(self):
        wab.ensure_roles(self.cfg)
        self.avail["fable"] = True
        wab.ensure_roles(self.cfg, refresh=True)
        self.assertEqual(len(self.events), 1)
        self.avail["fable"] = False
        wab.ensure_roles(self.cfg, refresh=True)
        self.assertEqual(len([e for e in self.events if "fable" in e]), 2)

    def test_launch_busy_does_not_probe(self):
        wab.save_state(self.cfg, {"waves": {"W1": {"tmux": "x"}}, "current": "W1"})
        prompt = self.dir / "p.md"
        prompt.write_text("задача\n", encoding="utf-8")
        with mock.patch.object(wab, "tmux_alive", return_value=True):
            with self.assertRaises(SystemExit) as cm:
                wab.launch(self.cfg, "W2", str(prompt))
        self.assertIn("сейчас идёт волна W1", str(cm.exception))
        self.probe.assert_not_called()

    def test_launch_existing_tmux_does_not_probe(self):
        prompt = self.dir / "p.md"
        prompt.write_text("задача\n", encoding="utf-8")
        with mock.patch.object(wab, "tmux_alive", return_value=True):
            with self.assertRaises(SystemExit) as cm:
                wab.launch(self.cfg, "W1", str(prompt))
        self.assertIn("уже существует", str(cm.exception))
        self.probe.assert_not_called()


_CHILD = r"""
import sys, time, pathlib
sys.path.insert(0, sys.argv[2])
import wab
cfg = wab.load_waves(sys.argv[1])
def probe(m, *a, **k):
    time.sleep(1.0)
    return (m != "fable", "rc=0" if m != "fable" else "rc=1")
wab.probe_model = probe
wab.ensure_roles(cfg)
"""


class TestConcurrentNotice(unittest.TestCase):
    def test_two_processes_one_event(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_for(t)
            cmd = [sys.executable, "-B", "-c", _CHILD, str(pathlib.Path(t) / "waves.json"), str(ROOT / "scripts")]
            procs = [subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
            for p in procs:
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
            log = (cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
            self.assertEqual(len([l for l in log.splitlines() if "fable" in l]), 1, log)


class TestDashRoles(Tmp):
    def test_fallback_mark_in_header(self):
        st = {"waves": {}, "roles_effective": dict(self.cfg["roles"], tester="opus"),
              "role_fallbacks": {"tester": {"from": "sonnet", "to": "opus"}}}
        line = dash.roles_line(self.cfg, st)
        self.assertIn("⚠ tester: sonnet → opus (fallback)", line)
        self.assertIn("coder", line)

    def test_old_state_renders(self):
        st = {"waves": {}}
        line = dash.roles_line(self.cfg, st)
        self.assertNotIn("fallback", line)
        self.assertIn("coder", line)
        dash.header(self.cfg, st)  # не падает


class TestDocs(unittest.TestCase):
    def test_protocol(self):
        t = (ROOT / "PROTOCOL.md").read_text(encoding="utf-8")
        for s in ("wave-tester", "wave-reviewer", "wave-reader", "BLOCKED", "Сценарий волны"):
            self.assertIn(s, t)
        self.assertIn("красн", t.lower())
        for line in t.splitlines():
            if "AskUserQuestion" in line:
                self.assertTrue("запрещ" in line or "не используй" in line.lower() or "отключ" in line,
                                f"AskUserQuestion не как запрет: {line}")

    def test_skill(self):
        t = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(t.startswith("---\nname: wave-autobot\n"))
        for s in ("roles.architect", "waves.md", "plan_sha256", "wab.py watch", "dash.py", "wab.py models",
                  "wab.py launch", "wab.py validate"):
            self.assertIn(s, t)
        self.assertIn("SKILL.md", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
