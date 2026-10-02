"""prepare_worktree на временных git-репозиториях (локальный bare как origin). tmux не нужен."""
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

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

    def test_recreated_empty_dir_restored(self):
        """Каталог worktree удалён и создан заново пустым: запись в git есть, но это не рабочая копия."""
        import shutil
        path = wab.prepare_worktree(self.cfg, "W1")
        sha = self._commit_in(path, "mine.txt")
        shutil.rmtree(path)
        pathlib.Path(path).mkdir()
        again = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(again, path)
        self.assertEqual(git("-C", again, "rev-parse", "--show-toplevel"), path)
        self.assertEqual(git("-C", again, "rev-parse", "--abbrev-ref", "HEAD"), "wab/demo/2026-10-02/W1")
        self.assertEqual(git("-C", again, "rev-parse", "HEAD"), sha)
        self.assertTrue((pathlib.Path(again) / "mine.txt").exists())

    def test_recreated_nonempty_dir_refused_and_kept(self):
        """Каталог создан заново и в нём чужие файлы: отказ, ничего не удаляется."""
        import shutil
        path = wab.prepare_worktree(self.cfg, "W1")
        self._commit_in(path, "mine.txt")
        shutil.rmtree(path)
        pathlib.Path(path).mkdir()
        user = pathlib.Path(path) / "user.txt"
        user.write_text("моё\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        msg = str(cm.exception)
        self.assertIn(path, msg)
        self.assertIn("вручную", msg)
        self.assertEqual(user.read_text(encoding="utf-8"), "моё\n")
        self.assertEqual(sorted(p.name for p in pathlib.Path(path).iterdir()), ["user.txt"])
        listing = git("worktree", "list", "--porcelain", cwd=self.checkout)
        self.assertIn(path, listing)

    def test_recreated_as_foreign_repo_same_branch_refused(self):
        """На месте worktree — отдельный `git init` с веткой того же имени: это чужой репозиторий, отказ."""
        import shutil
        path = wab.prepare_worktree(self.cfg, "W1")
        branch = "wab/demo/2026-10-02/W1"
        sha = self._commit_in(path, "mine.txt")
        shutil.rmtree(path)
        git("init", "-b", branch, path)
        foreign = pathlib.Path(path) / "foreign.txt"
        foreign.write_text("чужое\n", encoding="utf-8")
        git("add", ".", cwd=path)
        git("commit", "-m", "foreign", cwd=path)
        foreign_sha = git("-C", path, "rev-parse", "HEAD")
        with self.assertRaises(SystemExit) as cm:
            wab.prepare_worktree(self.cfg, "W1")
        msg = str(cm.exception)
        self.assertIn(path, msg)
        self.assertIn("вручную", msg)
        self.assertIn("чужой репозиторий", msg)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "чужое\n")
        self.assertEqual(sorted(p.name for p in pathlib.Path(path).iterdir()), [".git", "foreign.txt"])
        self.assertEqual(git("-C", path, "rev-parse", "HEAD"), foreign_sha)
        self.assertEqual(git("-C", path, "rev-parse", "--show-toplevel"), path)
        self.assertEqual(git("rev-parse", f"refs/heads/{branch}", cwd=self.checkout), sha)
        self.assertIn(path, git("worktree", "list", "--porcelain", cwd=self.checkout))

    def test_is_worktree_of_checks_common_dir(self):
        """_is_worktree_of принимает настоящий worktree и отвергает отдельный репозиторий с той же веткой."""
        path = wab.prepare_worktree(self.cfg, "W1")
        branch = "wab/demo/2026-10-02/W1"
        self.assertTrue(wab._is_worktree_of(pathlib.Path(path), branch, self.checkout))
        other = self.root / "other"
        git("init", "-b", branch, str(other))
        self.assertFalse(wab._is_worktree_of(other.resolve(), branch, self.checkout))

    def test_recreated_dir_inside_other_repo_restored(self):
        """Пустой каталог внутри чужого git-репозитория: show-toplevel даёт не wt, а внешний корень."""
        import shutil
        outer = self.root / "outer"
        git("init", "-b", "main", str(outer))
        self.cfg["run_dir"] = outer / "runs" / "r1"
        path = wab.prepare_worktree(self.cfg, "W1")
        sha = self._commit_in(path, "mine.txt")
        shutil.rmtree(path)
        pathlib.Path(path).mkdir()
        again = wab.prepare_worktree(self.cfg, "W1")
        self.assertEqual(git("-C", again, "rev-parse", "--show-toplevel"), path)
        self.assertEqual(git("-C", again, "rev-parse", "HEAD"), sha)

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


def _c_quote(path):
    """Как git с core.quotePath: путь со спецсимволами в кавычках, не-ASCII байты — восьмерично."""
    if all(0x20 <= b < 0x7f and b not in b'"\\' for b in path.encode()):
        return path
    out = []
    for b in path.encode():
        if b in b'"\\':
            out.append("\\" + chr(b))
        elif b == 0x0a:
            out.append("\\n")
        elif 0x20 <= b < 0x7f:
            out.append(chr(b))
        else:
            out.append("\\%03o" % b)
    return '"' + "".join(out) + '"'


SPECIAL = {
    "cyr": "волны прогон",
    "quote": 'run "q"',
    "backslash": "run\\back",
    "space": "run with space",
    "newline": "run\nline",
}


class TestSpecialPaths(unittest.TestCase):
    """run_dir со спецсимволами: повторный вызов переиспользует worktree, восстановление работает."""
    setUp = TestPrepareWorktree.setUp
    _restore_env = TestPrepareWorktree._restore_env
    _commit_in = TestPrepareWorktree._commit_in

    def _cfg(self, key):
        return dict(self.cfg, run_id=f"r-{key}", run_dir=self.root / SPECIAL[key] / "r")

    def _branch(self, key):
        return f"wab/demo/r-{key}/W1"

    def test_reuse_and_restore(self):
        import shutil
        for key in SPECIAL:
            with self.subTest(key=key):
                cfg = self._cfg(key)
                path = wab.prepare_worktree(cfg, "W1")
                self.assertEqual(pathlib.Path(path), (cfg["run_dir"] / "worktrees" / "W1").resolve())
                self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), self._branch(key))
                sha = self._commit_in(path, "mine.txt")
                self.assertEqual(wab.prepare_worktree(cfg, "W1"), path)
                self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), self._branch(key))
                shutil.rmtree(path)
                self.assertEqual(wab.prepare_worktree(cfg, "W1"), path)
                self.assertEqual(git("-C", path, "rev-parse", "--abbrev-ref", "HEAD"), self._branch(key))
                self.assertEqual(git("-C", path, "rev-parse", "HEAD"), sha)

    def test_git_quoting_paths_without_z(self):
        """Новые git берут путь в кавычки без -z (core.quotePath); с -z — никогда."""
        real = wab._git

        def fake(checkout, *args):
            r = real(checkout, *args)
            if args[:2] == ("worktree", "list") and "-z" not in args and r.returncode == 0:
                lines = [("worktree " + _c_quote(l[len("worktree "):])) if l.startswith("worktree ") else l
                         for l in r.stdout.split("\n")]
                r.stdout = "\n".join(lines)
            return r

        with mock.patch.object(wab, "_git", fake):
            for key in ("cyr", "quote", "backslash"):
                with self.subTest(key=key):
                    cfg = self._cfg(key)
                    path = wab.prepare_worktree(cfg, "W1")
                    self.assertEqual(wab.prepare_worktree(cfg, "W1"), path)


class TestOldGit(unittest.TestCase):
    """git < 2.36 не знает `worktree list -z`: понятная SystemExit с требованием версии."""
    setUp = TestPrepareWorktree.setUp
    _restore_env = TestPrepareWorktree._restore_env

    def _fake(self, version):
        real_git, real_sh = wab._git, wab.sh

        def fake_git(checkout, *args):
            if args[:2] == ("worktree", "list") and "-z" in args:
                return subprocess.CompletedProcess(args, 129, "", "error: unknown switch `z'\n")
            return real_git(checkout, *args)

        def fake_sh(*args, **kw):
            if args == ("git", "version"):
                return subprocess.CompletedProcess(args, 0, f"git version {version}\n", "")
            return real_sh(*args, **kw)
        return mock.patch.multiple(wab, _git=fake_git, sh=fake_sh)

    def test_old_git_refused_with_version(self):
        with self._fake("2.35.1"):
            with self.assertRaises(SystemExit) as cm:
                wab.prepare_worktree(self.cfg, "W1")
        msg = str(cm.exception)
        self.assertIn("2.36", msg)
        self.assertIn("2.35", msg)
        self.assertFalse((self.cfg["run_dir"] / "worktrees" / "W1").exists())

    def test_new_git_error_passed_through(self):
        with self._fake("2.43.0"):
            with self.assertRaises(SystemExit) as cm:
                wab.prepare_worktree(self.cfg, "W1")
        self.assertIn("git worktree list", str(cm.exception))
        self.assertNotIn("нужен git", str(cm.exception))


class TestParseWorktreesZ(unittest.TestCase):
    def test_records_and_newline_in_path(self):
        out = ("worktree /a\0HEAD 1\0branch refs/heads/main\0\0"
               "worktree /b\nc\0HEAD 2\0detached\0\0"
               "worktree /d \"e\"\\f\0HEAD 3\0branch refs/heads/x\0\0")
        got = wab._parse_worktrees_z(out)
        self.assertEqual(got, {pathlib.Path("/a"): "refs/heads/main",
                               pathlib.Path("/b\nc"): None,
                               pathlib.Path('/d "e"\\f'): "refs/heads/x"})


if __name__ == "__main__":
    unittest.main()
