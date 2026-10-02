"""Общие помощники тестов: хороший конфиг и запись waves.json во временный каталог."""
import copy
import json
import pathlib
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
