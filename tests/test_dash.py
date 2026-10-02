"""dash.py: волна в фазе starting (cwd ещё None), подсчёт коммитов только ветки волны и
вычистка экрана волны (захват с -J, redact по всему тексту до обрезки)."""
import importlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)

RICH_NAMES = {"rich": (), "rich.box": ("SIMPLE_HEAVY", "ROUNDED"), "rich.align": ("Align",),
              "rich.console": ("Group",), "rich.layout": ("Layout",), "rich.live": ("Live",),
              "rich.panel": ("Panel",), "rich.table": ("Table",), "rich.text": ("Text",)}


def import_dash():
    """Импорт dash.py; без установленного rich — с заглушками его модулей (рендер тут не проверяется)."""
    try:
        return importlib.import_module("dash")
    except ModuleNotFoundError as e:
        if not (e.name or "").startswith("rich"):
            raise
    for name, attrs in RICH_NAMES.items():
        m = types.ModuleType(name)
        for a in attrs:
            setattr(m, a, type(a, (), {}))
        sys.modules[name] = m
    sys.modules["rich"].box = sys.modules["rich.box"]
    return importlib.import_module("dash")


dash = import_dash()


def git(cwd, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


class TestStartingWave(unittest.TestCase):
    """launch резервирует волну с cwd=None: кадр дашборда не должен падать."""

    def test_commits_since_none_cwd(self):
        self.assertEqual(dash.commits_since(None, 1_700_000_000), 0)

    def test_transcript_stats_none_cwd(self):
        self.assertEqual(dash.transcript_stats(None),
                         {"turns": 0, "tools": 0, "out": 0, "read": 0, "agents": 0})


class TestCommitsSince(unittest.TestCase):
    """Считаются только коммиты ветки worktree волны после старта, не всех refs репозитория."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.started = 1_700_000_000
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "до старта", when=self.started - 100)
        self.wt = self.root / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "wave", str(self.wt))

    def test_only_wave_branch_counted(self):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "чужая ветка", when=self.started + 10)
        git(self.wt, "commit", "-q", "--allow-empty", "-m", "волна 1", when=self.started + 20)
        git(self.wt, "commit", "-q", "--allow-empty", "-m", "волна 2", when=self.started + 30)
        self.assertEqual(dash.commits_since(str(self.wt), self.started), 2)

    def test_commit_before_start_not_counted(self):
        self.assertEqual(dash.commits_since(str(self.wt), self.started), 0)


TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # ghp_ + 36
PASSWORD = "очень-длинное-значение-пароля-1234567890"


class FakeRich:
    """Заглушка Text/Panel/Group/Align: хранит аргументы, чтобы достать текст кадра."""

    def __init__(self, *args, **kw):
        self.parts = [a for a in args]

    def append(self, other, style=None):
        self.parts.append(other)
        return self

    @classmethod
    def center(cls, *args, **kw):
        return cls(*args)


def flatten(obj):
    if isinstance(obj, str):
        return obj
    if isinstance(obj, FakeRich):
        return "\n".join(flatten(p) for p in obj.parts)
    return ""


class Capture:
    """Мок wab.sh для capture-pane: с -J — склеенный текст, без -J — мягко перенесённый."""

    def __init__(self, joined, wrapped):
        self.joined, self.wrapped = joined, wrapped
        self.calls = []

    def __call__(self, *args, check=True, **kw):
        self.calls.append(list(args))
        out = self.joined if "-J" in args else self.wrapped
        return subprocess.CompletedProcess(args, 0, out, "")


def soft_wrap(line, width):
    return "\n".join(line[i:i + width] for i in range(0, len(line), width))


class TestCurrentPanelRedact(unittest.TestCase):
    """Секрет, мягко перенесённый терминалом, не должен проступать в кадре дашборда."""

    def frame(self, joined, wrapped):
        cap = Capture(joined, wrapped)
        cfg = {"ctx_limit": 1000}
        st = {"current": "W1", "waves": {"W1": {"tmux": "wab-c-w1", "tokens": 10, "ctx_hist": [1, 2]}}}
        patches = [mock.patch.object(dash, n, FakeRich) for n in ("Text", "Panel", "Group", "Align")]
        patches += [mock.patch.object(dash.wab, "sh", cap),
                    mock.patch.object(dash.wab, "read", lambda p: "RUNNING"),
                    mock.patch.object(dash.wab, "wave_path", lambda c, w: pathlib.Path("/nonexistent")),
                    mock.patch.object(dash, "box", types.SimpleNamespace(ROUNDED=None))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return flatten(dash.current_panel(cfg, st)), cap

    def test_soft_wrapped_secrets_hidden(self):
        lines = ["работаю над задачей", f"export GH={TOKEN} дальше", f"password={PASSWORD}", "готово"]
        joined = "\n".join(lines) + "\n"
        wrapped = "\n".join(soft_wrap(l, 20) for l in lines) + "\n"
        text, cap = self.frame(joined, wrapped)
        captures = [c for c in cap.calls if "capture-pane" in c]
        self.assertTrue(captures and all("-J" in c for c in captures), cap.calls)
        for secret in (TOKEN, PASSWORD):
            for i in range(0, len(secret) - 6, 6):
                self.assertNotIn(secret[i:i + 8], text, f"фрагмент секрета в кадре: {text!r}")
        self.assertIn("работаю над задачей", text)
        self.assertIn("password=[скрыто]", text)
        self.assertIn("готово", text)

    def test_long_pane_not_truncated_before_split(self):
        """Текст панели длиннее REDACT_LIMIT: последние строки на месте, обрезка — только по ширине."""
        lines = [f"строка {i:02d} " + "x" * 50 for i in range(40)] + ["хвост " + "я" * 300]
        joined = "\n".join(lines)
        self.assertGreater(len(joined), dash.wab.REDACT_LIMIT * 3)
        text, _ = self.frame(joined, joined)
        self.assertIn("строка 39", text)
        self.assertNotIn("строка 26", text)  # видно только 14 последних непустых
        tail = [l for l in text.splitlines() if l.startswith("хвост")]
        self.assertEqual(len(tail), 1)
        self.assertEqual(len(tail[0]), 150)
        self.assertNotIn(" …", text)


class TestPaneTextJoin(unittest.TestCase):
    """-J только по запросу: wait_ready и маркеры работают на прежнем захвате."""

    def test_join_flag(self):
        cap = Capture("склеено", "разрезано")
        with mock.patch.object(dash.wab, "sh", cap):
            self.assertEqual(dash.wab.pane_text("wab-c-w1", join=True), "склеено")
            self.assertEqual(dash.wab.pane_text("wab-c-w1"), "разрезано")
        self.assertEqual(cap.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=wab-c-w1:"],
                                     ["tmux", "capture-pane", "-p", "-t", "=wab-c-w1:"]])

    def test_wait_ready_markers(self):
        for marker in dash.wab.READY_MARKERS:
            cap = Capture("", "экран\n" + marker + "\n")
            with mock.patch.object(dash.wab, "sh", cap), mock.patch.object(dash.wab.time, "sleep"):
                self.assertTrue(dash.wab.wait_ready("wab-c-w1", timeout=5))
            self.assertTrue(all("-J" not in c for c in cap.calls))

    def test_wait_ready_trust_then_ready(self):
        seq = iter([dash.wab.TRUST_MARKERS[0], dash.wab.READY_MARKERS[0]])
        calls = []

        def sh(*args, check=True, **kw):
            calls.append(list(args))
            out = next(seq) if "capture-pane" in args else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        with mock.patch.object(dash.wab, "sh", sh), mock.patch.object(dash.wab.time, "sleep"):
            self.assertTrue(dash.wab.wait_ready("wab-c-w1", timeout=5))
        self.assertIn(["tmux", "send-keys", "-t", "=wab-c-w1:", "Down"], calls)


@unittest.skipUnless(os.environ.get("WAB_LIVE_TMUX") == "1",
                     "живой тест tmux опционален: задайте WAB_LIVE_TMUX=1")
@unittest.skipUnless(shutil.which("tmux"), "нужен tmux")
class TestCaptureJoinLive(unittest.TestCase):
    """Живая проверка на приватном сокете: в окне шириной 40 длинный токен переносится,
    а capture-pane -J отдаёт его одной строкой. Сервер tmux по умолчанию не трогаем."""

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, "-f", os.devnull, *args], capture_output=True,
                              text=True, env={k: v for k, v in os.environ.items() if k != "TMUX"})

    def setUp(self):
        self.sock = f"wabjoin{os.getpid()}"
        self.addCleanup(self.tmux, "kill-server")

    def test_joined_capture(self):
        r = self.tmux("new-session", "-d", "-s", "j", "-x", "40", "-y", "20",
                      f"printf '%s\\n' 'GH={TOKEN}'; sleep 30")
        self.assertEqual(r.returncode, 0, r.stderr)
        plain = joined = ""
        for _ in range(50):
            plain = self.tmux("capture-pane", "-p", "-t", "=j:").stdout
            joined = self.tmux("capture-pane", "-p", "-J", "-t", "=j:").stdout
            if f"GH={TOKEN}" in joined:
                break
            time.sleep(0.1)
        self.assertNotIn(TOKEN, plain)  # без -J токен разрезан переносом
        self.assertIn(f"GH={TOKEN}", joined.splitlines())
        self.assertNotIn(TOKEN[:12], dash.wab.redact(joined, limit=10_000))


if __name__ == "__main__":
    unittest.main()
