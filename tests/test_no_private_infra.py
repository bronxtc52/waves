"""В коде и README не должно быть следов приватной инфраструктуры (docs/ и этот файл исключены)."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATTERN = "kv-bronxtc|keyvault|key vault|cc-autonomy|telegram|server-watchdog|admission"
ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
           GIT_CONFIG_NOSYSTEM="1")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com",
                           *args], env=ENV, capture_output=True, encoding="utf-8", errors="replace")


def scan(root):
    """Вхождения запрещённых слов в отслеживаемых файлах root, кроме docs/ и этого теста."""
    r = _git(root, "grep", "-niaE", PATTERN, "--", ".", ":(exclude)docs",
             ":(exclude)tests/test_no_private_infra.py")
    if r.returncode == 1:
        return []
    if r.returncode != 0:
        raise RuntimeError(f"git grep: {r.stderr.strip()}")
    return r.stdout.splitlines()


def copy_tree(dst, src=ROOT):
    """Копия отслеживаемых файлов репозитория src в новый git-репозиторий dst."""
    files = _git(src, "ls-files", "-z", "--cached").stdout.split("\0")
    for rel in filter(None, files):
        f = pathlib.Path(src) / rel
        if f.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst / rel)
    assert _git(dst, "init", "-b", "main").returncode == 0
    assert _git(dst, "add", "-A").returncode == 0


class TestNoPrivateInfra(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.copy = pathlib.Path(self._tmp.name) / "repo"
        self.copy.mkdir()

    def test_repository_is_clean(self):
        # новые файлы должны быть хотя бы добавлены в индекс (git add -N), иначе git grep их не видит
        self.assertEqual(scan(ROOT), [])

    def test_leak_in_scripts_detected(self):
        copy_tree(self.copy)
        (self.copy / "scripts").mkdir(exist_ok=True)
        (self.copy / "scripts" / "leak.py").write_text("X = 'Telegram'\n", encoding="utf-8")
        _git(self.copy, "add", "scripts/leak.py")
        hits = scan(self.copy)
        self.assertTrue(hits)
        self.assertTrue(any("scripts/leak.py" in h for h in hits), hits)

    def test_binary_file_detected(self):
        copy_tree(self.copy)
        (self.copy / "scripts").mkdir(exist_ok=True)
        (self.copy / "scripts" / "blob.bin").write_bytes(b"\x00\x01\xffserver-watchdog\x00\x02")
        _git(self.copy, "add", "scripts/blob.bin")
        hits = scan(self.copy)
        self.assertTrue(any("scripts/blob.bin" in h for h in hits), hits)

    def test_copy_tree_skips_untracked(self):
        src = pathlib.Path(self._tmp.name) / "src"
        src.mkdir()
        _git(src, "init", "-b", "main")
        (src / "a.txt").write_text("ok\n")
        _git(src, "add", "a.txt")
        (src / "draft.txt").write_text("telegram\n")  # неотслеживаемый черновик
        copy_tree(self.copy, src)
        self.assertTrue((self.copy / "a.txt").exists())
        self.assertFalse((self.copy / "draft.txt").exists())
        self.assertEqual(scan(self.copy), [])

    def test_every_word_matches(self):
        copy_tree(self.copy)
        words = ["kv-bronxtc", "keyvault", "key vault", "cc-autonomy", "telegram", "server-watchdog",
                 "admission", "KeyVault"]
        for i, w in enumerate(words):
            (self.copy / f"w{i}.txt").write_text(f"x {w} y\n", encoding="utf-8")
        _git(self.copy, "add", "-A")
        hits = scan(self.copy)
        for i in range(len(words)):
            self.assertTrue(any(h.startswith(f"w{i}.txt") for h in hits), (words[i], hits))

    def test_docs_are_exempt(self):
        copy_tree(self.copy)
        (self.copy / "docs").mkdir(exist_ok=True)
        (self.copy / "docs" / "note.md").write_text("telegram, keyvault\n", encoding="utf-8")
        _git(self.copy, "add", "-A")
        self.assertEqual(scan(self.copy), [])


if __name__ == "__main__":
    unittest.main()
