"""Загрузчик waves.json v1: по тесту на каждое правило ТЗ."""
import pathlib
import tempfile
import unittest

import helpers
from helpers import good, write_json

import waves_config
from waves_config import ConfigError, load_waves


class LoaderBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def load(self, data):
        return load_waves(write_json(self.dir, data))

    def fails(self, data, *fragments):
        with self.assertRaises(ConfigError) as cm:
            self.load(data)
        msg = str(cm.exception)
        self.assertIn("waves.json", msg)
        for f in fragments:
            self.assertIn(f, msg)
        return msg


class TestDefaults(LoaderBase):
    def test_config_error_is_value_error(self):
        self.assertTrue(issubclass(ConfigError, ValueError))

    def test_defaults_applied(self):
        c = self.load(good())
        self.assertEqual(c["base_branch"], "main")
        self.assertEqual(c["fallback_model"], "opus")
        self.assertIs(c["automerge"], False)
        self.assertEqual(c["ctx_limit"], 300000)
        self.assertEqual(c["idle_minutes"], 12)
        self.assertEqual(c["tick_seconds"], 60)
        self.assertIsNone(c["plan_sha256"])
        self.assertEqual(c["roles"], {"architect": "fable", "coder": "opus", "tester": "sonnet",
                                      "reviewer": "fable", "reader": "haiku"})
        self.assertEqual(c["waves"][0]["depends_on"], [])

    def test_partial_roles_filled(self):
        d = good()
        d["roles"] = {"coder": "sonnet"}
        c = self.load(d)
        self.assertEqual(c["roles"]["coder"], "sonnet")
        self.assertEqual(c["roles"]["architect"], "fable")

    def test_derived_fields(self):
        c = self.load(good())
        self.assertEqual(c["run_dir"], self.dir / "runs" / "2026-10-02")
        self.assertIsInstance(c["run_dir"], pathlib.Path)
        self.assertEqual(c["tmux_prefix"], "wab-demo-")
        self.assertFalse((self.dir / "runs").exists(), "загрузчик не создаёт каталоги")

    def test_explicit_values_kept(self):
        d = good()
        d.update(base_branch="dev", fallback_model="sonnet", automerge=True, ctx_limit=5,
                 idle_minutes=2, tick_seconds=3, plan_sha256="a" * 64)
        c = self.load(d)
        self.assertEqual((c["base_branch"], c["fallback_model"], c["automerge"], c["ctx_limit"],
                          c["idle_minutes"], c["tick_seconds"], c["plan_sha256"]),
                         ("dev", "sonnet", True, 5, 2, 3, "a" * 64))

    def test_input_not_mutated_and_new_dict(self):
        d = good()
        c = self.load(d)
        self.assertNotIn("run_dir", d)
        self.assertIsNot(c, d)


class TestFileLevel(LoaderBase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError) as cm:
            load_waves(self.dir / "nope.json")
        self.assertIn("nope.json", str(cm.exception))

    def test_invalid_json(self):
        self.fails("{not json", "waves.json")

    def test_top_level_not_object(self):
        self.fails("[1]", "объект")

    def test_duplicate_key_top(self):
        self.fails('{"chain": "a", "chain": "b"}', "дубликат", "chain")

    def test_duplicate_key_nested(self):
        raw = ('{"chain":"a","run_id":"r","repo":"o/n","checkout":"/x","waves":[{"id":"W1","id":"W2",'
               '"title":"t","goal":"g","done_when":["d"],"check":"c"}]}')
        self.fails(raw, "дубликат", "id")


class TestFileReading(LoaderBase):
    def test_not_utf8(self):
        p = self.dir / "waves.json"
        p.write_bytes(b"\xff\xfe{}")
        with self.assertRaises(ConfigError) as cm:
            load_waves(p)
        self.assertIn("waves.json", str(cm.exception))
        self.assertIn("UTF-8", str(cm.exception))

    def test_directory_instead_of_file(self):
        (self.dir / "waves.json").mkdir()
        with self.assertRaises(ConfigError):
            load_waves(self.dir / "waves.json")

    def test_unreadable_file(self):
        import os
        p = write_json(self.dir, good())
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        if os.access(p, os.R_OK):
            self.skipTest("права не действуют (root)")
        with self.assertRaises(ConfigError):
            load_waves(p)


class TestStrictValues(LoaderBase):
    def test_repo_strict(self):
        for bad in ("./name", "owner/..", "../x", "a/.", "a/b\n", "a b/c", "a/b/"):
            d = good()
            d["repo"] = bad
            self.fails(d, "repo")

    def test_base_branch_rules(self):
        for bad in ("-x", "--upload-pack=x", "a b", "a..b", "a\x00b", "a\tb", "a\x7fb", "/a", "a/",
                    "a//b", "a.lock", "a@{b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b",
                    "@", ".a", "a/.b", "a."):
            d = good()
            d["base_branch"] = bad
            self.fails(d, "base_branch")
        for ok in ("main", "release/1.2", "feat_x-1"):
            d = good()
            d["base_branch"] = ok
            self.assertEqual(self.load(d)["base_branch"], ok)

    def test_nul_in_strings(self):
        for key in ("chain", "run_id", "checkout", "fallback_model"):
            d = good()
            d[key] = d.get(key, "x") + "\x00x"
            self.fails(d, key)
        d = good()
        d["waves"][0]["title"] = "a\x00b"
        self.fails(d, "waves[0].title")
        d = good()
        d["roles"] = {"coder": "a\x00b"}
        self.fails(d, "roles.coder")

    def test_depends_on_duplicate(self):
        d = good()
        d["waves"][1]["depends_on"] = ["W1", "W1"]
        self.fails(d, "waves[1].depends_on", "W1", "дубликат")


class TestTopLevel(LoaderBase):
    def test_extra_key(self):
        d = good()
        d["bogus"] = 1
        self.fails(d, "лишний ключ bogus")

    def test_missing_required(self):
        for key in ("chain", "run_id", "repo", "checkout", "waves"):
            d = good()
            del d[key]
            self.fails(d, key)

    def test_chain_charset(self):
        for bad in ("a b", "", "..", "."):
            d = good()
            d["chain"] = bad
            self.fails(d, "chain")

    def test_run_id_charset(self):
        for bad in ("a/b", "", "..."):
            d = good()
            d["run_id"] = bad
            self.fails(d, "run_id")

    def test_repo_format(self):
        for bad in ("name", "a/b/c", "/b", "a/"):
            d = good()
            d["repo"] = bad
            self.fails(d, "repo")

    def test_checkout_absolute(self):
        d = good()
        d["checkout"] = "relative/dir"
        self.fails(d, "checkout", "абсолют")

    def test_waves_nonempty_list(self):
        for bad in ([], {}, "W1"):
            d = good()
            d["waves"] = bad
            self.fails(d, "waves")

    def test_wrong_types_name_expected_type(self):
        cases = {"chain": 5, "repo": 5, "checkout": 5, "base_branch": 5, "fallback_model": 5,
                 "automerge": "yes", "ctx_limit": "5", "idle_minutes": 1.5, "tick_seconds": [],
                 "plan_sha256": 5, "roles": []}
        for key, val in cases.items():
            d = good()
            d[key] = val
            msg = self.fails(d, key)
            self.assertIn("ожидается", msg, key)

    def test_bool_is_not_int(self):
        for key in ("ctx_limit", "idle_minutes", "tick_seconds"):
            d = good()
            d[key] = True
            self.fails(d, key, "int")

    def test_int_is_not_bool(self):
        d = good()
        d["automerge"] = 1
        self.fails(d, "automerge", "bool")

    def test_positive_ints(self):
        for key in ("ctx_limit", "idle_minutes", "tick_seconds"):
            for bad in (0, -1):
                d = good()
                d[key] = bad
                self.fails(d, key, "> 0")

    def test_plan_sha256_format(self):
        for bad in ("A" * 64, "a" * 63, "g" * 64):
            d = good()
            d["plan_sha256"] = bad
            self.fails(d, "plan_sha256")
        d = good()
        d["plan_sha256"] = None
        self.assertIsNone(self.load(d)["plan_sha256"])


class TestRoles(LoaderBase):
    def test_extra_role(self):
        d = good()
        d["roles"] = {"janitor": "opus"}
        self.fails(d, "roles", "лишний ключ janitor")

    def test_role_value_nonempty_str(self):
        for bad in ("", 5, None):
            d = good()
            d["roles"] = {"coder": bad}
            self.fails(d, "roles.coder")


class TestWaves(LoaderBase):
    def test_extra_wave_key(self):
        d = good()
        d["waves"][0]["bogus"] = 1
        self.fails(d, "waves[0]", "лишний ключ bogus")

    def test_missing_wave_keys(self):
        for key in ("id", "title", "goal", "done_when", "check"):
            d = good()
            del d["waves"][1][key]
            self.fails(d, "waves[1]", key)

    def test_wave_not_object(self):
        d = good()
        d["waves"][0] = "W1"
        self.fails(d, "waves[0]")

    def test_wave_id_charset(self):
        d = good()
        d["waves"][0]["id"] = "W 1"
        self.fails(d, "waves[0].id")

    def test_empty_strings(self):
        for key in ("title", "goal", "check"):
            d = good()
            d["waves"][0][key] = ""
            self.fails(d, "waves[0]." + key)

    def test_done_when(self):
        for bad in ([], [""], [1], "x"):
            d = good()
            d["waves"][0]["done_when"] = bad
            self.fails(d, "waves[0].done_when")

    def test_depends_on_type(self):
        d = good()
        d["waves"][1]["depends_on"] = "W1"
        self.fails(d, "waves[1].depends_on")
        d["waves"][1]["depends_on"] = [1]
        self.fails(d, "waves[1].depends_on")

    def test_duplicate_id(self):
        d = good()
        d["waves"][1]["id"] = "W1"
        d["waves"][1]["depends_on"] = []
        self.fails(d, "waves[1].id", "дубликат", "W1")

    def test_depends_on_self(self):
        d = good()
        d["waves"][1]["depends_on"] = ["W2"]
        self.fails(d, "waves[1].depends_on", "W2")

    def test_depends_on_unknown(self):
        d = good()
        d["waves"][1]["depends_on"] = ["W9"]
        self.fails(d, "waves[1].depends_on", "W9", "неизвестн")

    def test_depends_on_forward(self):
        d = good()
        d["waves"][0]["depends_on"] = ["W2"]
        msg = self.fails(d, "waves[0].depends_on", "«W2»", "объявлена ниже")
        self.assertIn("зависимость вперёд запрещена", msg)

    def test_error_message_format(self):
        d = good()
        d["waves"].append({"id": "W3", "title": "t", "goal": "g", "done_when": ["d"], "check": "c",
                           "depends_on": ["W4"]})
        d["waves"].append({"id": "W4", "title": "t", "goal": "g", "done_when": ["d"], "check": "c"})
        msg = self.fails(d)
        self.assertEqual(msg, "waves.json: waves[2].depends_on: волна «W4» объявлена ниже — "
                              "зависимость вперёд запрещена")


if __name__ == "__main__":
    unittest.main()
