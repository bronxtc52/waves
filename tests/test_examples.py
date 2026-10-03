"""W5: examples/ валидны, README ведёт по существующим файлам, версия согласована (SKILL.md, CHANGELOG, README)."""
import hashlib
import json
import os
import pathlib
import re
import shlex
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

    def test_prompt_relies_on_wave_context_not_plan_files(self):
        """Волна идёт в worktree от origin/<base>: незакоммиченных waves.md/waves.json там нет."""
        prompt = (EX / "prompt-W1.md").read_text(encoding="utf-8")
        self.assertIn("Контекст волны", prompt)
        self.assertNotIn("waves.md", prompt)
        self.assertNotIn("waves.json", prompt)
        # раздел, на который ссылается промпт, действительно есть в системной инструкции волны
        import wab
        cfg = waves_config.load_waves(EX / "waves.json")
        text = wab.system_prompt_text(cfg, cfg["waves"][0], "/wdir", "/cwd", cfg["roles"])
        self.assertIn("## Контекст волны", text)
        self.assertIn(cfg["waves"][0]["check"], text)


class TestReadme(unittest.TestCase):
    def test_warning_on_top(self):
        head = "\n".join(README.splitlines()[:15])
        self.assertIn("--permission-mode auto", head)
        self.assertIn("automerge", head)
        self.assertNotIn("🚧", README)

    def test_warning_is_honest_about_isolation(self):
        """worktree и feature-ветка — порядок работы, не ограничение прав: README не обещает изоляцию."""
        head = "\n".join(README.splitlines()[:25])
        self.assertIn("feature-ветка", head)
        self.assertIn("не ограничение прав", head)
        self.assertIn("защиту базовой ветки на GitHub", head)
        self.assertNotIn("сама не пишет", README)
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("только feature-ветки", skill)
        self.assertIn("не\nограничение прав", skill)

    def test_wab_open_example_keeps_quotes_inside_value(self):
        """keys.tmux исполняет @wab_open через sh -c: кавычки вокруг путей должны попасть в значение."""
        lines = [l for l in README.splitlines() if l.startswith("tmux set -g @wab_open ")]
        self.assertEqual(len(lines), 1)
        argv = shlex.split(lines[0])  # так команду разберёт оболочка пользователя
        self.assertEqual(argv[:4], ["tmux", "set", "-g", "@wab_open"])
        self.assertEqual(len(argv), 5)
        value = argv[4]
        self.assertTrue(value.startswith('"') and value.endswith('"'), value)
        self.assertNotIn("$", value)
        # sh -c разберёт значение в ровно два слова: wab-open и waves.json, даже с пробелами в путях
        spaced = value.replace("/путь/к/репозиторию", "/мой репо").replace("/home/вы", "/home/my user")
        words = shlex.split(spaced)
        self.assertEqual(len(words), 2, words)
        self.assertTrue(words[0].endswith("/scripts/wab-open"))
        self.assertTrue(words[1].endswith("/waves.json"))

    def test_blocked_step_uses_switch_client_inside_tmux(self):
        """На шаге BLOCKED пользователь уже в tmux: attach изнутри tmux отказывает."""
        self.assertIn("switch-client -t =wab-hello-w1", README)
        self.assertIn("switch-client -t =wab`", README)
        self.assertIn("Ctrl-b L", README)
        self.assertRegex(README, r"вне tmux — `tmux attach -t =wab-hello-w1`")

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

    def test_every_wab_block_sets_wab(self):
        """Новая сессия и окно tmux не наследуют переменную оболочки: каждый блок с "$WAB" задаёт её сам."""
        blocks = re.findall(r"```[a-z]*\n(.*?)```", README, re.S)
        using = [b for b in blocks if '"$WAB"' in b]
        self.assertGreaterEqual(len(using), 4)
        for b in using:
            first = b.index('"$WAB"')
            self.assertIn('WAB="$HOME/.claude/skills/wave-autobot"\n', b[:first], b)
        self.assertIn("worktree", README)
        self.assertRegex(README, r"prompt-W1\.md` лежат только в основной\s+рабочей копии")

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
