"""Подставной `claude` для офлайн-сквозного теста (tests/test_e2e_offline.py).

`claude -p …` (probe_model) — печатает «ok», код 0.
Интерактивный режим (окно волны в tmux) — ведёт себя как TUI Claude Code в том, что видит диспетчер:
  * печатает маркер готовности «? for shortcuts» и включает bracketed paste;
  * читает ввод сам (без канонического режима терминала): вставка — одно сообщение, Enter — конец;
  * пишет журнал JSONL в $HOME/.claude/projects/<cwd, где всё кроме [A-Za-z0-9] заменено на «-»>/<id>.jsonl:
    первое сообщение — user с текстом (метка волны), usage — в записях assistant;
  * `/clear` — новая сессия с новым uuid и новым журналом (как настоящий claude), контекст сброшен;
  * `/exit` — выход.
Что делать — сценарий шагов из ctl/scenario.json по волне ($WAB_WAVE). Действия пишутся в
ctl/actions-<волна>.log, нарушения сценария — в ctl/errors.log (тест требует пустой errors.log).

Запуск: fake_claude.py --fake-ctl <каталог> <аргументы claude>.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid

READY = "? for shortcuts"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


def ctl_path(ctl, name):
    return pathlib.Path(ctl) / name


def log(ctl, name, line):
    with open(ctl_path(ctl, name), "a", encoding="utf-8") as f:
        f.write(line.replace("\n", " ⏎ ") + "\n")


class Agent:
    def __init__(self, ctl, argv):
        self.ctl = ctl
        self.cfg = json.loads(ctl_path(ctl, "config.json").read_text(encoding="utf-8"))
        self.wave = os.environ.get("WAB_WAVE", "?")
        self.wdir = pathlib.Path(os.environ.get("WAB_DIR", "."))
        self.sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else str(uuid.uuid4())
        self.tokens = 100
        self.buf = b""
        self.cwd = os.getcwd()
        self.steps = json.loads(ctl_path(ctl, "scenario.json").read_text(encoding="utf-8")).get(self.wave, [])

    # ---------- журнал ----------
    def journal(self):
        d = pathlib.Path(self.cfg["home"]) / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.sid}.jsonl"

    def append(self, entry):
        entry.update(sessionId=self.sid, uuid=str(uuid.uuid4()), isSidechain=False, cwd=self.cwd)
        with open(self.journal(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def assistant(self):
        self.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                                                      "usage": {"input_tokens": self.tokens,
                                                                "cache_creation_input_tokens": 0,
                                                                "cache_read_input_tokens": 0}}})

    # ---------- ввод ----------
    def _raw_lines(self):
        """Сообщения из терминала: вставка (bracketed paste) целиком, Enter вне вставки — конец."""
        in_paste, cur = False, b""
        while True:
            i = 0
            while i < len(self.buf):
                rest = self.buf[i:]
                if rest[:1] == b"\x1b" and len(rest) < len(PASTE_START) and \
                        (PASTE_START.startswith(rest) or PASTE_END.startswith(rest)):
                    break  # escape-последовательность пришла не целиком — дочитать
                if rest.startswith(PASTE_START):
                    in_paste, i = True, i + len(PASTE_START)
                    continue
                if rest.startswith(PASTE_END):
                    in_paste, i = False, i + len(PASTE_END)
                    continue
                ch = rest[:1]
                i += 1
                if ch in (b"\r", b"\n"):
                    if in_paste:
                        cur += b"\n"
                    else:
                        self.buf = self.buf[i:]
                        i = 0
                        text = cur.decode("utf-8", "replace").strip()
                        cur = b""
                        if text:
                            yield text
                else:
                    cur += ch
            self.buf = self.buf[i:]
            data = os.read(0, 65536)
            if not data:
                sys.exit(0)
            self.buf += data

    def next_message(self):
        """Следующее настоящее сообщение; /clear и /exit обрабатываются здесь же."""
        for text in self._lines:
            if text == "/exit":
                log(self.ctl, f"actions-{self.wave}.log", "exit")
                sys.exit(0)
            if text == "/clear":
                self.sid, self.tokens = str(uuid.uuid4()), 100
                log(self.ctl, f"actions-{self.wave}.log", f"clear:{self.sid}")
                sys.stdout.write("\x1b[2J\x1b[H(no content)\n" + READY + "\n")
                sys.stdout.flush()
                continue
            self.append({"type": "user", "message": {"role": "user", "content": text}})
            self.assistant()
            log(self.ctl, f"actions-{self.wave}.log", f"msg:{text[:80]}")
            return text

    # ---------- шаги сценария ----------
    def git(self, *args):
        r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout.strip()

    def error(self, text):
        log(self.ctl, "errors.log", f"{self.wave}: {text}")

    def step(self, s):
        op = s["op"]
        act = f"actions-{self.wave}.log"
        if op == "msg":
            before = self.sid
            text = self.next_message()
            if s.get("prefix") and not text.startswith(s["prefix"]):
                self.error(f"ждали сообщение с «{s['prefix']}», пришло «{text[:200]}»")
            if s.get("contains") and s["contains"] not in text:
                self.error(f"в сообщении нет «{s['contains']}»: «{text[:200]}»")
            if s.get("new_session") and self.sid == before:
                self.error("продолжение пришло без /clear (сессия та же)")
        elif op == "status":
            (self.wdir / "status").write_text(s["text"] + "\n", encoding="utf-8")
            log(self.ctl, act, f"status:{s['text']}")
        elif op == "usage":
            self.tokens = s["tokens"]
            self.assistant()
            log(self.ctl, act, f"usage:{self.tokens}")
        elif op == "wait_file":
            while not ctl_path(self.ctl, s["name"]).exists():
                time.sleep(0.1)
        elif op == "commit":
            pathlib.Path(self.cwd, s["file"]).write_text(s["content"], encoding="utf-8")
            self.git("add", s["file"])
            self.git("commit", "-q", "-m", s.get("message", f"{self.wave}: {s['file']}"))
            branch = self.git("symbolic-ref", "--short", "HEAD")
            self.git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
            log(self.ctl, act, f"commit:{s['file']}")
        elif op == "pr_create":
            branch = self.git("symbolic-ref", "--short", "HEAD")
            argv = ["gh", "pr", "create", "--base", self.cfg["base"], "--head", branch,
                    "--title", f"{self.wave}: e2e", "--body", "офлайн-сквозной тест"]
            if s.get("draft"):
                argv.append("--draft")
            r = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "FAKE_GH_CALLER": "agent"})
            if r.returncode != 0:
                self.error(f"gh pr create: {r.stderr.strip()}")
            log(self.ctl, act, f"pr:{r.stdout.strip()}")
        elif op == "handoff":
            (self.wdir / "handoff.md").write_text(
                f"# Handoff — {self.wave}\n## Сделано\n- {s['done']}\n## Следующий шаг\n{s['next']}\n",
                encoding="utf-8")
            log(self.ctl, act, "handoff")
        elif op == "read_handoff":
            text = (self.wdir / "handoff.md").read_text(encoding="utf-8")
            nxt = text.split("## Следующий шаг", 1)[-1].strip().splitlines()[0]
            log(self.ctl, act, f"handoff-next:{nxt}")
        elif op == "finish":
            (self.wdir / "result.md").write_text(f"# Итог {self.wave}\nPR открыт, проверки зелёные.\n",
                                                 encoding="utf-8")
            if s.get("next_prompt"):
                (self.wdir / "next-prompt.md").write_text(s["next_prompt"] + "\n", encoding="utf-8")
            (self.wdir / "status").write_text("DONE\n", encoding="utf-8")
            log(self.ctl, act, "status:DONE")
        else:
            self.error(f"неизвестный шаг {op}")

    def run(self):
        import termios
        try:
            attrs = termios.tcgetattr(0)
            attrs[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
            attrs[3] &= ~(termios.ICANON | termios.ECHO)
            attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
            termios.tcsetattr(0, termios.TCSANOW, attrs)
        except termios.error:
            pass
        sys.stdout.write("\x1b[?2004h" + "fake claude (e2e)\n" + READY + "\n")
        sys.stdout.flush()
        self._lines = self._raw_lines()
        log(self.ctl, f"actions-{self.wave}.log", f"start:{self.sid}")
        for s in self.steps:
            try:
                self.step(s)
            except Exception as e:  # noqa: BLE001 — любое падение шага видно тесту в errors.log
                self.error(f"шаг {s}: {e!r}")
        while True:  # сценарий кончился: дальше только /clear и /exit, остальное — нарушение
            text = self.next_message()
            self.error(f"неожиданное сообщение после конца сценария: «{text[:200]}»")


def main(argv):
    if len(argv) < 2 or argv[0] != "--fake-ctl":
        print("fake_claude: нужен --fake-ctl <каталог>", file=sys.stderr)
        return 2
    ctl, args = argv[1], argv[2:]
    if "-p" in args:
        log(ctl, "claude-probe.log", " ".join(args[:4]))
        print("ok")
        return 0
    Agent(ctl, args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
