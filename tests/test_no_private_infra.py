"""В коде и README не должно быть следов приватной инфраструктуры (docs/ и этот файл исключены)."""
import os
import pathlib
import re
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


_RE = re.compile(PATTERN.encode(), re.IGNORECASE)
_SELF = "tests/test_no_private_infra.py"


def scan(root):
    """Вхождения запрещённых слов в отслеживаемых файлах root, кроме docs/ и этого теста.

    Не зависит от платформы: файлы читаются байтами, поиск — регэкспом по bytes (git grep
    на macOS по-разному ведёт себя с NUL-байтами). Результат: `путь:номер_строки:фрагмент`.
    """
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"], env=ENV,
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files: {r.stderr.decode('utf-8', 'replace').strip()}")
    hits = []
    for raw in filter(None, r.stdout.split(b"\0")):
        rel = os.fsdecode(raw)
        if rel == _SELF or rel == "docs" or rel.startswith("docs/"):
            continue
        path = os.path.join(str(root), rel)
        try:
            if os.path.islink(path):
                data = os.fsencode(os.readlink(path))  # симлинк не разыменовываем
            elif os.path.isfile(path):
                with open(path, "rb") as fh:
                    data = fh.read()
            else:
                continue  # отсутствует в рабочем дереве, каталог или gitlink
        except OSError:
            continue
        for n, line in enumerate(data.split(b"\n"), 1):
            if _RE.search(line):
                hits.append(f"{rel}:{n}:{line.decode('utf-8', 'replace')}")
    return hits


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

    def _add(self, name, data):
        copy_tree(self.copy)
        f = self.copy / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return f

    def test_word_after_nul_found(self):
        self._add("scripts/nul.bin", b"\x00\x00Server-Watchdog\x00")
        _git(self.copy, "add", "-A")
        self.assertTrue(any(h.startswith("scripts/nul.bin:1:") for h in scan(self.copy)))

    def test_word_in_very_long_line_found(self):
        self._add("scripts/long.txt", b"a" * 200_000 + b" telegram " + b"b" * 100_000 + b"\n")
        _git(self.copy, "add", "-A")
        self.assertTrue(any(h.startswith("scripts/long.txt:1:") for h in scan(self.copy)))

    def test_word_without_trailing_newline_found(self):
        self._add("scripts/tail.txt", b"ok\nline two keyvault")
        _git(self.copy, "add", "-A")
        self.assertTrue(any(h.startswith("scripts/tail.txt:2:") for h in scan(self.copy)))

    def test_untracked_and_pyc_not_found(self):
        copy_tree(self.copy)
        (self.copy / "__pycache__").mkdir()
        (self.copy / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00telegram\x00")
        (self.copy / ".gitignore").write_text("__pycache__/\n")
        _git(self.copy, "add", "-A")  # .pyc игнорируется и не попадает в индекс
        (self.copy / "draft.txt").write_text("telegram\n")  # неотслеживаемый
        self.assertEqual(scan(self.copy), [])

    def test_symlink_and_missing_file(self):
        copy_tree(self.copy)
        (self.copy / "outside.txt").write_text("fine\n")
        os.symlink("telegram-target", self.copy / "lnk")
        (self.copy / "gone.txt").write_text("x\n")
        _git(self.copy, "add", "-A")
        (self.copy / "gone.txt").unlink()
        hits = scan(self.copy)
        self.assertTrue(any(h.startswith("lnk:1:") for h in hits), hits)
        self.assertFalse(any(h.startswith("gone.txt") for h in hits), hits)


if __name__ == "__main__":
    unittest.main()
