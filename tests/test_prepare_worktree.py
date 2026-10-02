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
        self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), "wab/demo/2026-10-02/W1")
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

    def test_reuse_on_other_branch_refused(self):
        path = wab.prepare_worktree(self.cfg, "W1")
        git("checkout", "-b", "foreign", cwd=path)
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        msg = str(cm.exception)
        self.assertIn(path, msg)
        self.assertIn("wab/demo/2026-10-02/W1", msg)
        self.assertIn("foreign", msg)
        self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), "foreign")

    def test_reuse_on_detached_head_refused(self):
        path = wab.prepare_worktree(self.cfg, "W1")
        git("checkout", "--detach", cwd=path)
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        msg = str(cm.exception)
        self.assertIn(path, msg)
        self.assertIn("wab/demo/2026-10-02/W1", msg)
        self.assertIn("detached HEAD", msg)
        self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")

    def test_reuse_on_expected_branch_ok(self):
        path = wab.prepare_worktree(self.cfg, "W1")
        sha = self._commit_in(path, "mine.txt")
        self.assertEqual(wab.prepare_worktree(self.cfg, "W1"), path)
        self.assertEqual(git("-C", path, "rev-parse", "HEAD"), sha)

    def _commit_in(self, path, name):
        (pathlib.Path(path) / name).write_text("x\n")
        git("add", ".", cwd=path)
        git("commit", "-m", name, cwd=path)
        return git("-C", path, "rev-parse", "HEAD")

    def test_restore_after_rm_of_directory(self):
        import shutil
        path = wab.prepare_worktree(self.cfg, "W1")
        sha = self._commit_in(path, "mine.txt")
        shutil.rmtree(path)
        again = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(again, path)
        self.assertEqual(git("-C", again, "rev-parse", "--abbrev-ref", "HEAD"), "wab/demo/2026-10-02/W1")
        self.assertEqual(git("-C", again, "rev-parse", "HEAD"), sha)
        self.assertTrue((pathlib.Path(again) / "mine.txt").exists())

    def test_restore_after_worktree_remove(self):
        path = wab.prepare_worktree(self.cfg, "W1")
        sha = self._commit_in(path, "mine.txt")
        git("worktree", "remove", path, cwd=self.checkout)
        again = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(git("-C", again, "rev-parse", "HEAD"), sha)
        self.assertEqual(git("-C", again, "rev-parse", "--abbrev-ref", "HEAD"), "wab/demo/2026-10-02/W1")

    def test_new_run_id_gets_new_branch_from_origin(self):
        first = wab.prepare_worktree(self.cfg, "W1")
        self._commit_in(first, "mine.txt")
        cfg2 = dict(self.cfg, run_id="r2", run_dir=self.root / "runs" / "r2")
        second = wab.prepare_worktree(cfg2, "W1")
        self.assertNotEqual(first, second)
        self.assertEqual(git("-C", second, "rev-parse", "--abbrev-ref", "HEAD"), "wab/demo/r2/W1")
        self.assertEqual(git("-C", second, "rev-parse", "HEAD"),
                         git("-C", str(self.checkout), "rev-parse", "origin/main"))

    def test_branch_busy_in_another_worktree(self):
        git("checkout", "-b", "wab/demo/2026-10-02/W1", cwd=self.checkout)
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("wab/demo/2026-10-02/W1", str(cm.exception))
        self.assertIn("занята", str(cm.exception))

    def test_bad_base_branch(self):
        self.cfg["base_branch"] = "nope"
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("nope", str(cm.exception))


class TestBaseRefspec(unittest.TestCase):
    setUp = TestPrepareWorktree.setUp
    _restore_env = TestPrepareWorktree._restore_env

    """Явный refspec для fetch: база берётся из origin даже при --single-branch клоне."""

    def _develop(self):
        git("checkout", "-b", "develop", cwd=self.checkout)
        (self.checkout / "d.txt").write_text("d\n")
        git("add", ".", cwd=self.checkout)
        git("commit", "-m", "dev", cwd=self.checkout)
        git("push", "origin", "develop", cwd=self.checkout)
        sha = git("rev-parse", "HEAD", cwd=self.checkout)
        git("checkout", "main", cwd=self.checkout)
        return sha

    def test_single_branch_clone_other_base(self):
        sha = self._develop()
        clone = self.root / "single"
        git("clone", "--single-branch", "--branch", "main", str(self.origin), str(clone))
        self.cfg.update(checkout=str(clone), base_branch="develop")
        path = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(git("-C", path, "rev-parse", "HEAD"), sha)

    def test_regular_clone(self):
        clone = self.root / "regular"
        git("clone", str(self.origin), str(clone))
        main_sha = git("rev-parse", "HEAD", cwd=clone)
        self.cfg.update(checkout=str(clone))
        path = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(git("-C", path, "rev-parse", "HEAD"), main_sha)

    def test_missing_base_branch(self):
        self.cfg.update(base_branch="nope")
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("nope", str(cm.exception))
        self.assertFalse((self.cfg["run_dir"] / "worktrees" / "W1").exists())
        branches = git("branch", "--list", "wab/*", cwd=self.checkout)
        self.assertEqual(branches, "")


if __name__ == "__main__":
    unittest.main()
