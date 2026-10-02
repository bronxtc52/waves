"""Загрузчик и валидатор единого конфига waves.json (версия 1).

`load_waves(path)` возвращает новый dict с применёнными умолчаниями и производными полями
`run_dir` и `tmux_prefix`. Каталогов он не создаёт. Любая проблема — `ConfigError`
с сообщением вида `waves.json: waves[2].depends_on: …`.
"""
import json
import pathlib
import re

SCHEMA_VERSION = 1

ROLE_DEFAULTS = {"architect": "fable", "coder": "opus", "tester": "sonnet",
                 "reviewer": "fable", "reader": "haiku"}

REQUIRED = ("chain", "run_id", "repo", "checkout", "waves")
OPTIONAL_DEFAULTS = {"base_branch": "main", "fallback_model": "opus", "automerge": False,
                     "ctx_limit": 300_000, "idle_minutes": 12, "tick_seconds": 60,
                     "plan_sha256": None}
WAVE_REQUIRED = ("id", "title", "goal", "done_when", "check")

_NAME = re.compile(r"[A-Za-z0-9._-]+")
_CHAIN = re.compile(r"[A-Za-z0-9_-]+")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH_BAD = re.compile(r"[\x00-\x20\x7f ~^:?*\[\\]|\.\.|@\{|//")
_WAVE_ID = re.compile(r"[A-Za-z0-9_-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _ref_rule(value):
    """Правило, нарушенное значением как компонентом имени ветки git, либо None."""
    if value.startswith("-"):
        return "не может начинаться с «-» (опасно как аргумент командной строки)"
    if value.startswith("."):
        return "не может начинаться с точки"
    if value.endswith("."):
        return "не может оканчиваться на точку"
    if value.lower().endswith(".lock"):
        return "не может оканчиваться на .lock"
    if ".." in value:
        return "не может содержать «..»"
    return None


class ConfigError(ValueError):
    """Конфиг waves.json не прошёл проверку."""


class _Checker:
    def __init__(self, filename):
        self.filename = filename

    def err(self, where, text):
        prefix = f"{self.filename}: {where}: " if where else f"{self.filename}: "
        raise ConfigError(prefix + text)

    def obj(self, value, where, required, allowed):
        if not isinstance(value, dict):
            self.err(where, f"ожидается объект, получено {_tname(value)}")
        for key in value:
            if key not in allowed:
                self.err(where, f"лишний ключ {key}")
        for key in required:
            if key not in value:
                self.err(where, f"нет обязательного ключа {key}")

    def string(self, value, where, rx=None, nonempty=True, dot_only_ok=True):
        if not isinstance(value, str):
            self.err(where, f"ожидается str, получено {_tname(value)}")
        if nonempty and not value.strip():
            self.err(where, "строка не должна быть пустой")
        if rx is not None and not rx.fullmatch(value):
            self.err(where, f"значение «{value}» не соответствует формату {rx.pattern}")
        if "\x00" in value:
            self.err(where, "строка содержит NUL-символ")
        if not dot_only_ok and set(value) == {"."}:
            self.err(where, f"значение «{value}» не может состоять из одних точек")
        return value

    def ref_component(self, value, where):
        rule = _ref_rule(value)
        if rule:
            self.err(where, f"«{value}» не годится для имени ветки git: {rule}")
        return value

    def chain(self, value, where):
        """chain входит и в ветку git, и в имя tmux-сессии (там точка — разделитель окна)."""
        self.string(value, where, dot_only_ok=False)
        self.ref_component(value, where)
        if "." in value:
            self.err(where, f"«{value}»: точка недопустима в имени tmux-сессии")
        return self.string(value, where, _CHAIN)

    def boolean(self, value, where):
        if not isinstance(value, bool):
            self.err(where, f"ожидается bool, получено {_tname(value)}")
        return value

    def positive_int(self, value, where):
        if isinstance(value, bool) or not isinstance(value, int):
            self.err(where, f"ожидается int, получено {_tname(value)}")
        if value <= 0:
            self.err(where, f"ожидается int > 0, получено {value}")
        return value

    def str_list(self, value, where, nonempty):
        if not isinstance(value, list):
            self.err(where, f"ожидается list, получено {_tname(value)}")
        if nonempty and not value:
            self.err(where, "список не должен быть пустым")
        for i, item in enumerate(value):
            self.string(item, f"{where}[{i}]")
        for i, item in enumerate(value):
            if item in value[:i]:
                self.err(f"{where}[{i}]", f"дубликат «{item}»")
        return list(value)


def _tname(v):
    return {type(None): "null", bool: "bool", int: "int", float: "float", str: "str",
            list: "list", dict: "object"}.get(type(v), type(v).__name__)


def _no_duplicates(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ConfigError(f"дубликат ключа «{k}» в JSON")
        seen[k] = v
    return seen


def _read(path):
    path = pathlib.Path(path)
    name = path.name
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{name}: файл не в кодировке UTF-8") from None
    except OSError as e:
        raise ConfigError(f"{name}: не удалось прочитать файл: {e.strerror or e}") from None
    try:
        return name, json.loads(raw, object_pairs_hook=_no_duplicates)
    except ConfigError as e:
        raise ConfigError(f"{name}: {e}") from None
    except RecursionError:
        raise ConfigError(f"{name}: невалидный JSON: слишком глубокая вложенность") from None
    except ValueError as e:
        raise ConfigError(f"{name}: невалидный JSON: {e}") from None


def _check_wave(c, w, i, seen):
    where = f"waves[{i}]"
    c.obj(w, where, WAVE_REQUIRED, set(WAVE_REQUIRED) | {"depends_on"})
    wid = c.string(w["id"], f"{where}.id", _WAVE_ID)
    if wid.lower() in seen:
        c.err(f"{where}.id", f"дубликат id «{wid}» (без учёта регистра: имя tmux-сессии одно)")
    out = {"id": wid,
           "title": c.string(w["title"], f"{where}.title"),
           "goal": c.string(w["goal"], f"{where}.goal"),
           "done_when": c.str_list(w["done_when"], f"{where}.done_when", nonempty=True),
           "check": c.string(w["check"], f"{where}.check")}
    out["depends_on"] = c.str_list(w.get("depends_on", []), f"{where}.depends_on", nonempty=False)
    return out


def _check_dependencies(c, waves):
    index = {w["id"]: i for i, w in enumerate(waves)}
    for i, w in enumerate(waves):
        where = f"waves[{i}].depends_on"
        for dep in w["depends_on"]:
            if dep == w["id"]:
                c.err(where, f"волна «{dep}» зависит от самой себя")
            if dep not in index:
                c.err(where, f"неизвестная волна «{dep}»")
            if index[dep] > i:
                c.err(where, f"волна «{dep}» объявлена ниже — зависимость вперёд запрещена")


def load_waves(path):
    """Прочитать и проверить waves.json; вернуть нормализованный dict."""
    name, data = _read(path)
    c = _Checker(name)
    allowed = set(REQUIRED) | set(OPTIONAL_DEFAULTS) | {"roles"}
    c.obj(data, "", REQUIRED, allowed)

    cfg = {
        "chain": c.chain(data["chain"], "chain"),
        "run_id": c.ref_component(c.string(data["run_id"], "run_id", _NAME, dot_only_ok=False),
                                  "run_id"),
        "repo": c.string(data["repo"], "repo", _REPO),
    }
    if any(set(seg) == {"."} for seg in cfg["repo"].split("/")):
        c.err("repo", "сегмент «.» или «..» недопустим, ожидается owner/name")
    checkout = c.string(data["checkout"], "checkout")
    if not pathlib.PurePath(checkout).is_absolute():
        c.err("checkout", "ожидается абсолютный путь")
    cfg["checkout"] = checkout

    base = c.string(data.get("base_branch", OPTIONAL_DEFAULTS["base_branch"]), "base_branch")
    if (base.startswith(("-", "/", ".")) or base.endswith(("/", ".", ".lock")) or base == "@"
            or _BRANCH_BAD.search(base) or any(p.startswith(".") or p.endswith(".lock")
                                                for p in base.split("/"))):
        c.err("base_branch", f"«{base}» не годится как имя ветки")
    cfg["base_branch"] = base
    cfg["fallback_model"] = c.string(data.get("fallback_model", OPTIONAL_DEFAULTS["fallback_model"]),
                                     "fallback_model")
    cfg["automerge"] = c.boolean(data.get("automerge", OPTIONAL_DEFAULTS["automerge"]), "automerge")
    for key in ("ctx_limit", "idle_minutes", "tick_seconds"):
        cfg[key] = c.positive_int(data.get(key, OPTIONAL_DEFAULTS[key]), key)
    sha = data.get("plan_sha256")
    if sha is not None:
        c.string(sha, "plan_sha256", _SHA256)
    cfg["plan_sha256"] = sha

    roles = data.get("roles", {})
    c.obj(roles, "roles", (), set(ROLE_DEFAULTS))
    cfg["roles"] = {role: c.string(roles.get(role, default), f"roles.{role}")
                    for role, default in ROLE_DEFAULTS.items()}

    raw_waves = data["waves"]
    if not isinstance(raw_waves, list):
        c.err("waves", f"ожидается list, получено {_tname(raw_waves)}")
    if not raw_waves:
        c.err("waves", "список не должен быть пустым")
    waves, seen = [], set()
    for i, w in enumerate(raw_waves):
        item = _check_wave(c, w, i, seen)
        seen.add(item["id"].lower())
        waves.append(item)
    _check_dependencies(c, waves)
    cfg["waves"] = waves

    cfg["run_dir"] = pathlib.Path(path).absolute().parent / "runs" / cfg["run_id"]
    cfg["tmux_prefix"] = f"wab-{cfg['chain']}-"
    return cfg
