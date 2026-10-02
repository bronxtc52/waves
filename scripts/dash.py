#!/usr/bin/env python3
"""wave-autobot, живой дашборд: `dash.py <waves.json>` (запускать внутри tmux, выход — Ctrl-C)."""
import json
import pathlib
import subprocess
import sys
import time

from rich import box
from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import wab  # noqa: E402
from waves_config import ConfigError  # noqa: E402

SPARK = "▁▂▃▄▅▆▇█"
STYLE = {  # phase/status -> (icon, colour, label)
    "pending": ("○", "grey50", "в очереди"),
    "STARTING": ("◌", "cyan", "старт"),
    "RUNNING": ("⚙", "bright_green", "работает"),
    "RESUMING": ("↻", "cyan", "свежая голова"),
    "checkpoint": ("💾", "yellow", "handoff"),
    "HANDOFF_READY": ("💾", "yellow", "handoff готов"),
    "BLOCKED": ("✋", "bold red", "ждёт тебя"),
    "DONE": ("✔", "bold green", "готово"),
    "dead": ("✖", "bold red", "окно закрыто"),
}


def fmt_dur(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}ч{m:02d}м" if h else f"{m}м{sec % 60:02d}с"


def ktok(n):
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n // 1000}k"


def bar(value, limit, width=24):
    frac = min(value / limit, 1.0) if limit else 0
    fill = int(frac * width)
    colour = "green" if frac < 0.6 else "yellow" if frac < 0.9 else "red"
    t = Text("█" * fill, style=colour)
    t.append("░" * (width - fill), style="grey30")
    t.append(f" {ktok(value)}/{ktok(limit)}", style="bold " + colour)
    return t


def spark(values, limit, width=60):
    vals = values[-width:]
    if not vals:
        return Text("—", style="grey50")
    out = Text()
    for v in vals:
        frac = min(v / limit, 1.0) if limit else 0
        colour = "green" if frac < 0.6 else "yellow" if frac < 0.9 else "red"
        out.append(SPARK[min(int(frac * len(SPARK)), len(SPARK) - 1)], style=colour)
    return out


_stats_cache = {}


def transcript_stats(cwd):
    """Сводка по всем транскриптам рабочей копии волны (по файлу на каждый перезапуск /clear)."""
    if not cwd:  # волна зарезервирована launch, worktree ещё не готов
        return {"turns": 0, "tools": 0, "out": 0, "read": 0, "agents": 0}
    d = wab.transcript_dir(cwd)
    found = list(d.glob("*.jsonl")) + list(d.glob("*/subagents/*.jsonl")) if d.exists() else []
    stamped = []
    for f in found:  # транскрипт может исчезнуть между glob() и stat()
        try:
            stamped.append((f, f.stat().st_mtime))
        except OSError:
            continue
    files = [f for f, _ in stamped]
    key = (cwd, tuple(sorted((str(f), m) for f, m in stamped)))
    if _stats_cache.get(cwd, (None,))[0] == key:
        return _stats_cache[cwd][1]
    s = {"turns": 0, "tools": 0, "out": 0, "read": 0, "agents": 0}
    for f in files:
        sub = "subagents" in f.parts
        s["agents"] += sub
        for raw in f.open("rb"):
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") != "assistant":
                continue
            msg = e.get("message") or {}
            u = msg.get("usage") or {}
            s["turns"] += not sub
            s["out"] += u.get("output_tokens", 0)
            s["read"] += u.get("cache_read_input_tokens", 0) + u.get("input_tokens", 0)
            s["tools"] += sum(1 for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use")
    _stats_cache[cwd] = (key, s)
    return s


def commits_since(cwd, started):
    """Коммиты ветки worktree волны с момента старта: refs у worktree общие, поэтому только HEAD."""
    if not cwd:  # волна зарезервирована launch, worktree ещё не готов
        return 0
    r = subprocess.run(["git", "-C", cwd, "log", "--oneline", f"--since=@{int(started)}", "HEAD"],
                       capture_output=True, text=True)
    return len(r.stdout.splitlines()) if r.returncode == 0 else 0


def wave_state(cfg, st, wave):
    w = st["waves"].get(wave)
    if not w:
        return "pending", w
    status = wab.redact(wab.read(wab.wave_path(cfg, wave) / "status"))
    if status.startswith("BLOCKED"):
        return "BLOCKED", w
    if w.get("phase") == "checkpoint" and status != "HANDOFF_READY":
        return "checkpoint", w
    if st.get("current") == wave and not wab.tmux_alive(w["tmux"]) and status != "DONE":
        return "dead", w
    return status or "STARTING", w


def pipeline(cfg, st):
    ids = wab.wave_ids(cfg)
    t = Text(justify="center")
    for i, wave in enumerate(ids):
        key, _ = wave_state(cfg, st, wave)
        icon, colour, _ = STYLE.get(key, ("?", "white", key))
        current = st.get("current") == wave
        t.append(f" {icon} {wave} ", style=f"{colour} {'reverse' if current else ''}")
        if i < len(ids) - 1:
            done = key == "DONE"
            t.append(" ━━▶ " if done else " ──▷ ", style="green" if done else "grey42")
    sub = Text("  ·  ".join(f"{w['id']}: {w['title']}" for w in cfg["waves"]),
               style="grey62", justify="center")
    return Group(t, sub)


def waves_table(cfg, st):
    tb = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold cyan")
    for col, j in (("Волна", "left"), ("Статус", "left"), ("Время", "right"), ("Контекст", "left"),
                   ("Пик", "right"), ("↻", "right"), ("Ходы", "right"), ("Tools", "right"),
                   ("Агенты", "right"), ("Вывод", "right"), ("Коммиты", "right")):
        tb.add_column(col, justify=j, no_wrap=True)
    now = time.time()
    for wave in wab.wave_ids(cfg):
        key, w = wave_state(cfg, st, wave)
        icon, colour, label = STYLE.get(key, ("?", "white", key))
        if not w:
            tb.add_row(Text(wave, style="grey50"), Text(f"{icon} {label}", style=colour), *[""] * 9)
            continue
        s = transcript_stats(w["cwd"])
        end = w.get("finished") or now
        tb.add_row(
            Text(wave, style="bold"), Text(f"{icon} {label}", style=colour),
            fmt_dur(end - w["started"]), bar(w.get("tokens", 0), cfg["ctx_limit"], 16),
            ktok(w.get("peak", 0)), str(w.get("restarts", 0)), str(s["turns"]), str(s["tools"]),
            str(s["agents"]), ktok(s["out"]), str(commits_since(w["cwd"], w["started"])))
    return tb


def screen_text(name, rows=14, width=150):
    """Хвост экрана волны для дашборда, вычищенный от секретов.

    Захват с -J: мягкий перенос терминала не режет секрет на куски, которые шаблоны не узнают.
    Вычистка — по всему тексту сразу и без лимита redact(); строки и ширина режутся после:
    обрывок секрета на границе среза шаблоном уже не узнался бы. Жёсткий перенос (настоящий
    перевод строки внутри секрета в самом выводе) так не склеить — это известное ограничение."""
    clean = wab.redact(wab.pane_text(name, join=True), limit=sys.maxsize)
    lines = [l for l in clean.splitlines() if l.strip()][-rows:]
    return "\n".join(l[:width] for l in lines)


def current_panel(cfg, st):
    wave = st.get("current")
    if not wave:
        return Panel(Align.center(Text("цепочка не запущена или завершена", style="grey50")),
                     title="Текущая волна", border_style="grey42")
    w = st["waves"][wave]
    status = wab.redact(wab.read(wab.wave_path(cfg, wave) / "status"))
    head = Text()
    head.append(f"{wave}  ", style="bold magenta")
    head.append(f"tmux attach -t ={w['tmux']}", style="bold white on grey23")
    head.append(f"   статус: {status}", style="yellow" if status.startswith("BLOCKED") else "green")
    ctx = Group(Text("Контекст ", style="bold").append(bar(w.get("tokens", 0), cfg["ctx_limit"], 40)),
                Text("История  ", style="bold").append(spark(w.get("ctx_hist", []), cfg["ctx_limit"])))
    screen = Text(screen_text(w["tmux"]), style="grey78")
    return Panel(Group(head, Text(), ctx, Text(), Panel(screen, title="экран волны (live)",
                                                         border_style="grey35", box=box.ROUNDED)),
                 title=f"⚙ Текущая волна {wave}", border_style="magenta")


def events_panel(cfg, n=12):
    p = cfg["run_dir"] / "events.log"
    lines = p.read_text(encoding="utf-8").splitlines()[-n:] if p.exists() else []
    t = Text()
    for l in lines:
        ts, _, msg = l.partition(" ")
        colour = ("red" if any(k in msg for k in ("BLOCKED", "gone", "FAILED", "idle", "permission"))
                  else "yellow" if any(k in msg for k in ("checkpoint", "handoff", "/clear"))
                  else "green" if any(k in msg for k in ("DONE", "launched", "finished"))
                  else "grey70")
        t.append(ts[11:19] + " ", style="grey50")
        t.append(msg[:140] + "\n", style=colour)
    return Panel(t or Text("событий пока нет", style="grey50"), title="📜 События", border_style="blue")


def roles_line(cfg, st):
    """Роли и модели одной строкой; роль на fallback помечена «⚠ роль: было → стало (fallback)»."""
    eff = st.get("roles_effective") or cfg["roles"]
    fb = st.get("role_fallbacks") or {}
    parts = [f"{r} {m}" for r, m in eff.items() if r not in fb]
    marks = [f"⚠ {r}: {v['from']} → {v['to']} (fallback)" for r, v in fb.items()]
    return "роли: " + " · ".join(parts + marks) if parts or marks else ""


def header(cfg, st):
    waves = st["waves"]
    done = sum(1 for w in wab.wave_ids(cfg) if wab.read(wab.wave_path(cfg, w) / "status") == "DONE")
    started = min((w["started"] for w in waves.values()), default=time.time())
    restarts = sum(w.get("restarts", 0) for w in waves.values())
    turns = sum(transcript_stats(w["cwd"])["turns"] for w in waves.values())
    t = Text(justify="center")
    t.append("🌊 wave-autobot ", style="bold bright_cyan")
    t.append(f"· {cfg['chain']} ", style="bold white")
    t.append(f"·  волн {done}/{len(cfg['waves'])}  ", style="green")
    t.append(f"·  в работе {fmt_dur(time.time() - started)}  ", style="white")
    t.append(f"·  свежих голов {restarts}  ", style="yellow")
    t.append(f"·  ходов {turns}  ", style="cyan")
    t.append(f"·  порог {ktok(cfg['ctx_limit'])}  ", style="grey62")
    t.append(time.strftime("·  %H:%M:%S UTC", time.gmtime()), style="grey50")
    t.append("\n")
    fb = st.get("role_fallbacks") or {}
    t.append(roles_line(cfg, st), style="bold yellow" if fb else "grey62")
    return t


def render(cfg):
    st = wab.load_state(cfg)
    lay = Layout()
    lay.split_column(Layout(Panel(header(cfg, st), border_style="bright_cyan"), size=4),
                     Layout(Panel(pipeline(cfg, st), title="Конвейер", border_style="cyan"), size=5),
                     Layout(Panel(waves_table(cfg, st), title="📊 Статистика волн", border_style="cyan"),
                            size=len(cfg["waves"]) + 6),
                     Layout(name="bottom"))
    lay["bottom"].split_row(Layout(current_panel(cfg, st), ratio=3), Layout(events_panel(cfg), ratio=2))
    return lay


def safe_render(cfg):
    """The dispatcher rewrites state.json, status and events.log while we read them:
    a half-written file or a missing key must cost one frame, not the whole view."""
    try:
        return render(cfg)
    except Exception as e:  # noqa: BLE001 — any read race; the next frame retries
        return Panel(Text(f"кадр не отрисован: {type(e).__name__}: {e}\nповтор через 3 с",
                          style="yellow"), title="dash", border_style="yellow")


def main():
    if len(sys.argv) != 2:
        sys.exit("использование: dash.py <waves.json>")
    try:
        cfg = wab.load_waves(sys.argv[1])
    except ConfigError as e:
        sys.exit(str(e))
    with Live(safe_render(cfg), refresh_per_second=1, screen=True) as live:
        while True:
            time.sleep(3)
            live.update(safe_render(cfg))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
