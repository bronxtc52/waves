"""W3: защита запуска (tmux >= 3.2, пин плана, сбой new-session, прерванный launch, dispatcher.lock, UTF-8)."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import helpers
from helpers import ROOT, good, write_json

import wab
import waves_config

PLAN = "# План\n\nВолны W1, W2.\n"
PLAN_SHA = hashlib.sha256(PLAN.encode("utf-8")).hexdigest()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.data = good()
        self.cfg_path = write_json(self.dir, self.data)
        self.cfg = wab.load_waves(str(self.cfg_path))
        self.prompt = self.dir / "p.md"
        self.prompt.write_text("задача\n", encoding="utf-8")

    def reload(self):
        write_json(self.dir, self.data)
        self.cfg = wab.load_waves(str(self.cfg_path))

    def log(self):
        p = self.cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def mocks(self, **override):
        """Побочные эффекты launch подменены; вызовы sh пишутся в self.sh_calls."""
        self.sh_calls = []
        spec = {"tmux_alive": {"return_value": False},
                "prepare_worktree": {"return_value": str(self.dir / "wt")},
                "sh": {"side_effect": lambda *a, **k: self.sh_calls.append(a)},
                "wait_ready": {"return_value": True},
                "send_text": {"return_value": None}}
        spec.update(override)
        pt = helpers.stub_ensure_roles(wab)
        pt.start()
        self.addCleanup(pt.stop)
        self.m = {}
        for name, kw in spec.items():
            p = mock.patch.object(wab, name, **kw)
            self.m[name] = p.start()
            self.addCleanup(p.stop)

    def launch(self, wave="W1"):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return wab.launch(self.cfg, wave, str(self.prompt))


class TestTmuxVersion(_Base):
    def test_parse(self):
        p = wab.parse_tmux_version
        self.assertEqual(p("tmux 3.4"), (3, 4))
        self.assertEqual(p("tmux 3.2a"), (3, 2))
        self.assertEqual(p("tmux next-3.5"), (3, 5))
        self.assertGreater(p("tmux master"), (3, 99))
        self.assertIsNone(p("tmux"))
        self.assertIsNone(p(""))
        self.assertIsNone(p(None))

    def run_require(self, stdout="tmux 3.4\n", rc=0, exc=None):
        def fake(*a, **k):
            if exc:
                raise exc
            return subprocess.CompletedProcess(a, rc, stdout, "")
        with mock.patch.object(wab.subprocess, "run", side_effect=fake):
            return wab.require_tmux()

    def test_ok_versions(self):
        for out in ("tmux 3.2\n", "tmux 3.4\n", "tmux 3.2a\n", "tmux 4.0\n", "tmux next-3.5\n"):
            self.run_require(out)

    def test_old_refused_with_text(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_require("tmux 3.1c\n")
        self.assertIn("3.2", str(cm.exception))
        self.assertIn("3.1", str(cm.exception))

    def test_missing_tmux_refused(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_require(exc=FileNotFoundError("tmux"))
        self.assertIn("tmux", str(cm.exception))

    def test_unparseable_refused(self):
        with self.assertRaises(SystemExit):
            self.run_require("что-то\n")
        with self.assertRaises(SystemExit):
            self.run_require("tmux 3.4\n", rc=1)

    def test_launch_checks_before_worktree_and_reservation(self):
        self.mocks()
        with mock.patch.object(wab, "require_tmux", side_effect=SystemExit("tmux слишком старый")):
            with self.assertRaises(SystemExit) as cm:
                self.launch()
        self.assertIn("старый", str(cm.exception))
        self.m["prepare_worktree"].assert_not_called()
        self.assertEqual(self.sh_calls, [])
        self.assertIsNone(wab.load_state(self.cfg).get("current"))

    def test_watch_checks_at_start(self):
        with mock.patch.object(wab, "require_tmux", side_effect=SystemExit("tmux слишком старый")), \
                mock.patch.object(wab, "tick") as tick:
            with self.assertRaises(SystemExit):
                wab.watch(self.cfg, str(self.cfg_path))
        tick.assert_not_called()


class TestPlanPin(_Base):
    def pin(self, sha=PLAN_SHA):
        self.data["plan_sha256"] = sha
        self.reload()

    def blocked(self):
        self.mocks()
        with self.assertRaises(SystemExit) as cm:
            self.launch()
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("BLOCKED: plan changed since approval:"), msg)
        self.assertIn("plan changed since approval", self.log())
        self.m["prepare_worktree"].assert_not_called()
        self.assertEqual(self.sh_calls, [])
        self.assertIsNone(wab.load_state(self.cfg).get("current"))
        return msg

    def test_config_derives_plan_path(self):
        self.assertEqual(self.cfg["plan_path"], self.dir / "waves.md")

    def test_match_launches(self):
        (self.dir / "waves.md").write_text(PLAN, encoding="utf-8")
        self.pin()
        self.mocks()
        self.assertTrue(self.launch())

    def test_missing_plan(self):
        self.pin()
        self.assertIn("waves.md", self.blocked())

    def test_changed_plan(self):
        (self.dir / "waves.md").write_text(PLAN + "правка\n", encoding="utf-8")
        self.pin()
        self.assertIn("sha256", self.blocked())

    def test_symlink_refused(self):
        (self.dir / "real.md").write_text(PLAN, encoding="utf-8")
        os.symlink(self.dir / "real.md", self.dir / "waves.md")
        self.pin()
        self.blocked()

    def test_directory_refused(self):
        (self.dir / "waves.md").mkdir()
        self.pin()
        self.blocked()

    def test_too_big_refused(self):
        (self.dir / "waves.md").write_bytes(b"x" * (1024 * 1024 + 1))
        self.pin()
        self.assertIn("1", self.blocked())

    def test_unset_warns_and_does_not_check(self):
        self.mocks()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        self.assertIn("plan_sha256", err.getvalue())


class TestPlanChangedDuringRoles(_Base):
    def test_plan_edited_inside_ensure_roles_blocks_before_reserve(self):
        (self.dir / "waves.md").write_text(PLAN, encoding="utf-8")
        self.data["plan_sha256"] = PLAN_SHA
        self.reload()
        self.mocks()

        def slow_roles(cfg, refresh=False):
            (self.dir / "waves.md").write_text(PLAN + "правка во время проб\n", encoding="utf-8")
            return dict(cfg["roles"]), {}

        with mock.patch.object(wab, "ensure_roles", side_effect=slow_roles):
            with self.assertRaises(SystemExit) as cm:
                self.launch()
        self.assertTrue(str(cm.exception).startswith("BLOCKED: plan changed since approval:"), str(cm.exception))
        self.assertIsNone(wab.load_state(self.cfg).get("current"))
        self.assertNotIn("W1", wab.load_state(self.cfg).get("waves", {}))
        self.m["prepare_worktree"].assert_not_called()
        self.assertEqual(self.sh_calls, [])


class TestNewSessionFailure(_Base):
    def run_fail(self, exc):
        def sh(*a, **k):
            if "new-session" in a:
                raise exc
        self.mocks(sh={"side_effect": sh})
        with self.assertRaises(SystemExit) as cm:
            self.launch()
        st = wab.load_state(self.cfg)
        self.assertIsNone(st.get("current"))
        self.assertNotIn("W1", st["waves"])
        return str(cm.exception)

    def test_called_process_error(self):
        msg = self.run_fail(subprocess.CalledProcessError(1, ["tmux"], stderr="duplicate session: wab-demo-w1\n"))
        self.assertIn("duplicate session", msg)

    def test_oserror(self):
        msg = self.run_fail(FileNotFoundError(2, "No such file", "tmux"))
        self.assertIn("tmux", msg)


class _InterruptedBase(_Base):
    def setUp(self):
        super().setUp()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.dead_pid = dead.pid
        self.alive = True
        for name, kw in (("tmux_alive", {"side_effect": lambda *a, **k: self.alive}),
                         ("pane_text", {"return_value": ""}),
                         ("send_keys", {"return_value": None})):
            pt = mock.patch.object(wab, name, **kw)
            pt.start()
            self.addCleanup(pt.stop)
        self.st = {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-w1", "cwd": str(self.dir), "phase": "starting", "launcher_pid": self.dead_pid,
            "restarts": 0, "notified": {}, "sessions": ["s"]}}}

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return wab.tick(self.cfg, self.st)


class TestInterruptedLaunch(_InterruptedBase):
    def test_window_alive_blocked_once(self):
        self.assertTrue(self.tick())
        w = self.st["waves"]["W1"]
        self.assertEqual(w["phase"], "not_ready")
        status = (wab.wave_path(self.cfg, "W1") / "status").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("BLOCKED: launch прерван до отправки задачи"), status)
        self.assertTrue(self.tick())
        self.assertTrue(self.tick())
        self.assertEqual(self.log().count("launch прерван"), 1)
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["phase"], "not_ready")

    def test_no_window_is_dead(self):
        self.alive = False
        self.assertFalse(self.tick())
        self.assertEqual(self.st["waves"]["W1"]["phase"], "dead")

    def test_check_launchable_tells_what_to_do(self):
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        msg = str(cm.exception)
        self.assertIn("kill-session", msg)
        self.assertIn("=wab-demo-w1", msg)
        self.assertIn("launch", msg)

    def test_check_launchable_not_ready_after_interrupted(self):
        self.st["waves"]["W1"].update(phase="not_ready")
        self.st["waves"]["W1"].pop("launcher_pid")
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        self.assertIn("kill-session", str(cm.exception))


class TestSendingWindow(_InterruptedBase):
    """launch убит между send_text первого промпта и phase=running: окно доставки неоднозначно."""

    def setUp(self):
        super().setUp()
        self.projects = self.dir / "projects"
        self.projects.mkdir()
        pt = mock.patch.object(wab, "PROJECTS", self.projects)
        pt.start()
        self.addCleanup(pt.stop)
        self.st["waves"]["W1"]["phase"] = "sending"

    def journal(self, first_text):
        d = wab.transcript_dir(self.st["waves"]["W1"]["cwd"])
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"type": "user", "message": {"role": "user", "content": first_text}}, ensure_ascii=False)
        (d / "s.jsonl").write_text(line + "\n", encoding="utf-8")

    def test_marker_in_journal_means_delivered(self):
        self.journal(f"{wab.session_marker(self.cfg, 'W1')} Волна W1. задача")
        self.assertTrue(self.tick())
        self.assertTrue(self.tick())
        w = self.st["waves"]["W1"]
        self.assertEqual(w["phase"], "running")
        self.assertNotIn("launcher_pid", w)
        self.assertEqual(self.log().count("launch прерван после доставки, слежу дальше"), 1)
        self.assertNotIn("kill-session", self.log())

    def test_no_marker_blocked_without_kill_advice(self):
        self.journal("совсем другое сообщение")
        self.assertTrue(self.tick())
        self.assertTrue(self.tick())
        w = self.st["waves"]["W1"]
        self.assertEqual(w["phase"], "not_ready")
        status = (wab.wave_path(self.cfg, "W1") / "status").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("BLOCKED: launch прерван при отправке задачи"), status)
        self.assertIn("first-prompt.md", status)
        self.assertNotIn("kill-session", status + self.log())
        self.assertIn("tmux attach -t =wab-demo-w1", self.log())
        self.assertEqual(self.log().count("launch прерван при отправке"), 1)

    def test_no_journal_blocked(self):
        self.assertTrue(self.tick())
        self.assertEqual(self.st["waves"]["W1"]["phase"], "not_ready")

    def test_live_launcher_in_sending_is_waited_for(self):
        self.st["waves"]["W1"]["launcher_pid"] = os.getpid()
        self.assertTrue(self.tick())
        self.assertEqual(self.st["waves"]["W1"]["phase"], "sending")

    def test_check_launchable_in_sending_has_no_kill_advice(self):
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        msg = str(cm.exception)
        self.assertNotIn("kill-session", msg)
        self.assertIn("tmux attach -t =wab-demo-w1", msg)
        self.assertIn("first-prompt.md", msg)

    def _to_not_ready(self):
        self.journal("совсем другое сообщение")
        self.assertTrue(self.tick())
        self.assertEqual(self.st["waves"]["W1"]["phase"], "not_ready")

    def test_relaunch_after_sending_to_not_ready_has_no_kill_advice(self):
        self.st["waves"]["W1"]["prompt_maybe_sent"] = True
        self._to_not_ready()
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        msg = str(cm.exception)
        self.assertNotIn("kill-session", msg)
        self.assertIn("first-prompt.md", msg)

    def test_legacy_sending_without_flag_gets_flag_and_no_kill_advice(self):
        self._to_not_ready()
        self.assertTrue(self.st["waves"]["W1"].get("prompt_maybe_sent"))
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        self.assertNotIn("kill-session", str(cm.exception))

    def test_agent_status_not_overwritten_and_leads_to_running(self):
        wdir = wab.wave_path(self.cfg, "W1")
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "status").write_text("RUNNING\n", encoding="utf-8")
        self.assertTrue(self.tick())
        self.assertEqual(self.st["waves"]["W1"]["phase"], "running")
        self.assertEqual((wdir / "status").read_text(encoding="utf-8").strip(), "RUNNING")
        self.assertNotIn("BLOCKED", self.log())

    def test_starting_still_advises_kill_in_check_launchable(self):
        self.st["waves"]["W1"]["phase"] = "starting"
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        self.assertIn("kill-session", str(cm.exception))

    def test_starting_unchanged(self):
        self.st["waves"]["W1"]["phase"] = "starting"
        self.assertTrue(self.tick())
        status = (wab.wave_path(self.cfg, "W1") / "status").read_text(encoding="utf-8")
        self.assertIn("kill-session", status)


class TestLegacyStarting(_InterruptedBase):
    """Запись до W3: фаза starting без ключа sessions — launch мог успеть отправить промпт."""

    def setUp(self):
        super().setUp()
        self.st["waves"]["W1"].pop("sessions")

    def test_tick_blocked_without_kill_advice(self):
        for status in (None, "STARTING"):
            with self.subTest(status=status):
                self.st["waves"]["W1"].update(phase="starting", launcher_pid=self.dead_pid, notified={})
                wdir = wab.wave_path(self.cfg, "W1")
                wdir.mkdir(parents=True, exist_ok=True)
                if status:
                    (wdir / "status").write_text(status + "\n", encoding="utf-8")
                self.assertTrue(self.tick())
                w = self.st["waves"]["W1"]
                self.assertEqual(w["phase"], "not_ready")
                self.assertTrue(w.get("prompt_maybe_sent"))
                self.assertNotIn("launcher_pid", w)
                text = (wdir / "status").read_text(encoding="utf-8")
                self.assertTrue(text.startswith("BLOCKED: launch (версия до W3)"), text)
                self.assertNotIn("kill-session", text)
                self.assertIn("first-prompt.md", text)

    def test_tick_agent_status_means_running_and_kept(self):
        wdir = wab.wave_path(self.cfg, "W1")
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "status").write_text("RUNNING\n", encoding="utf-8")
        self.assertTrue(self.tick())
        w = self.st["waves"]["W1"]
        self.assertEqual(w["phase"], "running")
        self.assertTrue(w.get("prompt_maybe_sent"))
        self.assertEqual((wdir / "status").read_text(encoding="utf-8").strip(), "RUNNING")
        self.assertNotIn("BLOCKED", self.log())
        self.assertIn("запись до W3", self.log())

    def test_check_launchable_no_kill_advice(self):
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        msg = str(cm.exception)
        self.assertNotIn("kill-session", msg)
        self.assertIn("Не закрывайте", msg)

    def test_new_format_still_advises_kill(self):
        self.st["waves"]["W1"]["sessions"] = ["s"]
        with self.assertRaises(SystemExit) as cm:
            wab._check_launchable(self.cfg, self.st, "W1", "wab-demo-w1")
        self.assertIn("kill-session", str(cm.exception))


class TestLaunchSendingPhase(_Base):
    def test_sending_saved_and_first_prompt_written_before_send_text(self):
        seen = {}

        def fake_send(name, text):
            seen["phase"] = wab.load_state(self.cfg)["waves"]["W1"]["phase"]
            seen["file"] = (wab.wave_path(self.cfg, "W1") / "first-prompt.md").exists()
            seen["flag"] = wab.load_state(self.cfg)["waves"]["W1"].get("prompt_maybe_sent") is True

        pt = helpers.stub_ensure_roles(wab)
        pt.start()
        self.addCleanup(pt.stop)
        for name, kw in {"tmux_alive": {"return_value": False},
                         "prepare_worktree": {"return_value": str(self.dir / "wt")},
                         "sh": {"return_value": None}, "wait_ready": {"return_value": True},
                         "send_text": {"side_effect": fake_send}}.items():
            p = mock.patch.object(wab, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))
        self.assertEqual(seen, {"phase": "sending", "file": True, "flag": True})
        self.assertEqual(wab.load_state(self.cfg)["waves"]["W1"]["phase"], "running")


class TestDispatcherLock(_Base):
    @helpers.deadline(10)
    def test_second_watch_refused_and_lock_released(self):
        with wab.dispatcher_lock(self.cfg):
            with mock.patch.object(wab, "require_tmux"), mock.patch.object(wab, "tick") as tick:
                with self.assertRaises(SystemExit) as cm:
                    wab.watch(self.cfg, str(self.cfg_path))
            tick.assert_not_called()
        self.assertIn("другой watch", str(cm.exception))
        self.assertTrue(cm.exception.code)
        with wab.dispatcher_lock(self.cfg):
            pass   # снят после выхода

    @helpers.deadline(10)
    def test_watch_holds_lock_for_whole_loop_and_releases(self):
        seen = []

        def tick(cfg, st, wj=None):
            try:
                with wab.dispatcher_lock(cfg):
                    seen.append("taken")
            except SystemExit:
                seen.append("held")
            return False

        with mock.patch.object(wab, "require_tmux"), mock.patch.object(wab, "tick", side_effect=tick), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, str(self.cfg_path))
        self.assertEqual(seen, ["held"])
        with wab.dispatcher_lock(self.cfg):
            pass

    def test_launch_does_not_take_it(self):
        self.mocks_done = True
        pt = helpers.stub_ensure_roles(wab)
        pt.start()
        self.addCleanup(pt.stop)
        for name, kw in {"tmux_alive": {"return_value": False},
                         "prepare_worktree": {"return_value": str(self.dir)},
                         "sh": {"return_value": None}, "wait_ready": {"return_value": True},
                         "send_text": {"return_value": None}}.items():
            p = mock.patch.object(wab, name, **kw)
            p.start()
            self.addCleanup(p.stop)
        with wab.dispatcher_lock(self.cfg), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(wab.launch(self.cfg, "W1", str(self.prompt)))

    def test_symlink_lock_refused(self):
        self.cfg["run_dir"].mkdir(parents=True)
        os.symlink(self.dir / "elsewhere", self.cfg["run_dir"] / "dispatcher.lock")
        with self.assertRaises(SystemExit):
            with wab.dispatcher_lock(self.cfg):
                pass


class TestUtf8(_Base):
    def test_sh_decodes_utf8_with_replace(self):
        with mock.patch.object(wab.subprocess, "run") as run:
            wab.sh("tmux", "x")
        kw = run.call_args.kwargs
        self.assertEqual((kw.get("encoding"), kw.get("errors")), ("utf-8", "replace"))

    def test_read_replaces_bad_bytes(self):
        p = self.dir / "status"
        p.write_bytes("BLOCKED: вопрос \xff".encode("utf-8")[:-1] + b"\xff")
        self.assertIn("вопрос", wab.read(p))

    def test_tmux_text_calls_use_u(self):
        calls = []
        rec = lambda *a, **k: calls.append(list(a)) or subprocess.CompletedProcess(a, 0, "", "")
        with mock.patch.object(wab, "sh", side_effect=rec):
            wab.pane_text("n")
            wab.send_keys("n", "-l", "привет")
        self.assertEqual([c[:3] for c in calls], [["tmux", "-u", "capture-pane"], ["tmux", "-u", "send-keys"]])

    def test_u_is_accepted_by_real_tmux_in_that_position(self):
        import shutil
        if not shutil.which("tmux"):
            self.skipTest("нет tmux")
        sock = f"wabu{os.getpid()}"
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        run = lambda *a: subprocess.run(["tmux", "-L", sock, *a], capture_output=True, text=True, env=env)
        try:
            self.assertEqual(run("new-session", "-d", "-s", "u", "-x", "40", "-y", "10", "cat").returncode, 0)
            r = run("-u", "capture-pane", "-p", "-t", "=u:")
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run("-u", "send-keys", "-t", "=u:", "-l", "привет")
            self.assertEqual(r.returncode, 0, r.stderr)
        finally:
            run("kill-server")

    ENV = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "PYTHONIOENCODING": ""}

    def child(self, *args):
        env = {**os.environ, **self.ENV}
        cmd = [sys.executable, "-B", *([] if args and args[0].endswith(".py") else [str(ROOT / "scripts" / "wab.py")]), *args]
        return subprocess.run(cmd, capture_output=True, env=env, cwd=str(ROOT / "scripts"))

    def prepare_cyrillic(self):
        d = self.dir / "прогон"
        d.mkdir()
        data = good()
        data["waves"][0]["title"] = "Каркас — надёжность"
        cfg = write_json(d, data)
        c = wab.load_waves(str(cfg))
        wdir = wab.wave_dir(c, "W1")
        (wdir / "status").write_bytes("BLOCKED: окно не готово — проверьте \xff".encode("utf-8")[:-1] + b"\xff")
        wab.save_state(c, {"current": "W1", "waves": {"W1": {
            "tmux": "wab-demo-w1", "phase": "running", "restarts": 0, "notified": {}}}})
        return cfg

    def test_status_and_validate_survive_c_locale(self):
        cfg = self.prepare_cyrillic()
        for cmd in ("status", "validate"):
            r = self.child(cmd, str(cfg))
            self.assertEqual(r.returncode, 0, r.stderr.decode("ascii", "replace"))
            self.assertNotIn(b"Traceback", r.stderr)

    def test_event_survives_c_locale(self):
        cfg = self.prepare_cyrillic()
        script = self.dir / "ev.py"  # исходник в UTF-8: argv с кириллицей в C-локали не передать
        script.write_text("import sys; sys.path.insert(0, %r); import wab; c = wab.load_waves(sys.argv[1]); "
                          "wab.event(c, 'W1: окно закрыто — проверьте', trusted=', cwd /tmp/прогон')\n"
                          % str(ROOT / "scripts"), encoding="utf-8")
        r = self.child(str(script), str(cfg))
        self.assertEqual(r.returncode, 0, r.stderr.decode("ascii", "replace"))
        log = (pathlib.Path(cfg).parent / "runs" / "2026-10-02" / "events.log").read_text(encoding="utf-8")
        self.assertIn("окно закрыто — проверьте", log)
        self.assertIn("/tmp/прогон", log)

    def test_all_text_io_declares_encoding(self):
        import re
        for f in (ROOT / "scripts").glob("*.py"):
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\.(read_text|write_text)\(", line) and "encoding" not in line:
                    self.fail(f"{f.name}:{n}: нет encoding: {line.strip()}")
                m = re.search(r"\bopen\(([^)]*)\)", line)
                if m and "def " not in line and "os.open" not in line and "tmux" not in line:
                    args = m.group(1)
                    if "encoding" not in args and not re.search(r"""["'][rwa]?[+]?b[+]?["']""", args):
                        self.fail(f"{f.name}:{n}: open без encoding/бинарного режима: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
