#!/usr/bin/env python3
"""wave-autobot, диспетчер: гонит волны плана, каждую в своей tmux-сессии Claude.

Команды:
  wab.py launch <waves.json> <волна> <файл-промпта>   запустить одну волну в tmux
  wab.py watch  <waves.json>                           следить за текущей волной до DONE
  wab.py status <waves.json>                           статус одним экраном
  wab.py validate <waves.json>                         проверить конфиг и напечатать его

Каждая сессия волны пишет $WAB_DIR/status (RUNNING | HANDOFF_READY | BLOCKED: … | DONE),
handoff.md, result.md, next-prompt.md — см. PROTOCOL.md в корне репозитория.
После DONE диспетчер закрывает окно волны и останавливается: следующую волну сам не
запускает (сначала мердж PR и решение координатора), а печатает команду launch для неё.
Нужен tmux >= 3.2: new-session принимает команду списком аргументов (3.0+) и ключ -e (3.2+).
Нужен git >= 2.36: пути worktree читаются из `git worktree list --porcelain -z`.
Импорт модуля ничего не запускает и не создаёт файлов.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

import waves_config
from waves_config import ConfigError, load_waves

HERE = pathlib.Path(__file__).resolve().parent
PROTOCOL = HERE.parent / "PROTOCOL.md"
PROJECTS = pathlib.Path.home() / ".claude" / "projects"
PERMISSION_MARKERS = ("Do you want to proceed", "Do you want to make this edit",
                      "Do you want to create", "❯ 1. Yes")
READY_MARKERS = ("? for shortcuts", "shift+tab to cycle", "for agents")
TRUST_MARKERS = ("Yes, I trust this folder", "Do you trust the files")
HANDOFF_TIMEOUT_MINUTES = 25   # сколько ждём handoff после запроса контрольной точки


# ---------- конфиг и состояние ----------

def wave_ids(cfg):
    return [w["id"] for w in cfg["waves"]]


def state_path(cfg):
    return cfg["run_dir"] / "state.json"


def load_state(cfg):
    p = state_path(cfg)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"waves": {}}


def save_state(cfg, st):
    cfg["run_dir"].mkdir(parents=True, exist_ok=True)
    tmp = state_path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path(cfg))


def event(cfg, text, trusted=""):
    """Строка в журнал событий (events.log) и на экран; дашборд читает журнал.

    `text` может нести слова сессии волны (статус, вопрос BLOCKED, result.md), поэтому
    всегда вычищается здесь, в одном месте, до склейки строк, печати и записи.
    `trusted` — хвост, который диспетчер собрал сам из своих значений (команда launch,
    путь рабочей копии); он дописывается без вычистки, иначе длинный путь съедается как
    «непрозрачная строка». Агентский текст в `trusted` не передавать никогда.
    """
    text = redact(text.replace("\x00", ""), REDACT_MESSAGE_LIMIT)
    text = " ⏎ ".join(l for l in text.splitlines() if l.strip())
    trusted = " ".join(trusted.replace("\x00", "").splitlines())
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z {text}{trusted}"
    cfg["run_dir"].mkdir(parents=True, exist_ok=True)
    with open(cfg["run_dir"] / "events.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def wave_path(cfg, wave):
    """Каталог волны без создания (для чтения)."""
    return cfg["run_dir"] / wave


def wave_dir(cfg, wave):
    d = wave_path(cfg, wave)
    d.mkdir(parents=True, exist_ok=True)
    return d


def read(p):
    p = pathlib.Path(p)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


# ---------- tmux ----------

def sh(*args, check=True, **kw):
    return subprocess.run(args, check=check, capture_output=True, text=True, **kw)


def sess_target(name):
    """Цель-сессия с точным совпадением: `-t имя` совпало бы по префиксу (w1 найдёт w10)."""
    return f"={name}"


def pane_target(name):
    """Цель-pane активного окна сессии `name`, тоже с точным совпадением."""
    return f"={name}:"


def send_keys(name, *keys, check=True):
    return sh("tmux", "send-keys", "-t", pane_target(name), *keys, check=check)


def tmux_alive(name):
    return sh("tmux", "has-session", "-t", sess_target(name), check=False).returncode == 0


def pane_text(name):
    r = sh("tmux", "capture-pane", "-p", "-t", pane_target(name), check=False)
    return r.stdout if r.returncode == 0 else ""


def send_text(name, text):
    """Вставить текст одной bracketed-вставкой и отправить. Буфер приватный для этого
    диспетчера и сессии: буферы tmux общие на весь сервер."""
    buf = f"wab-{os.getpid()}-{name}"
    sh("tmux", "load-buffer", "-b", buf, "-", input=text)
    sh("tmux", "paste-buffer", "-p", "-d", "-b", buf, "-t", pane_target(name))
    time.sleep(1.5)
    send_keys(name, "Enter")


def send_command(name, cmd):
    send_keys(name, "-l", cmd)
    time.sleep(0.7)
    send_keys(name, "Enter")


def wait_ready(name, timeout=90):
    """Ждать поле ввода Claude; диалог доверия к папке принимаем — папка наша."""
    end = time.time() + timeout
    while time.time() < end:
        txt = pane_text(name)
        if any(m in txt for m in TRUST_MARKERS):
            # по умолчанию выбрано «No, exit»: сначала сдвигаемся на «Yes, I trust this folder»
            send_keys(name, "Down")
            time.sleep(0.5)
            send_keys(name, "Enter")
            time.sleep(3)
            continue
        if any(m in txt for m in READY_MARKERS):
            return True
        time.sleep(2)
    return False


# ---------- заполненность контекста по транскрипту ----------

def transcript_dir(cwd):
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def context_tokens(cwd):
    """Токены в окне на последнем ходе ассистента основного потока в самом свежем транскрипте."""
    d = transcript_dir(cwd)
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    if not files:
        return 0
    with open(files[-1], "rb") as f:  # читаем ограниченный хвост: транскрипты вырастают до сотен МБ
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4_000_000))
        lines = f.read().splitlines()[-400:]
    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        u = (d.get("message") or {}).get("usage")
        if u:
            return (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    + u.get("cache_read_input_tokens", 0))
    return 0


# ---------- вычистка секретов из текста ----------

# Текст пишет сессия волны, и в нём могут оказаться секреты или персональные данные.
# Всё, что выходит за пределы каталога волны, пропускается через redact().
REDACT_LIMIT = 600           # цитата из текста волны (result.md, вопрос BLOCKED)
REDACT_MESSAGE_LIMIT = 1200  # сообщение целиком; цитату сначала режем до REDACT_LIMIT
_REDACT = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    # Слева у префиксных шаблонов нет \b: секрет может быть приклеен к пути или слову
    # («…/runs/2026sk-…», «abcghp_…»). У sk-/pk-/rk- без границы слова нужна цифра в теле,
    # чтобы не скрывать обычные слова вроде «task-implementing-features».
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|(?:sk|pk|rk)-(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}\b"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}\b"),                         # токен бота
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|dsn|"
               r"connection[_-]?string|accountkey|sharedaccesskey)\b(\s*[:=]\s*)"
               r"(?:\"[^\"]*\"?|'[^']*'?|\S+)"),                             # значение целиком, в кавычках тоже
    re.compile(r"(?i)\b((?:proxy-)?authorization)(\s*:\s*)(?:(?:bearer|basic|token|digest)\s+)?\S+"),
    re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),                    # логин:пароль в URL
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),                      # адреса почты
    re.compile(r"\+\d[\d ()-]{8,}\d"),                                  # телефоны (+7 …)
    re.compile(r"\b[A-Za-z0-9+/_]{40,}={0,2}"),                          # длинные непрозрачные строки
]


def redact(text, limit=REDACT_LIMIT):
    for rx in _REDACT:
        text = rx.sub(lambda m: (m.group(1) + m.group(2) + "[скрыто]") if rx.groups >= 2 and m.group(2)
                      else "[скрыто]", text)
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


# ---------- рабочая копия волны ----------

def _git(checkout, *args):
    return sh("git", "-C", str(checkout), *args, check=False)


GIT_MIN = (2, 36)  # `git worktree list -z` появился в git 2.36


def _git_version():
    """Версия git кортежем (major, minor) или None, если разобрать не удалось."""
    r = sh("git", "version", check=False)
    m = re.search(r"(\d+)\.(\d+)", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _require_git_worktree_z():
    """SystemExit с требованием версии, если git слишком стар для `worktree list -z`."""
    v = _git_version()
    if v is not None and v < GIT_MIN:
        raise SystemExit(f"нужен git >= {GIT_MIN[0]}.{GIT_MIN[1]} (`git worktree list -z`), "
                         f"установлен {v[0]}.{v[1]}; обновите git")


def _parse_worktrees_z(out):
    """Разбор `git worktree list --porcelain -z`: атрибуты разделены NUL, запись кончается пустым полем.

    Возвращает {путь worktree (resolve): ref ветки или None}."""
    paths = {}
    cur = None
    for field in out.split("\0"):
        if field == "":
            cur = None
        elif field.startswith("worktree "):
            cur = pathlib.Path(field[len("worktree "):]).resolve()
            paths[cur] = None
        elif field.startswith("branch ") and cur is not None:
            paths[cur] = field[len("branch "):]
    return paths


def _locked_worktrees_z(out):
    """Пути (resolve) worktree с атрибутом `locked` из `git worktree list --porcelain -z`."""
    locked = set()
    cur = None
    for field in out.split("\0"):
        if field == "":
            cur = None
        elif field.startswith("worktree "):
            cur = pathlib.Path(field[len("worktree "):]).resolve()
        elif (field == "locked" or field.startswith("locked ")) and cur is not None:
            locked.add(cur)
    return locked


def _common_dir(path):
    """Абсолютный общий git-каталог (--git-common-dir) для path или None. --path-format — git >= 2.31."""
    r = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0:
        return None
    return pathlib.Path(r.stdout.removesuffix("\n")).resolve()


def _is_worktree_of(wt, branch, checkout):
    """Каталог — настоящая рабочая копия checkout: корень git ровно wt, общий git-каталог тот же,
    что у checkout (а не отдельный репозиторий с веткой того же имени), HEAD на ветке волны."""
    r = _git(wt, "rev-parse", "--show-toplevel")
    if r.returncode != 0 or pathlib.Path(r.stdout.removesuffix("\n")).resolve() != wt:
        return False
    common = _common_dir(wt)
    if common is None or common != _common_dir(checkout):
        return False
    r = _git(wt, "symbolic-ref", "--quiet", "HEAD")
    return r.returncode == 0 and r.stdout.removesuffix("\n") == f"refs/heads/{branch}"


def prepare_worktree(cfg, wave):
    """Готовит git worktree волны и возвращает его путь.

    `checkout` из конфига обязан быть корнем git-checkout. Ветка волны — wab/<chain>/<run_id>/<wave>:
    новый прогон получает новую ветку от свежего origin/<base_branch>. Уже зарегистрированный
    worktree переиспользуется; если каталог пропал (или worktree удалён), а ветка осталась,
    worktree восстанавливается на той же ветке со всеми её коммитами; так же — если каталог
    создан заново пустым (запись в git есть, метаданных нет). Такой же каталог с файлами —
    обычная папка или отдельный репозиторий с веткой того же имени — SystemExit без удаления.
    Ветка, занятая другим worktree, — SystemExit. Устаревшая запись снимается точечно
    (`git worktree remove --force` только для этой волны, без общего prune): записи чужих
    отсутствующих worktree, например на отключённом диске, остаются. Заблокированная запись
    волны (`git worktree lock`) — SystemExit, ничего не удаляется. Символическая ссылка (в том
    числе битая) или файл на месте каталога волны — SystemExit до любых git-операций.
    """
    checkout = pathlib.Path(cfg["checkout"])
    r = _git(checkout, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        raise SystemExit(f"checkout {checkout}: не git-репозиторий: {r.stderr.strip()}")
    top = pathlib.Path(r.stdout.removesuffix("\n")).resolve()
    if top != checkout.resolve():
        raise SystemExit(f"checkout {checkout}: это не корень git-checkout (корень — {top}); "
                         f"укажите в waves.json корень")

    # родителя разыменовываем, сам каталог волны — нет: симлинк на его месте не должен увести
    # rmdir и `worktree add` за пределы run_dir; для обычного каталога путь совпадает с resolve
    wt = (cfg["run_dir"] / "worktrees").resolve() / wave
    if wt.is_symlink():  # в том числе битая ссылка
        raise SystemExit(f"{wt} — символическая ссылка, а не каталог волны; уберите её вручную, "
                         f"ничего не тронуто")
    if wt.exists() and not wt.is_dir():
        raise SystemExit(f"{wt} — файл, а не каталог волны; уберите его вручную, ничего не тронуто")
    branch = f"wab/{cfg['chain']}/{cfg['run_id']}/{wave}"

    def listing_raw():
        # -z: путь отдаётся как есть, без кавычек и экранирования (core.quotePath), даже с переводом строки
        r = _git(checkout, "worktree", "list", "--porcelain", "-z")
        if r.returncode != 0:
            _require_git_worktree_z()
            raise SystemExit(f"git worktree list: {r.stderr.strip()}")
        return r.stdout

    def listing():
        return _parse_worktrees_z(listing_raw())

    raw = listing_raw()
    registered = _parse_worktrees_z(raw)

    def refuse_if_locked():
        if wt in _locked_worktrees_z(raw):
            raise SystemExit(f"worktree {wt} заблокирован: git worktree unlock {wt}; "
                             f"файлы, ветки и запись не тронуты")

    if wt in registered and wt.is_dir():
        actual = registered[wt]
        if actual != f"refs/heads/{branch}":
            shown = actual[len("refs/heads/"):] if actual and actual.startswith("refs/heads/") \
                else (actual or "detached HEAD")
            raise SystemExit(f"worktree {wt} стоит не на ветке волны: ожидается {branch}, "
                             f"фактически {shown}; переключите его обратно вручную "
                             f"(git -C {wt} switch {branch}), файлы и ветки не тронуты")
        if _is_worktree_of(wt, branch, checkout):
            return str(wt)
        # запись есть, но на месте каталога обычная папка или отдельный репозиторий (в нём всегда .git)
        if any(wt.iterdir()):
            raise SystemExit(f"{wt} зарегистрирован как worktree этого репозитория, но в каталоге нет "
                             f"его рабочей копии (чужой репозиторий или обычная папка); "
                             f"уберите каталог вручную, файлы не тронуты")
        refuse_if_locked()
        wt.rmdir()  # пустой каталог ничего не хранит; дальше — восстановление на той же ветке
    if wt in registered:  # запись есть, каталога нет: убираем ровно запись этой волны, чужие не трогаем
        refuse_if_locked()
        # каталога волны здесь гарантированно нет (пустой удалён выше, непустой — отказ),
        # поэтому --force не может стереть пользовательские файлы: он лишь снимает запись
        r = _git(checkout, "worktree", "remove", "--force", str(wt))
        if r.returncode != 0:
            raise SystemExit(f"git worktree remove {wt}: {r.stderr.strip()}")
        registered = listing()
    elif wt.exists():
        raise SystemExit(f"{wt} уже существует, но не является worktree этого репозитория")
    for path, ref in registered.items():
        if ref == f"refs/heads/{branch}":
            raise SystemExit(f"ветка {branch} занята: она уже выбрана в worktree {path}")

    wt.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(checkout, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    if exists:
        r = _git(checkout, "worktree", "add", str(wt), branch)
    else:
        base = cfg["base_branch"]
        # явный refspec: в single-branch клоне обычный fetch кладёт базу только в FETCH_HEAD
        r = _git(checkout, "fetch", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        if r.returncode != 0:
            raise SystemExit(f"git fetch origin {base}: {r.stderr.strip()}")
        r = _git(checkout, "worktree", "add", "-b", branch, str(wt), f"refs/remotes/origin/{base}")
    if r.returncode != 0:
        raise SystemExit(f"git worktree add {branch}: {r.stderr.strip()}")
    return str(wt)


# ---------- запуск ----------

def launch(cfg, wave, prompt_file):
    """Запустить одну волну. False, если окно Claude не стало готовым: тогда ничего не отправляем."""
    if wave not in wave_ids(cfg):
        raise SystemExit(f"волны «{wave}» нет в waves.json (есть: {', '.join(wave_ids(cfg))})")
    # промпт читаем и проверяем ДО любых побочных эффектов (worktree, tmux, state)
    try:
        prompt = pathlib.Path(prompt_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"файл промпта {prompt_file} не прочитан: {e}")
    if not prompt:
        raise SystemExit(f"файл промпта {prompt_file} пустой")
    st = load_state(cfg)
    name = f"{cfg['tmux_prefix']}{wave.lower()}"
    cur = st.get("current")
    if cur and cur != wave:
        # иначе диспетчер потеряет идущую волну, а зависимые волны пойдут параллельно
        cur_name = (st.get("waves", {}).get(cur) or {}).get("tmux") or f"{cfg['tmux_prefix']}{str(cur).lower()}"
        msg = f"сейчас идёт волна {cur} (tmux {cur_name}); дождитесь DONE или остановите её"
        if not tmux_alive(cur_name):
            phase = (st.get("waves", {}).get(cur) or {}).get("phase") or "?"
            msg += (f". Сессии {cur_name} нет (фаза {phase}): продолжите именно её — "
                    f"wab.py launch <waves.json> {shlex.quote(str(cur))} <файл-промпта>")
        raise SystemExit(msg)
    if tmux_alive(name):
        raise SystemExit(f"tmux-сессия {name} уже существует")
    wdir = wave_dir(cfg, wave)
    cwd = prepare_worktree(cfg, wave)
    (wdir / "status").write_text("STARTING\n", encoding="utf-8")
    cmd = ["claude", "--permission-mode", "auto", "--append-system-prompt-file", str(PROTOCOL),
           "--name", f"wab-{cfg['chain']}-{wave}"]
    sh("tmux", "new-session", "-d", "-s", name, "-c", cwd, "-x", "220", "-y", "60",
       "-e", f"WAB_DIR={wdir}", "-e", f"WAB_WAVE={wave}", *cmd)
    # публикуем волну до ожидания: диспетчер, упавший здесь, не должен потерять сессию
    st["current"] = wave
    st["waves"][wave] = w = {"tmux": name, "cwd": cwd, "started": time.time(), "restarts": 0,
                             "phase": "starting", "notified": {}}
    save_state(cfg, st)
    if not wait_ready(name):
        (wdir / "status").write_text("BLOCKED: окно Claude не стало готовым, задача не отправлена\n", encoding="utf-8")
        w["phase"] = "not_ready"
        save_state(cfg, st)
        event(cfg, f"{wave}: окно Claude не готово в {name}, промпт НЕ отправлен; посмотреть: tmux attach -t ={name}")
        return False
    head = (f"[wave-autobot] Волна {wave}. Каталог волны: {wdir} (он же $WAB_DIR). "
            f"Рабочая копия (git worktree): {cwd}. Протокол — в системной инструкции.\n\n")
    (wdir / "first-prompt.md").write_text(head + prompt + "\n", encoding="utf-8")
    send_text(name, head + prompt)
    w["phase"] = "running"
    save_state(cfg, st)
    event(cfg, f"{wave}: запущена в tmux {name}", trusted=f", cwd {cwd}")
    return True


# ---------- надзор ----------

def once_per(w, key, value):
    """True при первом появлении `value` под ключом `key` (чтобы не повторять события)."""
    if w["notified"].get(key) == value:
        return False
    w["notified"][key] = value
    return True


def _has_line_break(s):
    """Есть ли в строке разделитель, на котором режет str.splitlines() (\\n, \\r, \\u2028 …)."""
    lines = s.splitlines()
    return len(lines) > 1 or (bool(lines) and lines[0] != s)


def tick(cfg, st, waves_json=None):
    wave = st.get("current")
    if not wave:
        return False
    w = st["waves"][wave]
    name, wdir = w["tmux"], wave_dir(cfg, wave)
    status = read(wdir / "status")
    now = time.time()
    attach = f"tmux attach -t ={name}  (выйти: Ctrl-b d)"

    # сначала DONE: волна могла закончиться и закрыть окно между двумя тиками
    if status == "DONE":
        # DONE — это «PR готов, CI зелёный», а не «смержено». Следующая волна строится от
        # свежего origin/<base_branch>, поэтому без мерджа она стартовала бы без этой волны.
        # Диспетчер сам следующую волну не запускает: цепочка стоит до мерджа и координатора.
        nxt = wdir / "next-prompt.md"
        ids = wave_ids(cfg)
        idx = ids.index(wave)
        event(cfg, f"{wave}: DONE")
        send_keys(name, "-l", "/exit", check=False)
        send_keys(name, "Enter", check=False)
        w["phase"] = "done"
        w["finished"] = now
        st["current"] = None
        save_state(cfg, st)
        if idx + 1 >= len(ids):
            event(cfg, "цепочка завершена")
        elif not nxt.exists():
            event(cfg, f"{wave} готова, но нет next-prompt.md — следующую волну не запускаю")
        else:
            # команду собирает сам диспетчер из своих путей: квотируем для shell, не вычищаем
            args = [waves_json or "", ids[idx + 1], str(nxt)]
            if any(_has_line_break(a) for a in args):
                # event() склеивает строки trusted в одну: команда указала бы на другой путь
                event(cfg, f"{wave}: готова; жду мерджа PR и координатора. Путь содержит перевод "
                           f"строки — команду не печатаю, запустите следующую волну {ids[idx + 1]} вручную.")
            else:
                target = shlex.quote(waves_json) if waves_json else "<waves.json>"
                cmd = " ".join(["wab.py", "launch", target, shlex.quote(ids[idx + 1]), shlex.quote(str(nxt))])
                event(cfg, f"{wave}: готова; жду мерджа PR и координатора.", trusted=" Следующая волна: " + cmd)
        return False

    if not tmux_alive(name):
        if once_per(w, "dead", "1"):
            event(cfg, f"{wave}: tmux-сессия {name} закрыта (статус «{status}»), цепочка стоит")
        w["phase"] = "dead"
        save_state(cfg, st)
        return False  # цепочка стоит: перезапустить волну руками и снова запустить watch

    if status.startswith("BLOCKED"):
        if once_per(w, "blocked", status):
            event(cfg, f"{wave}: {status[:200]}; ответить: {attach}")
        save_state(cfg, st)
        return True

    if status == "HANDOFF_READY" and w["phase"] == "checkpoint":
        event(cfg, f"{wave}: handoff готов, /clear + /update (перезапуск №{w['restarts'] + 1})")
        send_command(name, "/clear")
        time.sleep(6)
        send_text(name, f"/update Продолжаем волну {wave} wave-autobot. Каталог волны: {wdir}. "
                        f"Прочитай {wdir}/handoff.md и продолжи с шага «Следующий шаг».")
        w["restarts"] += 1
        w["phase"] = "running"
        w["checkpoint_at"] = None
        (wdir / "status").write_text("RESUMING\n", encoding="utf-8")
        save_state(cfg, st)
        return True

    txt = pane_text(name)
    tokens = context_tokens(w["cwd"])
    w["tokens"] = tokens
    w["peak"] = max(w.get("peak", 0), tokens)
    w["ctx_hist"] = (w.get("ctx_hist", []) + [tokens])[-120:]

    if w["phase"] == "running" and tokens >= cfg["ctx_limit"]:
        event(cfg, f"{wave}: контекст {tokens} >= {cfg['ctx_limit']}, запрошена контрольная точка")
        w["phase"] = "checkpoint"  # сначала сохраняем: перезапущенный watch обязан увидеть запрос
        w["checkpoint_at"] = now
        w["checkpoint_sent"] = False
        save_state(cfg, st)
    if w["phase"] == "checkpoint" and not w.get("checkpoint_sent", True):
        # досылается и после перезапуска диспетчера между сохранением выше и доставкой
        send_text(name, f"WAB-CHECKPOINT: контекст {tokens // 1000}k токенов. По протоколу: доведи "
                        f"шаг, закоммить и запушь WIP, перепиши {wdir}/handoff.md, запиши "
                        f"HANDOFF_READY в {wdir}/status и остановись.")
        w["checkpoint_sent"] = True
        save_state(cfg, st)
    elif w["phase"] == "checkpoint" and now - (w.get("checkpoint_at") or now) > HANDOFF_TIMEOUT_MINUTES * 60:
        if once_per(w, "checkpoint_timeout", str(w.get("checkpoint_at"))):
            event(cfg, f"{wave}: handoff не записан за {HANDOFF_TIMEOUT_MINUTES} мин после запроса; {attach}")

    if any(m in txt for m in PERMISSION_MARKERS):
        if once_per(w, "permission", hashlib.sha1(txt[-800:].encode()).hexdigest()):
            event(cfg, f"{wave}: на экране запрос подтверждения (permission prompt); {attach}")

    digest = hashlib.sha1(txt.encode()).hexdigest()
    if digest != w.get("pane_digest"):
        w["pane_digest"], w["pane_changed"] = digest, now
    elif now - w.get("pane_changed", now) > cfg["idle_minutes"] * 60:
        if once_per(w, "idle", digest):
            event(cfg, f"{wave}: экран не меняется {cfg['idle_minutes']}+ мин (idle), статус «{status}»; {attach}")
    save_state(cfg, st)
    return True


def watch(cfg, path):
    event(cfg, f"watch запущен, ctx_limit={cfg['ctx_limit']}")
    last = 0.0
    while True:
        try:
            cfg = load_waves(path)  # пороги можно подкручивать на ходу
        except ConfigError as e:
            event(cfg, f"конфиг не перечитан, остаются прежние значения: {e}")
        st = load_state(cfg)
        if not tick(cfg, st, str(pathlib.Path(path).resolve())):
            event(cfg, "watch остановлен: нет текущей волны")
            return
        st = load_state(cfg)
        w = st["waves"].get(st.get("current") or "", {})
        if time.time() - last > 600 and w:
            event(cfg, f"{st['current']}: phase={w.get('phase')} ctx={w.get('tokens', 0) // 1000}k "
                       f"restarts={w.get('restarts')} status={read(wave_path(cfg, st['current']) / 'status')}")
            last = time.time()
        time.sleep(cfg["tick_seconds"])


def status_cmd(cfg):
    st = load_state(cfg)
    print("current:", st.get("current"))
    for wave, w in st["waves"].items():
        print(f"{wave}: tmux={w['tmux']} phase={w['phase']} ctx={w.get('tokens', 0) // 1000}k "
              f"restarts={w['restarts']} status={read(wave_path(cfg, wave) / 'status')}")


# ---------- командная строка ----------

def build_parser():
    p = argparse.ArgumentParser(prog="wab.py", description="wave-autobot: диспетчер волн в tmux.")
    sub = p.add_subparsers(dest="cmd", metavar="команда", required=True)
    s = sub.add_parser("launch", help="запустить одну волну в tmux")
    s.add_argument("waves_json")
    s.add_argument("wave", help="id волны из waves.json")
    s.add_argument("prompt_file", help="файл со стартовым промптом волны")
    s = sub.add_parser("watch", help="следить за текущей волной до DONE")
    s.add_argument("waves_json")
    s = sub.add_parser("status", help="статус одним экраном")
    s.add_argument("waves_json")
    s = sub.add_parser("validate", help="проверить waves.json и напечатать нормализованный конфиг")
    s.add_argument("waves_json")
    return p


def main(argv=None):
    # вывод не должен падать на кодировке терминала (ascii, C-локаль)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        cfg = load_waves(args.waves_json)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if args.cmd == "validate":
        print(json.dumps(cfg, ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "launch":
        if not launch(cfg, args.wave, args.prompt_file):
            print(f"{args.wave}: окно Claude не стало готовым, задача волне не отправлена "
                  f"(подробности в событиях и status волны)", file=sys.stderr)
            return 3
    elif args.cmd == "watch":
        watch(cfg, args.waves_json)
    elif args.cmd == "status":
        status_cmd(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
