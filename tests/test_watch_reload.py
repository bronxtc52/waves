"""watch(): на ходу перечитываются только пороги, поля прогона остаются прежними (Codex P1 на PR #1)."""
import contextlib
import io
import tempfile
import pathlib
import unittest
from unittest import mock

import helpers  # noqa: F401  (кладёт scripts/ в sys.path)
from helpers import good, write_json

import wab


class TestWatchReload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.path = write_json(self.dir, good())
        self.cfg = wab.load_waves(str(self.path))

    def run_watch(self, edit):
        """Первый тик правит waves.json, второй запоминает cfg и останавливает watch."""
        seen = []

        def fake_tick(cfg, st, waves_json=None):
            seen.append(cfg)
            if len(seen) == 1:
                data = good()
                edit(data)
                write_json(self.dir, data)
                return True
            return False

        with mock.patch.object(wab, "tick", side_effect=fake_tick), \
                mock.patch.object(wab.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            wab.watch(self.cfg, str(self.path))
        return seen[-1]

    def log(self):
        p = self.cfg["run_dir"] / "events.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_run_id_change_keeps_run(self):
        def edit(data):
            data["run_id"] = "other-run"
            data["idle_minutes"] = 99
        cfg = self.run_watch(edit)
        self.assertEqual(cfg["run_id"], self.cfg["run_id"])
        self.assertEqual(cfg["run_dir"], self.cfg["run_dir"])
        self.assertEqual(cfg["idle_minutes"], 99)  # порог применён
        self.assertIn("run_id", self.log())
        self.assertIn("перезапуск", self.log())

    def test_chain_and_waves_change_ignored(self):
        def edit(data):
            data["chain"] = "otherchain"
            data["waves"] = data["waves"][:1]
            data["tick_seconds"] = 5
        cfg = self.run_watch(edit)
        self.assertEqual(cfg["chain"], self.cfg["chain"])
        self.assertEqual(cfg["tmux_prefix"], self.cfg["tmux_prefix"])
        self.assertEqual(cfg["waves"], self.cfg["waves"])
        self.assertEqual(cfg["tick_seconds"], 5)

    def test_thresholds_only_no_warning(self):
        cfg = self.run_watch(lambda data: data.update(ctx_limit=123_000))
        self.assertEqual(cfg["ctx_limit"], 123_000)
        self.assertNotIn("перезапуск", self.log())


if __name__ == "__main__":
    unittest.main()
