"""W5: examples/ валидны, README ведёт по существующим файлам, версия согласована (SKILL.md, CHANGELOG, README)."""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)

import waves_config

ROOT = pathlib.Path(__file__).resolve().parent.parent
EX = ROOT / "examples"
README = (ROOT / "README.md").read_text(encoding="utf-8")
VERSION = "0.1.0"


class TestExamples(unittest.TestCase):
    def test_validate_passes(self):
        r = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "wab.py"), "validate",
                            str(EX / "waves.json")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((EX / "runs").exists(), "validate не создаёт каталогов")

    def test_plan_sha_matches(self):
        cfg = waves_config.load_waves(EX / "waves.json")
        self.assertEqual(cfg["plan_sha256"], hashlib.sha256((EX / "waves.md").read_bytes()).hexdigest())
        self.assertEqual(cfg["plan_path"], EX / "waves.md")

    def test_two_waves_match_plan(self):
        cfg = waves_config.load_waves(EX / "waves.json")
        self.assertEqual([w["id"] for w in cfg["waves"]], ["W1", "W2"])
        self.assertEqual(cfg["waves"][1]["depends_on"], ["W1"])
        self.assertFalse(cfg["automerge"], "пример не включает автомердж")
        plan = (EX / "waves.md").read_text(encoding="utf-8")
        for w in cfg["waves"]:
            self.assertIn(f"## Волна {w['id']} — ", plan)
        self.assertTrue((EX / "prompt-W1.md").read_text(encoding="utf-8").strip())


class TestReadme(unittest.TestCase):
    def test_warning_on_top(self):
        head = "\n".join(README.splitlines()[:15])
        self.assertIn("--permission-mode auto", head)
        self.assertIn("automerge", head)
        self.assertIn("feature-ветке", head)
        self.assertNotIn("🚧", README)

    def test_quick_start_commands_reference_existing_files(self):
        self.assertIn("./install.sh", README)
        self.assertTrue(os.access(ROOT / "install.sh", os.X_OK))
        refs = set(re.findall(r'"\$WAB"/([\w./-]+)', README))
        for need in ("examples/waves.md", "examples/waves.json", "examples/prompt-W1.md",
                     "scripts/wab.py", "scripts/dash.py"):
            self.assertIn(need, refs)
        for ref in refs:
            self.assertTrue((ROOT / ref).exists(), f"README ссылается на несуществующий {ref}")
        for cmd in ("wab.py validate waves.json", "wab.py models waves.json",
                    "wab.py launch waves.json W1 prompt-W1.md", "wab.py watch waves.json", "dash.py waves.json"):
            self.assertIn(cmd, README)
        self.assertIn("keys.tmux", README)
        self.assertTrue((ROOT / "keys.tmux").is_file())
        self.assertTrue((ROOT / "scripts" / "wab-open").is_file())

    def test_fields_table_matches_config(self):
        rows = {m.group(1): line for line in README.splitlines()
                for m in [re.match(r"\| `(\w+)` \|", line)] if m}
        for key in waves_config.REQUIRED:
            self.assertIn(key, rows)
            self.assertIn("| да |", rows[key])
        for key, default in waves_config.OPTIONAL_DEFAULTS.items():
            self.assertIn(key, rows)
            shown = default if isinstance(default, str) else json.dumps(default)
            self.assertIn(f"| `{shown}` |", rows[key], key)
        self.assertIn("roles", rows)
        for role, model in waves_config.ROLE_DEFAULTS.items():
            self.assertRegex(README, rf"\| `{role}` \|[^\n]*\| `{model}` \|")
        for key in (*waves_config.WAVE_REQUIRED, "depends_on"):
            self.assertIn(key, rows)

    def test_version_consistent(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        front = skill.split("\n---\n", 1)[0]
        self.assertIn(f"\nversion: {VERSION}", front)
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        first = re.search(r"^## (.+)$", changelog, re.M).group(1)
        self.assertTrue(first.startswith(f"{VERSION} — "), first)
        self.assertNotIn("## Не выпущено", changelog)
        self.assertIn(f"**{VERSION}**", README)
        self.assertIn("CHANGELOG.md", README)


if __name__ == "__main__":
    unittest.main()
