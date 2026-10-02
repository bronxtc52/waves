#!/usr/bin/env python3
"""wave-autobot, диспетчер: гонит волны плана, каждую в своей tmux-сессии Claude.

Команды:
  wab.py launch <waves.json> <волна> <файл-промпта>   запустить одну волну в tmux
  wab.py watch  <waves.json>                           следить за текущей волной до DONE
  wab.py status <waves.json>                           статус одним экраном
  wab.py validate <waves.json>                         проверить конфиг и напечатать его
  wab.py models <waves.json> [--refresh]               роли → модели с проверкой доступности и fallback

Каждая сессия волны пишет $WAB_DIR/status (RUNNING | HANDOFF_READY | BLOCKED: … | DONE),
handoff.md, result.md, next-prompt.md — см. PROTOCOL.md в корне репозитория.
После DONE диспетчер закрывает окно волны и останавливается: следующую волну сам не
запускает (сначала мердж PR и решение координатора), а печатает команду launch для неё.
Нужен tmux >= 3.2: new-session принимает команду списком аргументов (3.0+) и ключ -e (3.2+).
Нужен git >= 2.36: пути worktree читаются из `git worktree list --porcelain -z`.
Импорт модуля ничего не запускает и не создаёт файлов.
"""
import argparse
import contextlib
import copy
import fcntl
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
RELOADABLE = ("ctx_limit", "idle_minutes", "tick_seconds")  # что watch перечитывает на ходу
CLEAR_SETTLE_SECONDS = 6       # пауза прототипа, проверена вживую; детерминированный сигнал окончания /clear — волна W3
PROBE_TIMEOUT_SECONDS = 120    # проверка модели: один короткий `claude -p`
RUN_LOCK_TIMEOUT_SECONDS = 30  # дольше блокировку прогона не ждём: зависший wab.py не вешает launch навсегда


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


@contextlib.contextmanager
def run_lock(cfg, timeout=None):
    """Межпроцессная блокировка прогона: flock(LOCK_EX) на run_dir/state.lock.

    Сериализует «прочитать state → проверить → записать» между процессами wab.py одного
    прогона (два координатора с launch, watch). Ожидание ограничено `timeout` секундами
    (по умолчанию RUN_LOCK_TIMEOUT_SECONDS), дальше — SystemExit с понятным текстом.
    Блокировка не реентерабельна: внутри неё run_lock не вызывать (flock на втором
    дескрипторе того же процесса тоже ждёт). Симлинк вместо state.lock — отказ.
    """
    timeout = RUN_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    run_dir = cfg["run_dir"]
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "state.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except OSError as e:
        raise SystemExit(f"файл блокировки {path} не открыт: {e}")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SystemExit(f"другой wab.py держит блокировку прогона {run_dir} дольше {timeout} с "
                                     f"(файл {path}); повторите позже или проверьте, не завис ли он")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # закрытие дескриптора снимает flock


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


def pane_text(name, join=False):
    """Текст панели. join=True — `capture-pane -J`: мягко перенесённые терминалом строки
    склеиваются, и секрет, разрезанный переносом, redact() узнаёт целиком (для показа наружу).
    Маркеры готовности ищутся на обычном захвате: им склейка не нужна."""
    flags = ("-p", "-J") if join else ("-p",)
    r = sh("tmux", "capture-pane", *flags, "-t", pane_target(name), check=False)
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
    # Поле-секрет: имя видно, значение скрыто. Имя может стоять в кавычках (JSON, словарь
    # Python: {"password": "x"}, 'token': 'x') — закрывающая кавычка входит в группу 2 вместе
    # с разделителем. Значение в кавычках скрывается целиком, с пробелами внутри; без кавычек —
    # до пробела целиком (запятые внутри тоже), с конца отбрасываются только завершающие
    # «,)]}» разметки. Жадный \S* с откатом на хвосте линеен — без вложенных повторов.
    # Слово «token» без разделителя не трогается.
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|dsn|"
               r"connection[_-]?string|accountkey|sharedaccesskey)\b([\"']?\s*[:=]\s*)"
               r"(?:\"[^\"]*\"?|'[^']*'?|\S*[^\s,)\]}])"),
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

def _pid_alive(pid):
    """Жив ли процесс с этим pid на этой машине (для резерва волны другим launch)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _update_wave(cfg, wave, **fields):
    """Под блокировкой прогона перечитать state и обновить поля записи волны (не затирая чужие записи)."""
    with run_lock(cfg):
        st = load_state(cfg)
        w = st.setdefault("waves", {}).setdefault(wave, {})
        w.update(fields)
        if fields.get("phase") in ("running", "not_ready"):
            w.pop("launcher_pid", None)
        save_state(cfg, st)
        return w


def _release_reservation(cfg, wave, prev_current, prev_rec):
    """Снять резерв неудачного launch: вернуть прежний current и прежнюю запись волны (или убрать её)."""
    with run_lock(cfg):
        st = load_state(cfg)
        if st.get("current") != wave or (st.get("waves", {}).get(wave) or {}).get("launcher_pid") != os.getpid():
            return  # резерв уже не наш: state поменял кто-то другой, не трогаем
        st["current"] = prev_current
        if prev_rec is None:
            st["waves"].pop(wave, None)
        else:
            st["waves"][wave] = prev_rec
        save_state(cfg, st)


def _check_launchable(cfg, st, wave, name):
    """Можно ли запускать волну: другая текущая волна, уже идущий launcher, живая tmux-сессия.

    Вызывается под run_lock. SystemExit с понятным текстом; иначе (current, прежняя запись волны).
    """
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
    prev_rec = (st.get("waves") or {}).get(wave)
    if cur == wave and (prev_rec or {}).get("phase") == "starting" and _pid_alive(prev_rec.get("launcher_pid")):
        raise SystemExit(f"волна {wave} уже запускается другим wab.py (pid {prev_rec['launcher_pid']}); "
                         f"дождитесь его завершения")
    if tmux_alive(name):
        raise SystemExit(f"tmux-сессия {name} уже существует")
    return cur, prev_rec


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
    name = f"{cfg['tmux_prefix']}{wave.lower()}"
    # дешёвая предварительная проверка — до платных проверок моделей; окончательная — под резервом ниже
    with run_lock(cfg):
        _check_launchable(cfg, load_state(cfg), wave, name)
    # модели ролей — до резерва и вне блокировки: проверка долгая, а блокировку ждут 30 с
    roles, _ = ensure_roles(cfg)
    wave_obj = next(w for w in cfg["waves"] if w["id"] == wave)
    # проверка и резерв — одним шагом под блокировкой прогона: иначе два координатора при
    # пустом current оба пройдут проверку и запустят две волны, а current достанется последней
    with run_lock(cfg):
        st = load_state(cfg)
        cur, prev_rec = _check_launchable(cfg, st, wave, name)
        # резерв до worktree и tmux: параллельный launch увидит волну, а watch — её запись
        st.setdefault("waves", {})
        st["current"] = wave
        st["waves"][wave] = {"tmux": name, "cwd": None, "started": time.time(), "restarts": 0,
                             "phase": "starting", "notified": {}, "launcher_pid": os.getpid()}
        save_state(cfg, st)
    try:
        wdir = wave_dir(cfg, wave)
        cwd = prepare_worktree(cfg, wave)
        (wdir / "status").write_text("STARTING\n", encoding="utf-8")
        sp = wdir / "system-prompt.md"
        sp.write_text(system_prompt_text(cfg, wave_obj, wdir, cwd, roles), encoding="utf-8")
        cmd = wave_argv(cfg, wave, roles, sp)
        sh("tmux", "new-session", "-d", "-s", name, "-c", cwd, "-x", "220", "-y", "60",
           "-e", f"WAB_DIR={wdir}", "-e", f"WAB_WAVE={wave}", *cmd)
    except BaseException:
        # неудачный launch не должен оставить цепочку «занятой»
        _release_reservation(cfg, wave, cur, prev_rec)
        raise
    # сессия создана: публикуем cwd до ожидания — диспетчер, упавший здесь, не потеряет сессию
    _update_wave(cfg, wave, cwd=cwd)
    if not wait_ready(name):
        (wdir / "status").write_text("BLOCKED: окно Claude не стало готовым, задача не отправлена\n", encoding="utf-8")
        _update_wave(cfg, wave, phase="not_ready")
        event(cfg, f"{wave}: окно Claude не готово в {name}, промпт НЕ отправлен; посмотреть: tmux attach -t ={name}")
        return False
    head = (f"[wave-autobot] Волна {wave}. Каталог волны: {wdir} (он же $WAB_DIR). "
            f"Рабочая копия (git worktree): {cwd}. Протокол — в системной инструкции.\n\n")
    (wdir / "first-prompt.md").write_text(head + prompt + "\n", encoding="utf-8")
    send_text(name, head + prompt)
    _update_wave(cfg, wave, phase="running")
    event(cfg, f"{wave}: запущена в tmux {name}", trusted=f", cwd {cwd}")
    return True


# ---------- роли, модели, системная инструкция ----------

def agents_json(roles):
    """JSON для `claude --agents`: субагенты волны на моделях ролей (roles — эффективные модели)."""
    agents = {
        "wave-tester": {
            "description": "Тестер волны: свежий контекст, гоняет тесты и сценарии «Готово, когда», отвечает PASS/FAIL.",
            "prompt": ("Ты тестер волны. Контекста работы кодера у тебя нет — это намеренно. Тебе дадут цель волны, "
                       "критерии «Готово, когда» и команду проверки. Запусти тесты и пройди сценарии «Готово, когда» "
                       "на живых данных. Ответ: PASS или FAIL, затем вывод команд (хвост, если он длинный) и какие "
                       "сценарии не прошли. Код и тесты НЕ меняй."),
            "tools": ["Read", "Grep", "Glob", "Bash"],
            "model": roles["tester"],
        },
        "wave-reviewer": {
            "description": "Ревьюер волны: сверяет дифф с целью и «Готово, когда», находки P1/P2.",
            "prompt": ("Ты ревьюер волны. Тебе дадут путь к файлу с диффом (кодер сохраняет его туда), цель волны и "
                       "«Готово, когда». Сверь дифф с целью и критериями, найди ошибки, пропущенные случаи и "
                       "расхождения с требованиями. Каждую находку давай как P1 (блокирует) или P2 (желательно), с "
                       "файлом:строкой и сценарием, на котором это ломается. Нет находок — скажи прямо. Код НЕ меняй."),
            "tools": ["Read", "Grep", "Glob"],
            "model": roles["reviewer"],
        },
        "wave-reader": {
            "description": "Читатель: читает большие файлы и коротко отвечает на заданный вопрос.",
            "prompt": ("Ты читатель больших файлов. Тебе дадут путь и вопрос. Прочитай файл и ответь коротко и "
                       "точно на вопрос, без пересказа всего файла; укажи файл:строку, откуда взят ответ."),
            "tools": ["Read", "Grep", "Glob"],
            "model": roles["reader"],
        },
    }
    return json.dumps(agents, ensure_ascii=False)


def system_prompt_text(cfg, wave_obj, wdir, cwd, roles):
    """Системная инструкция волны: PROTOCOL.md + раздел «Контекст волны»."""
    w = wave_obj
    done = "\n".join(f"- {d}" for d in w["done_when"])
    deps = ", ".join(w["depends_on"]) or "нет"
    ctx = (f"## Контекст волны\n\n"
           f"- Волна: {w['id']} — {w['title']}\n"
           f"- Цель: {w['goal']}\n"
           f"- Готово, когда:\n{done}\n"
           f"- Команда проверки: `{w['check']}`\n"
           f"- Зависит от: {deps}\n"
           f"- Репозиторий: {cfg['repo']}\n"
           f"- Базовая ветка (PR сюда): {cfg['base_branch']}\n"
           f"- Каталог волны ($WAB_DIR): {wdir}\n"
           f"- Рабочая копия: {cwd}\n"
           f"- Модели ролей: " + ", ".join(f"{r}={m}" for r, m in roles.items()) + "\n")
    return PROTOCOL.read_text(encoding="utf-8").rstrip() + "\n\n" + ctx


def wave_argv(cfg, wave, roles, system_prompt_path):
    """Команда запуска сессии волны: кодер на своей модели, роли — субагентами, AskUserQuestion запрещён."""
    return ["claude", "--model", roles["coder"], "--permission-mode", "auto",
            "--append-system-prompt-file", str(system_prompt_path),
            "--agents", agents_json(roles), "--disallowedTools", "AskUserQuestion",
            "--name", f"wab-{cfg['chain']}-{wave}"]


def probe_model(model, timeout=PROBE_TIMEOUT_SECONDS):
    """Доступна ли модель: короткий `claude -p`. (ok, detail); вывод модели и stderr не возвращаем."""
    try:
        r = subprocess.run(["claude", "-p", "--model", model, "--no-session-persistence",
                            "Ответь одним словом: ok"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "claude не найден"
    return r.returncode == 0, f"rc={r.returncode}"


def probe_roles(cfg, cache, probe=None):
    """Проверить модели ролей (без событий и без записи state). Блокировку прогона не берёт: вызовы долгие.

    cache — прежний st['models'] (или None); проверяются только модели без записи в нём.
    Возвращает {models, effective, fallbacks, error}: error — текст отказа, если нужный fallback
    тоже недоступен (тогда effective/fallbacks неполны).
    """
    probe = probe or probe_model
    models = copy.deepcopy(cache or {})
    roles = cfg["roles"]
    fb = cfg["fallback_model"]

    def check(model):
        if model not in models:
            ok, detail = probe(model)
            models[model] = {"ok": bool(ok), "detail": detail, "checked": time.time()}

    for model in dict.fromkeys(roles.values()):
        check(model)
    effective, fallbacks, error = {}, {}, None
    for role, model in roles.items():
        if models[model]["ok"]:
            effective[role] = model
            continue
        check(fb)
        if not models[fb]["ok"]:
            error = (f"модель {model} (роль {role}) недоступна, и fallback_model {fb} тоже "
                     f"({models[fb]['detail']}); проверьте вход и доступ: claude -p --model {fb} ok")
            break
        effective[role] = fb
        fallbacks[role] = {"from": model, "to": fb}
    return {"models": models, "effective": effective, "fallbacks": fallbacks, "error": error}


def apply_notices(cfg, st, result):
    """Событие о недоступной модели — один раз: отметка st['model_notices'] {модель: detail}.

    Отметка не зависит от кэша проверок (его сбрасывает `models --refresh`) и сохраняется при
    отказе; снимается, когда модель снова доступна. Вызывать в одной критической секции с записью
    state (под run_lock), иначе два процесса напишут одно событие дважды.
    """
    roles, fb = cfg["roles"], cfg["fallback_model"]
    notices = st.setdefault("model_notices", {})
    for model in dict.fromkeys(roles.values()):
        info = result["models"].get(model)
        if info is None:
            continue
        if info["ok"]:
            notices.pop(model, None)
        elif model not in notices:
            notices[model] = info["detail"]
            users = ", ".join(r for r, m in roles.items() if m == model)
            if model == fb:
                event(cfg, f"модель {model} недоступна ({info['detail']}): роли {users}, "
                           f"fallback_model совпадает — запуск невозможен")
            elif not result["models"].get(fb, {"ok": True})["ok"]:
                event(cfg, f"модель {model} недоступна ({info['detail']}): роли {users}, "
                           f"fallback_model {fb} тоже недоступна — запуск невозможен")
            else:
                event(cfg, f"модель {model} недоступна ({info['detail']}): роли {users} → fallback {fb}")


def _store_roles(st, result):
    st["models"] = result["models"]
    st["roles_effective"] = result["effective"]
    st["role_fallbacks"] = result["fallbacks"]


def _drop_roles(st):
    """Отказ: кэша проверок и эффективных ролей в state не остаётся (отметки model_notices — остаются)."""
    for k in ("models", "roles_effective", "role_fallbacks"):
        st.pop(k, None)


def resolve_roles(cfg, st, probe=None):
    """Эффективные модели ролей в памяти (без блокировок): проверка + уведомления + запись в st.

    При отказе (fallback тоже недоступен) — SystemExit; кэш проверок в st не пишется, а отметки
    уведомлений остаются. Для прогона с общим state — ensure_roles (публикация под блокировкой).
    """
    r = probe_roles(cfg, st.get("models"), probe)
    apply_notices(cfg, st, r)
    if r["error"]:
        _drop_roles(st)
        raise SystemExit(r["error"])
    _store_roles(st, r)
    return r["effective"]


ENSURE_ROLES_ATTEMPTS = 5


def ensure_roles(cfg, refresh=False, probe=None):
    """Проверить модели вне блокировки (вызовы долгие), результат опубликовать под блокировкой.

    Публикация: перечитать state, записать отметки уведомлений (событие — только если отметки ещё
    нет) и итог. Отказ: кэш не сохраняется, отметки — да. Поколение кэша st['models_gen'] растёт
    при каждой публикации; если оно сменилось, пока шли пробы, результат устарел (другой wab.py
    обновил или сбросил кэш) — не публикуется, цикл повторяется по актуальному кэшу.
    """
    for attempt in range(ENSURE_ROLES_ATTEMPTS):
        with run_lock(cfg):
            st0 = load_state(cfg)
            gen = st0.get("models_gen", 0)
            cache = None if (refresh and attempt == 0) else copy.deepcopy(st0.get("models"))
        r = probe_roles(cfg, cache, probe)
        with run_lock(cfg):
            cur = load_state(cfg)
            if cur.get("models_gen", 0) != gen:
                continue
            apply_notices(cfg, cur, r)
            cur["models_gen"] = gen + 1
            if r["error"]:
                _drop_roles(cur)
                save_state(cfg, cur)
                raise SystemExit(r["error"])
            _store_roles(cur, r)
            save_state(cfg, cur)
        return r["effective"], r["fallbacks"]
    raise SystemExit("проверка моделей конкурирует с другим wab.py, повторите")


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
    if w.get("phase") == "starting" and _pid_alive(w.get("launcher_pid")):
        return True  # launch ещё создаёт worktree и сессию: не считать её мёртвой, ждать
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
            # статус целиком: event() вычищает до ограничения длины; срез сырого текста здесь
            # оставил бы от секрета на границе обрывок короче порога шаблона, и он ушёл бы открытым
            event(cfg, f"{wave}: {status}; ответить: {attach}")
        save_state(cfg, st)
        return True

    if status == "HANDOFF_READY" and w["phase"] == "checkpoint":
        send_command(name, "/clear")
        # сразу после /clear панель ещё показывает старый экран с «? for shortcuts» — сначала пауза
        time.sleep(CLEAR_SETTLE_SECONDS)
        if not wait_ready(name):
            msg = "BLOCKED: окно Claude не стало готовым после /clear, продолжение не отправлено"
            event(cfg, f"{wave}: {msg}; {attach}")
            w["phase"] = "not_ready"
            w["notified"]["blocked"] = msg  # ветка BLOCKED на следующем тике не повторит событие
            (wdir / "status").write_text(msg + "\n", encoding="utf-8")
            save_state(cfg, st)
            return True
        # обычный промпт, а не slash-команда: скилл не зависит от чужих команд вроде /update
        send_text(name, f"Продолжаем волну {wave} wave-autobot после /clear. Каталог волны: {wdir}. "
                        f"Прочитай {wdir}/handoff.md и продолжи с шага «Следующий шаг».")
        event(cfg, f"{wave}: handoff готов, /clear и продолжение (перезапуск №{w['restarts'] + 1})")
        w["restarts"] += 1
        w["phase"] = "running"
        w["checkpoint_at"] = None
        (wdir / "status").write_text("RESUMING\n", encoding="utf-8")
        save_state(cfg, st)
        return True

    if w["phase"] == "not_ready" and status == "RUNNING":
        # владелец подключился и вручную отправил продолжение: без возврата в running
        # контрольная точка больше не запрашивалась бы и контекст переполнился
        w["phase"] = "running"
        w["notified"].pop("blocked", None)  # будущий BLOCKED снова сообщится
        event(cfg, f"{wave}: восстановлена вручную, слежу дальше")
        save_state(cfg, st)

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
    last, warned = 0.0, None
    while True:
        # На ходу меняются только пороги. Остальные поля задают прогон (run_id, chain, волны):
        # их смена увела бы watch в чужой runs/<id>, и идущая волна осталась бы без надзора.
        try:
            fresh = load_waves(path)
        except ConfigError as e:
            event(cfg, f"конфиг не перечитан, остаются прежние значения: {e}")
        else:
            fixed = sorted(k for k in fresh if k not in RELOADABLE and fresh[k] != cfg.get(k))
            if fixed and fixed != warned:
                event(cfg, f"в waves.json изменены поля {', '.join(fixed)} — они применяются только "
                           f"перезапуском watch; на ходу применены пороги {', '.join(RELOADABLE)}")
            warned = fixed
            cfg = {**cfg, **{k: fresh[k] for k in RELOADABLE}}
        # тик целиком под блокировкой прогона: его записи state не перетирают резерв launch.
        # Внутри tick run_lock не вызывать — flock не реентерабелен.
        with run_lock(cfg):
            st = load_state(cfg)
            alive = tick(cfg, st, str(pathlib.Path(path).resolve()))
        if not alive:
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
              f"restarts={w['restarts']} status={redact(read(wave_path(cfg, wave) / 'status'))}")


def models_cmd(cfg, refresh=False):
    roles, fallbacks = ensure_roles(cfg, refresh=refresh)
    for role, model in roles.items():
        fb = fallbacks.get(role)
        print(f"{role}: {model}" + (f"  (fallback, {fb['from']} недоступна)" if fb else ""))


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
    s = sub.add_parser("models", help="роли → модели: проверить доступность, подставить fallback")
    s.add_argument("waves_json")
    s.add_argument("--refresh", action="store_true", help="сбросить кэш проверок и проверить заново")
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
    elif args.cmd == "models":
        models_cmd(cfg, refresh=args.refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
