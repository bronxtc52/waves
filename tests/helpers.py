"""Общие помощники тестов: хороший конфиг и запись waves.json во временный каталог."""
import contextlib
import copy
import json
import pathlib
import signal
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

GOOD = {
    "chain": "demo",
    "run_id": "2026-10-02",
    "repo": "owner/name",
    "checkout": "/tmp/some/checkout",
    "waves": [
        {"id": "W1", "title": "Каркас", "goal": "Сделать каркас", "done_when": ["CI зелёный"],
         "check": "python3 -m unittest"},
        {"id": "W2", "title": "Протокол", "goal": "Написать протокол", "done_when": ["готово"],
         "check": "true", "depends_on": ["W1"]},
    ],
}


def good():
    return copy.deepcopy(GOOD)


def write_json(directory, data, name="waves.json"):
    p = pathlib.Path(directory) / name
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


class _Patches:
    """Несколько mock.patch как один объект с start/stop (как у одиночного патча)."""

    def __init__(self, *patches):
        self.patches = patches

    def start(self):
        for p in self.patches:
            p.start()

    def stop(self):
        for p in reversed(self.patches):
            p.stop()


def stub_ensure_roles(wab):
    """Старые тесты launch не проверяют модели и версию tmux: ensure_roles и require_tmux подменены
    (claude -p и tmux -V не вызываются)."""
    from unittest import mock
    return _Patches(
        mock.patch.object(wab, "ensure_roles", side_effect=lambda cfg, refresh=False: (dict(cfg["roles"]), {})),
        mock.patch.object(wab, "require_tmux", return_value=None))


@contextlib.contextmanager
def deadline(seconds):
    """Ограничение времени теста (годится и как декоратор). SIGALRM (Linux и macOS): зависший системный вызов превращается в провал теста."""
    def boom(signum, frame):
        raise AssertionError(f"не уложился в {seconds} с (повисло)")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
