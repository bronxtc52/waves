"""redact(): поля-секреты с именем в кавычках (JSON, Python-словари) и без них.

Значение скрывается целиком, имя поля и разделитель остаются видны. Проверка — и самой
функцией, и по путям наружу: event() (events.log и экран) и dash.screen_text()."""
import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)
from helpers import good, write_json
from test_dash import import_dash

import wab

# (текст, что должно исчезнуть, что должно остаться)
CASES = [
    ('{"password": "Hunter2Secret"}', "Hunter2Secret", '{"password": [скрыто]}'),
    ('{"password": "a"}', '"a"', '"password": [скрыто]'),
    ("{'token': 'abc'}", "abc", "'token': [скрыто]"),
    ('{"api_key":"xyz"}', "xyz", '"api_key":[скрыто]'),
    ('password="x y z"', "x y", "password=[скрыто]"),
    ('"password" : "Hunter2Secret"', "Hunter2Secret", '"password" : [скрыто]'),
    ('{"Password":"Hunter2Secret"}', "Hunter2Secret", '"Password":[скрыто]'),
    ('{"SECRET": "s3 cr3t", "user": "bob"}', "s3 cr3t", '"user": "bob"'),
    ('{"access_token": "abc123", "n": 1}', "abc123", '"access_token": [скрыто], "n": 1'),
    ('{"token": 12345, "ok": true}', "12345", '"token": [скрыто], "ok": true'),
    ('{"token": null}', "null", '"token": [скрыто]}'),
    ("password: Hunter2Secret", "Hunter2Secret", "password: [скрыто]"),
    ("PASSWORD=Hunter2Secret more", "Hunter2Secret", "PASSWORD=[скрыто] more"),
    ('{\n  "user": "bob",\n  "password": "Hunter2 Secret",\n  "x": 1\n}', "Hunter2",
     '"password": [скрыто],\n  "x": 1'),
    ("dict(token='a', b=2)", "'a'", "token=[скрыто], b=2"),
    ("[password=abc]", "abc", "[password=[скрыто]]"),
    # без кавычек значение скрывается до пробела целиком, запятые внутри — тоже;
    # с конца отбрасываются только завершающие разделители JSON и скобки
    ("password=ab,cd", "cd", "password=[скрыто]"),
    ("pwd=a,b,c end", "b,c", "pwd=[скрыто] end"),
    ('{"token": abc},', "abc", '{"token": [скрыто]},'),
    ("password=ab, x", "ab", "password=[скрыто], x"),
]

# обычный текст событий: слово «token» без разделителя не трогается
PLAIN = [
    "W1: контекст 120000 >= 150000, запрошена контрольная точка",
    "token limit reached, tokens: 12",
    'the "token" word in quotes',
    "secret handshake done",
]


class TestRedactQuotedFields(unittest.TestCase):
    def test_cases(self):
        for text, gone, kept in CASES:
            with self.subTest(text=text):
                out = wab.redact(text, limit=10_000)
                self.assertNotIn(gone, out)
                self.assertIn(kept, out)

    def test_plain_text_untouched(self):
        for text in PLAIN:
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text, limit=10_000), text)


class TestRedactPaths(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = wab.load_waves(str(write_json(pathlib.Path(tmp.name), good())))

    def test_event_log_and_stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            wab.event(self.cfg, 'W1: BLOCKED: конфиг {"password": "Hunter2Secret", "token": "a"}')
        log = (self.cfg["run_dir"] / "events.log").read_text(encoding="utf-8")
        for where in (out.getvalue(), log):
            self.assertNotIn("Hunter2Secret", where)
            self.assertNotIn('"a"', where)
            self.assertIn('"password": [скрыто]', where)
            self.assertIn('"token": [скрыто]', where)

    def test_dash_screen_text(self):
        dash = import_dash()
        pane = 'работаю\n{"password": "Hunter2Secret"}\n{\'api_key\': \'xyz\'}\nготово\n'
        with mock.patch.object(dash.wab, "pane_text", return_value=pane):
            text = dash.screen_text("wab-W1")
        self.assertNotIn("Hunter2Secret", text)
        self.assertNotIn("xyz", text)
        self.assertIn('"password": [скрыто]', text)
        self.assertIn("'api_key': [скрыто]", text)
        self.assertIn("готово", text)


if __name__ == "__main__":
    unittest.main()


# ---------- W3: классы пропусков (фикстура), контрольные фразы, время ----------

import json
import time

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "redact_classes.json").read_text(encoding="utf-8"))


class TestRedactClasses(unittest.TestCase):
    def test_every_example_hidden(self):
        for cls, examples in FIXTURE.items():
            if cls == "plain":
                continue
            for ex in examples:
                with self.subTest(cls=cls, text=ex["text"]):
                    out = wab.redact(ex["text"], limit=10_000)
                    self.assertNotIn(ex["secret"], out)
                    self.assertIn("[скрыто]", out)

    def test_plain_phrases_untouched(self):
        for text in FIXTURE["plain"]:
            with self.subTest(text=text):
                self.assertEqual(wab.redact(text, limit=10_000), text)

    def test_names_stay_visible_and_neighbours_survive(self):
        out = wab.redact(r'{\"user\": \"bob\", \"password\": \"Hunter2Secret\", \"n\": 1}', limit=10_000)
        self.assertIn(r'\"user\": \"bob\"', out)
        self.assertIn("password", out)
        self.assertIn(r'\"n\": 1', out)
        self.assertEqual(wab.redact("mysql --user bob --password Hunter2Secret --host db", limit=10_000),
                         "mysql --user bob --password [скрыто] --host db")


class TestRedactLinear(unittest.TestCase):
    """Шаблоны не должны вести себя квадратично: до W3 300 КБ без «@» занимали ~115 с."""

    def timed(self, text):
        """Время redact() в дочернем процессе: регулярное выражение не прерывается сигналом, поэтому
        регрессия должна падать по timeout подпроцесса, а не вешать прогон тестов."""
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import wab; text = sys.stdin.read(); "
                "t = time.monotonic(); wab.redact(text, limit=10_000); print(time.monotonic() - t)")
        scripts = str(pathlib.Path(helpers.__file__).resolve().parent.parent / "scripts")
        try:
            r = subprocess.run([sys.executable, "-B", "-c", code, scripts], input=text, capture_output=True,
                               text=True, timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("redact не уложился в 30 с на входе 300 КБ (квадратичный шаблон?)")
        self.assertEqual(r.returncode, 0, r.stderr)
        return float(r.stdout.strip())

    def test_300kb_without_at(self):
        self.assertLess(self.timed("word.another-one_x " * 16_000), 1.0)
        self.assertLess(self.timed("a.b-c+d" * 43_000), 1.0)

    def test_300kb_single_letter_string(self):
        self.assertLess(self.timed("a" * 300_000), 1.0)
        self.assertLess(self.timed("abcdefghij" * 30_000), 1.0)

    def test_300kb_many_at_and_prefix_repeats(self):
        self.assertLess(self.timed("a@" * 150_000), 1.0)
        self.assertLess(self.timed("sk-" * 100_000), 1.0)
        self.assertLess(self.timed("a_" * 150_000), 1.0)
        self.assertLess(self.timed("x-" * 150_000 + "password"), 1.0)
