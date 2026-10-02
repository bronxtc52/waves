"""prepare_worktree на временных git-репозиториях (локальный bare как origin). tmux не нужен."""
import os
import pathlib
import subprocess
import tempfile
import unittest

import helpers
from helpers import good

import wab

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
           GIT_CONFIG_NOSYSTEM="1")


def git(*args, cwd=None):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       cwd=cwd, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class TestPrepareWorktree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.origin = self.root / "origin.git"
        git("init", "--bare", "-b", "main", str(self.origin))
        self.checkout = self.root / "checkout"
        git("init", "-b", "main", str(self.checkout))
        (self.checkout / "a.txt").write_text("1\n")
        git("add", ".", cwd=self.checkout)
        git("commit", "-m", "init", cwd=self.checkout)
        git("remote", "add", "origin", str(self.origin), cwd=self.checkout)
        git("push", "origin", "main", cwd=self.checkout)
        self.cfg = good()
        self.cfg.update(checkout=str(self.checkout), base_branch="main",
                        run_dir=self.root / "runs" / "r1")
        self._old_env = {k: os.environ.get(k) for k in ENV}
        os.environ.update(ENV)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_not_git(self):
        d = self.root / "plain"
        d.mkdir()
        self.cfg["checkout"] = str(d)
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("git", str(cm.exception))

    def test_missing_dir(self):
        self.cfg["checkout"] = str(self.root / "absent")
        with self.assertRaises(SystemExit):
            wab.prepare_worktree(self.cfg, "W1")

    def test_subdir_is_not_root(self):
        sub = self.checkout / "sub"
        sub.mkdir()
        self.cfg["checkout"] = str(sub)
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("корень", str(cm.exception))

    def test_success_creates_worktree_on_origin_base(self):
        path = wab.prepare_worktree(self.cfg, "W1")
        self.assertIsInstance(path, str)
        expected = self.cfg["run_dir"] / "worktrees" / "W1"
        self.assertEqual(pathlib.Path(path).resolve(), expected.resolve())
        self.assertTrue((expected / "a.txt").exists())
        self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), "wab/demo/W1")
        self.assertEqual(git("-C", path, "rev-parse", "HEAD"),
                         git("-C", str(self.checkout), "rev-parse", "origin/main"))

    def test_uses_fresh_origin(self):
        other = self.root / "other"
        git("clone", str(self.origin), str(other))
        (other / "b.txt").write_text("2\n")
        git("add", ".", cwd=other)
        git("commit", "-m", "second", cwd=other)
        git("push", "origin", "main", cwd=other)
        path = wab.prepare_worktree(self.cfg, "W1")
        self.assertTrue((pathlib.Path(path) / "b.txt").exists())

    def test_second_call_reuses(self):
        first = wab.prepare_worktree(self.cfg, "W1")
        marker = pathlib.Path(first) / "work.txt"
        marker.write_text("wip\n")
        second = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(first, second)
        self.assertTrue(marker.exists())

    def test_bad_base_branch(self):
        self.cfg["base_branch"] = "nope"
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("nope", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
