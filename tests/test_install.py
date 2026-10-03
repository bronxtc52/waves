"""W5: install.sh — проверки зависимостей и симлинк скилла.

install.sh запускается настоящим bash с временным HOME и PATH только из подставных бинарей:
claude, tmux, gh, git, python3 (обёртка над настоящим python3 с PYTHONPATH на каталог с подставным
пакетом rich) и нужных скрипту утилит (ln, mkdir, rm, readlink, cat). Тест не зависит от того,
стоят ли на машине настоящие rich, tmux, gh или claude.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
BASH = shutil.which("bash")
TOOLS = ("ln", "mkdir", "rm", "readlink", "cat")


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


@unittest.skipUnless(BASH, "нужен bash")
class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="wab-install-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            real = shutil.which(tool)
            self.assertIsNotNone(real, f"нет утилиты {tool}")
            os.symlink(real, self.bin / tool)
        self.pkgs = {}
        for name, body in (("with_rich", ""), ("no_rich", "raise ImportError('подставной: rich нет')\n")):
            d = self.tmp / name / "rich"
            d.mkdir(parents=True)
            (d / "__init__.py").write_text(body, encoding="utf-8")
            self.pkgs[name] = d.parent
        self.set_all_ok()

    # ---------- подставные бинари ----------
    def set_all_ok(self):
        _script(self.bin / "claude", "exit 0\n")
        self.set_tmux("tmux 3.4")
        self.set_gh(authorized=True)
        _script(self.bin / "git", 'echo "git version 2.43.0"\n')
        self.set_python(rich=True)

    def set_tmux(self, version):
        _script(self.bin / "tmux", f'[ "$1" = "-V" ] && echo "{version}"\nexit 0\n')

    def set_gh(self, authorized):
        _script(self.bin / "gh", f'[ "$1 $2" = "auth status" ] && exit {0 if authorized else 1}\nexit 0\n')

    def set_python(self, rich):
        pkg = self.pkgs["with_rich" if rich else "no_rich"]
        env = shutil.which("env")
        _script(self.bin / "python3", f'exec "{env}" PYTHONPATH="{pkg}" PYTHONNOUSERSITE=1 "{sys.executable}" "$@"\n')

    def remove(self, name):
        (self.bin / name).unlink()

    def run_install(self):
        env = {"HOME": str(self.home), "PATH": str(self.bin), "LANG": os.environ.get("LANG", "C.UTF-8")}
        return subprocess.run([BASH, str(INSTALL)], env=env, capture_output=True, text=True, timeout=60)

    @property
    def link(self):
        return self.home / ".claude" / "skills" / "wave-autobot"

    def assert_fails(self, needle):
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(needle, r.stderr)
        self.assertFalse(self.link.exists() or self.link.is_symlink(), "при нехватке симлинк не ставится")
        return r

    # ---------- проверки зависимостей ----------
    def test_syntax(self):
        r = subprocess.run([BASH, "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_rich(self):
        self.set_python(rich=False)
        r = self.assert_fails("нет модуля rich")
        self.assertIn("pip install", r.stderr)

    def test_no_tmux(self):
        self.remove("tmux")
        r = self.assert_fails("не найден tmux")
        self.assertIn("brew install tmux", r.stderr)

    def test_old_tmux(self):
        self.set_tmux("tmux 3.1c")
        self.assert_fails("tmux слишком старый")

    def test_tmux_master_ok(self):
        self.set_tmux("tmux master")
        self.assertEqual(self.run_install().returncode, 0)

    def test_gh_not_authorized(self):
        self.set_gh(authorized=False)
        self.assert_fails("gh auth login")

    def test_no_gh_claude_git(self):
        for name in ("gh", "claude", "git"):
            self.remove(name)
        r = self.assert_fails("не найден gh")
        self.assertIn("не найден claude", r.stderr)
        self.assertIn("не найден git", r.stderr)

    def test_old_git(self):
        _script(self.bin / "git", 'echo "git version 2.30.1"\n')
        self.assert_fails("git слишком старый")

    def test_no_python(self):
        self.remove("python3")
        self.assert_fails("не найден python3")

    def test_old_python(self):
        _script(self.bin / "python3", 'case "$*" in *version_info*) exit 1;; esac\necho "Python 3.8.10"\nexit 0\n')
        self.assert_fails("python3 старше 3.10")

    def test_all_missing_reported_at_once(self):
        self.remove("tmux")
        self.set_gh(authorized=False)
        self.set_python(rich=False)
        r = self.assert_fails("не найден tmux")
        self.assertIn("gh auth login", r.stderr)
        self.assertIn("нет модуля rich", r.stderr)

    # ---------- симлинк ----------
    def test_symlink_created_and_idempotent(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(pathlib.Path(os.readlink(self.link)), ROOT)
        self.assertIn("keys.tmux", r.stdout)
        self.assertIn(f'source-file "{ROOT}/keys.tmux"', r.stdout)
        self.assertIn("README.md", r.stdout)
        r2 = self.run_install()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("уже установлен", r2.stdout)
        self.assertEqual(pathlib.Path(os.readlink(self.link)), ROOT)

    def test_symlink_elsewhere_replaced(self):
        other = self.tmp / "old-copy"
        other.mkdir()
        (other / "keep.txt").write_text("x", encoding="utf-8")
        self.link.parent.mkdir(parents=True)
        os.symlink(other, self.link)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("заменён", r.stdout)
        self.assertIn(str(other), r.stdout)
        self.assertEqual(pathlib.Path(os.readlink(self.link)), ROOT)
        self.assertTrue((other / "keep.txt").exists(), "прежний каталог не трогается")

    def test_dangling_symlink_replaced(self):
        self.link.parent.mkdir(parents=True)
        os.symlink(self.tmp / "nowhere", self.link)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pathlib.Path(os.readlink(self.link)), ROOT)

    def test_existing_directory_untouched(self):
        self.link.mkdir(parents=True)
        (self.link / "mine.txt").write_text("мои файлы", encoding="utf-8")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("не симлинк", r.stderr)
        self.assertFalse(self.link.is_symlink())
        self.assertEqual((self.link / "mine.txt").read_text(encoding="utf-8"), "мои файлы")

    def test_existing_file_untouched(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("файл", encoding="utf-8")
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.link.read_text(encoding="utf-8"), "файл")

    def test_run_from_other_directory_via_relative_path(self):
        """Симлинк — на абсолютный путь каталога скрипта, даже если скрипт вызван относительным путём."""
        env = {"HOME": str(self.home), "PATH": str(self.bin)}
        r = subprocess.run([BASH, "./install.sh"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        target = pathlib.Path(os.readlink(self.link))
        self.assertTrue(target.is_absolute())
        self.assertEqual(target, ROOT)


if __name__ == "__main__":
    unittest.main()
