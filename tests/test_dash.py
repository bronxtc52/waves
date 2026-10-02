"""dash.py: волна в фазе starting (cwd ещё None) и подсчёт коммитов только ветки волны."""
import importlib
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest

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


if __name__ == "__main__":
    unittest.main()
