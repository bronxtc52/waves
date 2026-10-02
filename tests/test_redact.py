"""redact(): поля-секреты с именем в кавычках (JSON, Python-словари) и без них.

Значение скрывается целиком, имя поля и разделитель остаются видны. Проверка — и самой
функцией, и по путям наружу: event() (events.log и экран) и dash.screen_text()."""
import contextlib
import io
import tempfile
import pathlib
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
