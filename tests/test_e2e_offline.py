"""W5: офлайн-сквозной тест всего потока — настоящие процессы `wab.py launch` и `wab.py watch`,
приватный tmux-сервер, подставные `claude` и `gh` (tests/fakes/), локальный bare-репозиторий как origin.

Ни сети, ни настоящих claude/gh. tmux — настоящий, но только на уникальном сокете `-L`: в PATH лежит
shim `tmux`, который добавляет `-L <сокет> -f /dev/null` и снимает TMUX. HOME — временный каталог
(журналы сессий ~/.claude/projects/… пишутся туда). Нет tmux >= 3.2 — тест пропускается, но при
WAB_E2E_REQUIRED=1 (так в CI) это провал, а не пропуск.

Ожидания — опрос условий с общим дедлайном теста; упавший watch или нарушение сценария подставным
claude (ctl/errors.log) обрывают ожидание сразу, с журналами в тексте ошибки.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)

ROOT = pathlib.Path(__file__).resolve().parent.parent
WAB = ROOT / "scripts" / "wab.py"
FAKES = pathlib.Path(__file__).resolve().parent / "fakes"
REQUIRED = os.environ.get("WAB_E2E_REQUIRED") == "1"
TEST_SECONDS = 120   # дедлайн одного сценария целиком
CHAIN, RUN_ID, REPO = "e2e", "run1", "e2e-owner/demo"

_spec = importlib.util.spec_from_file_location("fake_gh", FAKES / "fake_gh.py")
fake_gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_gh)


def _real_tmux():
    """(путь, версия) настоящего tmux — ищется ДО подмены PATH; (None, причина), если не годится."""
    path = shutil.which("tmux")
    if not path:
        return None, "нет tmux в PATH"
    r = subprocess.run([path, "-V"], capture_output=True, text=True)
    m = re.search(r"(\d+)\.(\d+)", r.stdout or "")
    if r.returncode != 0 or not m:
        return None, f"версия tmux не определена: {r.stdout!r}"
    if (int(m.group(1)), int(m.group(2))) < (3, 2):
        return None, f"tmux {m.group(0)} старше 3.2"
    return path, m.group(0)


TMUX, TMUX_WHY = _real_tmux()

PLAN = """# План: e2e
## Волна W1 — hello
- Цель: файл hello.py с тестом
- Готово, когда: python3 hello.py печатает hello
- Проверка: python3 -m unittest
- Зависит от: нет
## Волна W2 — флаг
- Цель: флаг --name
- Готово, когда: python3 hello.py --name x печатает hello, x
- Проверка: python3 -m unittest
- Зависит от: W1
"""

HELLO = 'print("hello")\n'
HELLO2 = 'import sys\nprint("hello" + (", " + sys.argv[2] if len(sys.argv) > 2 else ""))\n'


def scenario_blocked_checkpoint_gate():
    """Сценарий (а): BLOCKED, контрольная точка с /clear, красный CI на первом DONE."""
    m1 = f"[wab:{CHAIN}/{RUN_ID}/W1]"
    return {
        "W1": [
            {"op": "msg", "prefix": m1},
            {"op": "status", "text": "RUNNING"},
            {"op": "usage", "tokens": 200},
            {"op": "status", "text": "BLOCKED: Какой вариант выбрать, A или B? Рекомендую A."},
            {"op": "msg", "prefix": "answer-A"},
            {"op": "status", "text": "RUNNING"},
            {"op": "commit", "file": "part1.txt", "content": "часть 1\n"},
            {"op": "usage", "tokens": 5000},
            {"op": "msg", "prefix": "WAB-CHECKPOINT"},
            {"op": "handoff", "done": "часть 1 (part1.txt)", "next": "часть 2: hello.py"},
            {"op": "status", "text": "HANDOFF_READY"},
            {"op": "msg", "prefix": f"{m1} Продолжаем", "new_session": True},
            {"op": "read_handoff"},
            {"op": "wait_file", "name": "go-W1-bound"},
            {"op": "commit", "file": "hello.py", "content": HELLO},
            {"op": "pr_create"},
            {"op": "finish", "next_prompt": "NEXT-W2: добавь флаг --name, план рядом с waves.json"},
            {"op": "msg", "prefix": "[wab] Гейт мерджа не пройден"},
            {"op": "wait_file", "name": "go-W1-green"},
            {"op": "status", "text": "DONE"},
        ],
        "W2": [
            {"op": "msg", "prefix": f"[wab:{CHAIN}/{RUN_ID}/W2]", "contains": "NEXT-W2"},
            {"op": "status", "text": "RUNNING"},
            {"op": "commit", "file": "hello.py", "content": HELLO2},
            {"op": "pr_create"},
            {"op": "finish"},
        ],
    }


def scenario_automerge():
    """Сценарий (б): обе волны без вопросов; PR W1 открыт черновиком (диспетчер делает `gh pr ready`)."""
    return {
        "W1": [
            {"op": "msg", "prefix": f"[wab:{CHAIN}/{RUN_ID}/W1]"},
            {"op": "status", "text": "RUNNING"},
            {"op": "commit", "file": "hello.py", "content": HELLO},
            {"op": "pr_create", "draft": True},
            {"op": "finish", "next_prompt": "NEXT-W2: добавь флаг --name"},
        ],
        "W2": [
            {"op": "msg", "prefix": f"[wab:{CHAIN}/{RUN_ID}/W2]", "contains": "NEXT-W2"},
            {"op": "status", "text": "RUNNING"},
            {"op": "commit", "file": "hello.py", "content": HELLO2},
            {"op": "pr_create"},
            {"op": "finish"},
        ],
    }


def _git(*args, cwd=None, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


class _E2E(unittest.TestCase):
    automerge = False
    scenario = staticmethod(scenario_automerge)

    def setUp(self):
        if TMUX is None:
            if REQUIRED:
                self.fail(f"WAB_E2E_REQUIRED=1, а tmux >= 3.2 недоступен: {TMUX_WHY}")
            self.skipTest(f"нужен tmux >= 3.2 ({TMUX_WHY}); в CI обязателен через WAB_E2E_REQUIRED=1")
        self.t0 = time.monotonic()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="wab-e2e-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.socket = f"wab-e2e-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.procs = []
        self.addCleanup(self._teardown_procs)
        self.home, self.ctl, self.bin = (self.tmp / n for n in ("home", "ctl", "bin"))
        for d in (self.home, self.ctl, self.bin):
            d.mkdir()
        self._write_shims()
        self.env = self._env()
        self._repos()
        self._plan()
        (self.ctl / "config.json").write_text(json.dumps({"home": str(self.home), "base": "main"}),
                                              encoding="utf-8")
        (self.ctl / "scenario.json").write_text(json.dumps(self.scenario(), ensure_ascii=False), encoding="utf-8")
        fail_first = getattr(self, "red_ci_first", False)
        (self.ctl / "gh-state.json").write_text(json.dumps({
            "owner": REPO.split("/")[0], "name": REPO.split("/")[1], "origin": str(self.origin),
            "next": 1, "prs": [],
            "checks": [{"name": "tests", "conclusion": "failure" if fail_first else "success"},
                       {"name": "lint", "conclusion": "success"}]}), encoding="utf-8")

    # ---------- окружение ----------
    def _write_shims(self):
        py = shlex.quote(sys.executable)
        shims = {
            "tmux": f'exec env -u TMUX {shlex.quote(TMUX)} -L {shlex.quote(self.socket)} -f /dev/null "$@"\n',
            "claude": f'exec {py} {shlex.quote(str(FAKES / "fake_claude.py"))} --fake-ctl '
                      f'{shlex.quote(str(self.ctl))} "$@"\n',
            "gh": f'exec {py} {shlex.quote(str(FAKES / "fake_gh.py"))} --fake-ctl {shlex.quote(str(self.ctl))} "$@"\n',
        }
        for name, body in shims.items():
            p = self.bin / name
            p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
            p.chmod(0o755)

    def _env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "TMUX_PANE", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        env.update(PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", HOME=str(self.home),
                   PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="e2e", GIT_AUTHOR_EMAIL="e2e@example.com",
                   GIT_COMMITTER_NAME="e2e", GIT_COMMITTER_EMAIL="e2e@example.com")
        return env

    def _repos(self):
        self.origin = self.tmp / "origin.git"
        self.checkout = self.tmp / "checkout"
        _git("init", "-q", "--bare", "-b", "main", str(self.origin), env=self.env)
        seed = self.tmp / "seed"
        _git("init", "-q", "-b", "main", str(seed), env=self.env)
        (seed / "README.md").write_text("demo\n", encoding="utf-8")
        _git("add", "README.md", cwd=seed, env=self.env)
        _git("commit", "-q", "-m", "init", cwd=seed, env=self.env)
        _git("push", "-q", str(self.origin), "main", cwd=seed, env=self.env)
        _git("clone", "-q", str(self.origin), str(self.checkout), env=self.env)

    def _plan(self):
        self.plan_dir = self.tmp / "plan"
        self.plan_dir.mkdir()
        (self.plan_dir / "waves.md").write_text(PLAN, encoding="utf-8")
        cfg = {
            "chain": CHAIN, "run_id": RUN_ID, "repo": REPO, "checkout": str(self.checkout),
            "base_branch": "main", "automerge": self.automerge, "ctx_limit": 1000, "tick_seconds": 1,
            "plan_sha256": hashlib.sha256(PLAN.encode("utf-8")).hexdigest(),
            "waves": [
                {"id": "W1", "title": "hello", "goal": "файл hello.py с тестом",
                 "done_when": ["python3 hello.py печатает hello"], "check": "python3 -m unittest"},
                {"id": "W2", "title": "флаг", "goal": "флаг --name",
                 "done_when": ["python3 hello.py --name x печатает hello, x"], "check": "python3 -m unittest",
                 "depends_on": ["W1"]},
            ],
        }
        self.waves_json = self.plan_dir / "waves.json"
        self.waves_json.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.run_dir = self.plan_dir / "runs" / RUN_ID
        (self.plan_dir / "prompt-W1.md").write_text("Сделай волну W1 по плану waves.md.\n", encoding="utf-8")

    def _teardown_procs(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(5)
        subprocess.run([TMUX, "-L", self.socket, "kill-server"], capture_output=True,
                       env={k: v for k, v in os.environ.items() if k != "TMUX"})

    # ---------- процессы диспетчера ----------
    def launch_w1(self):
        r = subprocess.run([sys.executable, "-B", str(WAB), "launch", str(self.waves_json), "W1",
                            str(self.plan_dir / "prompt-W1.md")], env=self.env, cwd=self.plan_dir,
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, f"launch W1: {r.stdout}\n{r.stderr}")

    def start_watch(self):
        self.watch_out = self.tmp / "watch.out"
        out = open(self.watch_out, "w", encoding="utf-8")
        self.addCleanup(out.close)
        p = subprocess.Popen([sys.executable, "-B", str(WAB), "watch", str(self.waves_json)], env=self.env,
                             cwd=self.plan_dir, stdout=out, stderr=subprocess.STDOUT)
        self.procs.append(p)
        self.watch = p
        return p

    # ---------- наблюдение ----------
    def state(self):
        try:
            return json.loads((self.run_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"waves": {}}

    def wave(self, wid):
        return self.state().get("waves", {}).get(wid) or {}

    def status(self, wid):
        try:
            return (self.run_dir / wid / "status").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def events(self):
        try:
            return (self.run_dir / "events.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def gh_calls(self):
        try:
            lines = (self.ctl / "gh.log").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(ln) for ln in lines if ln.strip()]

    def dispatcher_merges(self):
        return [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "merge"]]

    def errors(self):
        try:
            return (self.ctl / "errors.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def actions(self, wid):
        try:
            return (self.ctl / f"actions-{wid}.log").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def tmux(self, *args):
        return subprocess.run(["tmux", *args], env=self.env, capture_output=True, text=True)

    def alive(self, wid):
        return self.tmux("has-session", "-t", f"=wab-{CHAIN}-{wid.lower()}").returncode == 0

    def diagnostics(self):
        def tail(p, n=40):
            try:
                return "\n".join(pathlib.Path(p).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
            except OSError:
                return "(нет файла)"
        panes = []
        for wid in ("W1", "W2"):
            r = self.tmux("capture-pane", "-p", "-t", f"=wab-{CHAIN}-{wid.lower()}:")
            if r.returncode == 0:
                panes.append(f"--- окно {wid} ---\n" + "\n".join(r.stdout.strip().splitlines()[-15:]))
        return (f"\n--- events.log ---\n{tail(self.run_dir / 'events.log')}"
                f"\n--- watch ---\n{tail(getattr(self, 'watch_out', ''), 20)}"
                f"\n--- errors.log ---\n{self.errors()}"
                f"\n--- actions ---\n{self.actions('W1')}\n{self.actions('W2')}"
                f"\n--- status --- W1={self.status('W1')!r} W2={self.status('W2')!r}"
                f"\n--- gh ---\n{[c['argv'][:3] for c in self.gh_calls()][-12:]}\n" + "\n".join(panes))

    def wait_for(self, cond, what, watch_may_exit=False):
        """Опрос условия до общего дедлайна теста. Ошибка подставного claude или выход watch — провал сразу."""
        while True:
            if cond():
                return
            if self.errors():
                self.fail(f"нарушение сценария при ожидании «{what}»:{self.diagnostics()}")
            w = getattr(self, "watch", None)
            if w is not None and w.poll() is not None and not watch_may_exit:
                self.fail(f"watch завершился (код {w.returncode}) при ожидании «{what}»:{self.diagnostics()}")
            if time.monotonic() - self.t0 > TEST_SECONDS:
                self.fail(f"не дождались «{what}» за {TEST_SECONDS} с:{self.diagnostics()}")
            time.sleep(0.2)

    def touch(self, name):
        (self.ctl / name).write_text("", encoding="utf-8")

    def human_merge(self, number):
        return fake_gh.merge_pr(str(self.ctl), number)

    def origin_main(self):
        return _git("--git-dir", str(self.origin), "log", "--format=%H %s", "main", env=self.env).splitlines()

    def finish_chain(self):
        self.wait_for(lambda: self.watch.poll() is not None, "выход watch после цепочки", watch_may_exit=True)
        self.assertEqual(self.watch.returncode, 0, self.diagnostics())
        self.assertIn("цепочка завершена", self.events())
        chain = (self.run_dir / "chain-result.md").read_text(encoding="utf-8")
        st = self.state()
        for wid, number in (("W1", 1), ("W2", 2)):
            w = st["waves"][wid]
            self.assertEqual(w["phase"], "merged", wid)
            self.assertEqual(w["pr"]["number"], number)
            self.assertIn(f"#{number} https://github.com/{REPO}/pull/{number}", chain)
            self.assertIn(w["merged"]["oid"][:12], chain)
        self.assertNotIn("Не смержена", chain)
        log = self.origin_main()
        self.assertEqual(len(log), 3, log)   # init + два squash-коммита
        self.assertTrue(log[0].endswith("(#2)") and log[1].endswith("(#1)"), log)
        main_hello = _git("--git-dir", str(self.origin), "show", "main:hello.py", env=self.env)
        self.assertEqual(main_hello + "\n", HELLO2)
        self.assertFalse(self.alive("W1") or self.alive("W2"), "окна волн должны быть закрыты")
        self.assertEqual(self.errors(), "")
        # журналы сессий легли во временный HOME, а не в настоящий
        for wid in ("W1", "W2"):
            enc = re.sub(r"[^A-Za-z0-9]", "-", st["waves"][wid]["cwd"])
            self.assertTrue((self.home / ".claude" / "projects" / enc).is_dir(), wid)
            self.assertFalse((pathlib.Path.home() / ".claude" / "projects" / enc).exists(), wid)
        return chain


class TestChainHumanMerge(_E2E):
    """(а) automerge=false: BLOCKED → ответ, контрольная точка → /clear → продолжение, красный CI → BLOCKED
    merge gate, зелёный → awaiting_merge без `gh pr merge`, мердж человеком → W2 сама, chain-result.md."""
    automerge = False
    red_ci_first = True
    scenario = staticmethod(scenario_blocked_checkpoint_gate)

    def test_chain(self):
        self.launch_w1()
        self.assertEqual(self.wave("W1").get("phase"), "running")
        self.start_watch()

        # вопрос BLOCKED → ответ человека в окно → RUNNING
        self.wait_for(lambda: "W1: BLOCKED: Какой вариант" in self.events(), "событие BLOCKED W1")
        r = self.tmux("send-keys", "-t", f"=wab-{CHAIN}-w1:", "-l", "answer-A")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.tmux("send-keys", "-t", f"=wab-{CHAIN}-w1:", "Enter")
        self.wait_for(lambda: self.actions("W1").count("status:RUNNING") >= 2, "RUNNING после ответа")

        # контекст выше ctl_limit → WAB-CHECKPOINT → HANDOFF_READY → /clear → продолжение по метке
        self.wait_for(lambda: "запрошена контрольная точка" in self.events(), "запрос контрольной точки")
        self.wait_for(lambda: "handoff готов, отправлен /clear" in self.events(), "/clear")
        self.wait_for(lambda: "новая сессия привязана по метке" in self.events(), "привязка новой сессии")
        w1 = self.wave("W1")
        self.assertEqual(len(w1["sessions"]), 2, w1)
        self.assertEqual(w1["restarts"], 1)
        acts = self.actions("W1")
        self.assertTrue(acts[0].startswith("start:" + w1["sessions"][0]), acts)
        self.assertIn("clear:" + w1["sessions"][1], acts)
        self.assertIn("handoff-next:часть 2: hello.py", acts)
        journal = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", w1["cwd"])
        self.assertTrue((journal / f"{w1['sessions'][1]}.jsonl").exists())
        self.touch("go-W1-bound")

        # первый DONE при красном CI → BLOCKED: merge gate, W2 не стартует, мерджа нет
        self.wait_for(lambda: self.status("W1").startswith("BLOCKED: merge gate:"), "BLOCKED: merge gate")
        self.assertIn("tests=failure", self.status("W1"))
        self.wait_for(lambda: any(a.startswith("msg:[wab] Гейт мерджа не пройден") for a in self.actions("W1")),
                      "причина отказа гейта в окне волны")
        self.assertNotIn("W2", self.state()["waves"])
        self.assertEqual(self.dispatcher_merges(), [])

        # CI зелёный, волна снова DONE → awaiting_merge, и несколько тактов без `gh pr merge`
        fake_gh.update(str(self.ctl), lambda st: st["checks"][0].update(conclusion="success"))
        self.touch("go-W1-green")
        self.wait_for(lambda: self.wave("W1").get("phase") == "awaiting_merge", "фаза awaiting_merge")
        self.assertIn("W1: ждёт мерджа PR #1", self.events())
        seen = len(self.gh_calls())
        self.wait_for(lambda: len([c for c in self.gh_calls()[seen:] if c["argv"][:2] == ["pr", "list"]]) >= 2,
                      "ещё два такта гейта в ожидании мерджа")
        self.assertEqual(self.dispatcher_merges(), [])
        self.assertNotIn("W2", self.state()["waves"])
        self.assertTrue(self.alive("W1"))

        # человек мерджит PR → проверка предка, окно W1 закрыто, W2 запущена сама по next-prompt.md
        self.human_merge(1)
        self.wait_for(lambda: self.wave("W1").get("phase") == "merged", "W1 смержена")
        self.wait_for(lambda: self.wave("W2").get("phase") == "running", "W2 запущена диспетчером")
        self.wait_for(lambda: not self.alive("W1"), "окно W1 закрыто")
        self.assertIn("exit", self.actions("W1"))
        first = (self.run_dir / "W2" / "first-prompt.md").read_text(encoding="utf-8")
        self.assertIn("NEXT-W2", first)

        # W2: DONE → гейт → ожидание → мердж человеком → итог цепочки
        self.wait_for(lambda: self.wave("W2").get("phase") == "awaiting_merge", "W2 ждёт мерджа")
        self.assertEqual(self.dispatcher_merges(), [])
        self.human_merge(2)
        chain = self.finish_chain()
        self.assertEqual(self.dispatcher_merges(), [])

        # работа до контрольной точки не повторена: part1.txt закоммичен один раз
        self.assertEqual(self.actions("W1").count("commit:part1.txt"), 1)
        self.assertEqual(self.actions("W1").count("commit:hello.py"), 1)
        files = _git("--git-dir", str(self.origin), "ls-tree", "--name-only", "main", env=self.env).split()
        self.assertEqual(sorted(files), ["README.md", "hello.py", "part1.txt"])
        self.assertIn("Перезапуски после /clear: 1", chain)
        self.assertIn("BLOCKED: Какой вариант выбрать", chain)


class TestChainAutomerge(_E2E):
    """(б) automerge=true: черновик переводится в ready, ровно один `gh pr merge` на волну, обе смержены."""
    automerge = True
    scenario = staticmethod(scenario_automerge)

    def test_chain(self):
        self.launch_w1()
        self.start_watch()
        self.finish_chain()
        merges = self.dispatcher_merges()
        self.assertEqual([m["argv"][2] for m in merges], ["1", "2"], merges)
        for m in merges:
            self.assertIn("--squash", m["argv"])
            self.assertIn("--match-head-commit", m["argv"])
            self.assertEqual(m["caller"], "")   # вызвал диспетчер, а не агент
        readies = [c for c in self.gh_calls() if c["argv"][:2] == ["pr", "ready"]]
        self.assertEqual([c["argv"][2] for c in readies], ["1"])
        self.assertIn("W1: PR #1 отправлен в мердж", self.events())
        self.assertIn("W2: PR #2 отправлен в мердж", self.events())


if __name__ == "__main__":
    unittest.main()
