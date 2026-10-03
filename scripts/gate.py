"""wave-autobot, гейт мерджа: факты GitHub/git о PR волны и чистое решение wait/fail/pass/merged.

Сбор фактов (`collect_facts`) ходит в `gh` и `git` только через инъецируемый
`run(argv) -> (returncode, stdout, stderr)`, поэтому тесты подменяют его таблицей ответов, а
реальные gh/git не вызываются. Решение (`decide`) — чистая функция над словарём фактов, без I/O:
её легко проверять и невозможно «случайно» заставить смержить по побочному эффекту.

Любая ошибка сбора — `GateError` с усечённым текстом; вызывающий трактует её как wait с причиной
(сеть, rate limit, временный сбой gh не должны превращаться ни в мердж, ни в провал волны).
redact текста ошибок делает вызывающий (wab.py), здесь — только усечение.
Модуль не импортирует wab: wab импортирует отсюда `plan_problem` и `PLAN_MAX_BYTES`.
Импорт модуля ничего не запускает и не создаёт файлов.
"""
import hashlib
import json
import os
import stat
import subprocess

PLAN_MAX_BYTES = 1024 * 1024   # waves.md крупнее мегабайта — не план
RUN_TIMEOUT_SECONDS = 60       # один вызов gh/git дольше — считаем сбоем сбора, а не ждём вечно
ERROR_LIMIT = 300              # длина текста ошибки в причине вердикта
NAMES_LIMIT = 5                # сколько имён check-runs перечислять в причине
PLAN_PREFIX = "plan changed since approval: "
PR_FIELDS = ("number,state,headRefOid,isDraft,url,mergeCommit,headRepository,"
             "headRepositoryOwner,mergedAt")


class GateError(Exception):
    """Факт не собран (gh/git упал, таймаут, невалидный ответ). Для гейта это wait с причиной."""


class DuplicatePR(GateError):
    """У ветки волны два и больше открытых PR в основном репозитории: какой мерджить — решает человек."""


def _cut(text, limit=ERROR_LIMIT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def default_run(argv):
    """subprocess без shell; таймаут и OSError (нет gh/git) — GateError, а не исключение наружу."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise GateError(f"{argv[0]}: нет ответа за {RUN_TIMEOUT_SECONDS} с")
    except OSError as e:
        raise GateError(_cut(f"{argv[0]} не запустился: {e.strerror or e}"))
    return p.returncode, p.stdout, p.stderr


def _call(run, argv):
    """Вызов через run с единым превращением сбоев в GateError (подменённый run тоже может бросить)."""
    try:
        rc, out, err = run(argv)
    except GateError:
        raise
    except subprocess.TimeoutExpired:
        raise GateError(f"{argv[0]}: нет ответа за {RUN_TIMEOUT_SECONDS} с")
    except OSError as e:
        raise GateError(_cut(f"{argv[0]} не запустился: {e.strerror or e}"))
    if rc != 0:
        what = " ".join(argv[:3])
        raise GateError(_cut(f"{what}: код {rc}: {(err or out or '').strip() or 'без текста'}"))
    return out or ""


def _json(text, what):
    try:
        return json.loads(text)
    except ValueError as e:
        raise GateError(_cut(f"{what}: невалидный JSON ({e})"))


# ---------- PR ветки ----------

def _same_repo(p, repo):
    """PR из основного репозитория, а не из форка с одноимённой веткой (--head матчит только имя ветки)."""
    owner = ((p.get("headRepositoryOwner") or {}).get("login") or "")
    name = ((p.get("headRepository") or {}).get("name") or "")
    return bool(owner and name) and f"{owner}/{name}".lower() == repo.lower()


def find_pr(repo, branch, base, run=default_run):
    """PR ветки волны в repo: один OPEN → он; нет OPEN → самый свежий MERGED; иначе CLOSED; ничего → None.

    PR из форков отбрасываются. Два и больше OPEN — DuplicatePR (гейт: fail).
    """
    out = _call(run, ["gh", "pr", "list", "--repo", repo, "--head", branch, "--base", base,
                      "--state", "all", "--limit", "100", "--json", PR_FIELDS])
    data = _json(out, "gh pr list")
    if not isinstance(data, list) or not all(isinstance(p, dict) for p in data):
        raise GateError("gh pr list: ожидался список PR")
    own = [p for p in data if _same_repo(p, repo)]
    opened = [p for p in own if p.get("state") == "OPEN"]
    if len(opened) > 1:
        nums = ", ".join(f"#{p.get('number')}" for p in opened[:NAMES_LIMIT])
        raise DuplicatePR(f"два открытых PR ветки {branch}: {nums}")
    if opened:
        return opened[0]
    merged = [p for p in own if p.get("state") == "MERGED"]
    if merged:
        # ISO-8601 в UTC сравнивается строкой; номер — на случай пустого mergedAt
        return max(merged, key=lambda p: (p.get("mergedAt") or "", p.get("number") or 0))
    closed = [p for p in own if p.get("state") == "CLOSED"]
    if closed:
        return max(closed, key=lambda p: p.get("number") or 0)
    return None


def head_still(repo, number, sha, run=default_run):
    """Свежие headRefOid/state/isDraft PR прямо перед мерджем (sha — ожидаемый, сверяет вызывающий).

    Между сбором фактов и мерджем в ветку мог прийти push: мерджить надо ровно проверенный HEAD.
    """
    out = _call(run, ["gh", "pr", "view", str(number), "--repo", repo, "--json", "headRefOid,state,isDraft"])
    data = _json(out, "gh pr view")
    if not isinstance(data, dict) or not data.get("headRefOid") or not data.get("state"):
        raise GateError(f"gh pr view #{number}: нет headRefOid/state в ответе")
    return {"headRefOid": data["headRefOid"], "state": data["state"], "isDraft": bool(data.get("isDraft")),
            "same_head": data["headRefOid"] == sha}


# ---------- check-runs ----------

def _objects(text):
    """Конкатенация JSON-объектов (вывод `gh api --paginate`) → список; мусор между ними — GateError."""
    dec, i, n, out = json.JSONDecoder(), 0, len(text), []
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return out
        try:
            obj, i = dec.raw_decode(text, i)
        except ValueError as e:
            raise GateError(_cut(f"gh api check-runs: невалидный JSON ({e})"))
        out.append(obj)


def check_runs(repo, sha, run=default_run):
    """Все check-runs коммита (name, status, conclusion) по всем страницам.

    Число собранных сверяется с total_count первой страницы: неполный список мог бы потерять
    красный run и дать ложный pass, поэтому расхождение — GateError (гейт ждёт).
    """
    out = _call(run, ["gh", "api", "--paginate", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"])
    pages = _objects(out)
    if not pages:
        raise GateError("gh api check-runs: пустой ответ")
    runs = []
    for p in pages:
        if not isinstance(p, dict) or not isinstance(p.get("check_runs"), list):
            raise GateError("gh api check-runs: страница без check_runs")
        for r in p["check_runs"]:
            if not isinstance(r, dict):
                raise GateError("gh api check-runs: запись не объект")
            runs.append({"name": str(r.get("name") or "?"), "status": r.get("status"),
                         "conclusion": r.get("conclusion")})
    total = pages[0].get("total_count")
    if not isinstance(total, int) or total != len(runs):
        raise GateError(f"check-runs на {sha[:12]}: собрано {len(runs)}, а total_count {total}")
    return runs


# ---------- план и рабочая копия ----------

def plan_issue(cfg):
    """Пин плана без побочных эффектов: None или (причина, короткая причина для журнала).

    plan_sha256 не задан — None (без проверки; предупреждение печатает только wab.check_plan).
    Симлинк, FIFO, каталог не открываются как план: O_NOFOLLOW и O_NONBLOCK (FIFO без писателя
    иначе вешает open), тип проверяется по fstat уже открытого дескриптора.
    """
    want = cfg.get("plan_sha256")
    if not want:
        return None
    path = cfg["plan_path"]
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return "файла waves.md нет", None
    except OSError as e:
        return f"waves.md не открыт (симлинк или нет доступа): {e.strerror or e}", None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "waves.md не обычный файл", None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(PLAN_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > PLAN_MAX_BYTES:
        return f"waves.md больше {PLAN_MAX_BYTES // (1024 * 1024)} МБ", None
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        return (f"sha256 waves.md не совпадает с plan_sha256 (в конфиге {want[:12]}…, в файле {got[:12]}…)",
                "sha256 waves.md не совпадает с plan_sha256")
    return None


def plan_problem(cfg):
    """None, если план совпадает с пином (или пин не задан), иначе текст причины."""
    issue = plan_issue(cfg)
    return issue[0] if issue else None


def local_facts(cwd, run=default_run):
    """(HEAD рабочей копии волны, дерево чистое?). Неотслеживаемые файлы тоже делают дерево грязным."""
    head = _call(run, ["git", "-C", str(cwd), "rev-parse", "HEAD"]).strip()
    if not head:
        raise GateError(f"git rev-parse HEAD в {cwd}: пустой ответ")
    porcelain = _call(run, ["git", "-C", str(cwd), "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    return head, porcelain == ""


def collect_facts(cfg, branch, cwd, run=default_run, base="main"):
    """Все факты для decide. Пин плана сверяется при КАЖДОМ сборе: план мог смениться посреди волны.

    Ключи: pr (dict|None), duplicate (текст|None), local_head, tree_clean, checks (список|None —
    PR нет, закрыт или смержен: для MERGED check-runs не нужны), plan (None|причина), errors.
    Для MERGED локальные факты (HEAD, дерево) не собираются: local_head и tree_clean остаются None.
    """
    facts = {"pr": None, "duplicate": None, "local_head": None, "tree_clean": None, "checks": None,
             "plan": plan_problem(cfg), "errors": []}
    try:
        facts["pr"] = find_pr(cfg["repo"], branch, base, run)
    except DuplicatePR as e:
        facts["duplicate"] = str(e)
    except GateError as e:
        facts["errors"].append(str(e))
    p = facts["pr"]
    if p and p.get("state") == "MERGED":
        # смержено на GitHub: HEAD и дерево рабочей копии волны уже ничего не решают, а сломанный
        # локальный git не должен держать волну в wait (пин плана выше при этом в силе)
        return facts
    try:
        facts["local_head"], facts["tree_clean"] = local_facts(cwd, run)
    except GateError as e:
        facts["errors"].append(str(e))
    if p and p.get("state") == "OPEN" and p.get("headRefOid"):
        try:
            facts["checks"] = check_runs(cfg["repo"], p["headRefOid"], run)
        except GateError as e:
            facts["errors"].append(str(e))
    return facts


# ---------- решение ----------

def decide(facts):
    """(verdict, reasons), verdict ∈ wait|fail|pass|merged. Чистая функция: только словарь фактов.

    Причины копятся по всем правилам; любой fail перекрывает wait (провал виден сразу, а не после
    того, как временная причина пройдёт). merged отдаётся, только если нет ни fail, ни wait.
    Успех строгий: conclusion ровно "success"; neutral/skipped/cancelled — fail.
    """
    fails, waits = [], []
    if facts.get("plan"):
        fails.append(PLAN_PREFIX + facts["plan"])
    waits.extend(facts.get("errors") or [])
    if facts.get("duplicate"):
        fails.append(facts["duplicate"])
    p = facts.get("pr")
    merged = False
    if p is None:
        if not facts.get("duplicate") and not facts.get("errors"):
            waits.append("PR ветки не найден")
    elif p.get("state") == "MERGED":
        merged = True
    elif p.get("state") != "OPEN":
        fails.append(f"PR #{p.get('number')} закрыт без мерджа")
    else:
        if facts.get("tree_clean") is False:
            fails.append("в рабочей копии волны незакоммиченные изменения")
        head, want = facts.get("local_head"), p.get("headRefOid") or ""
        if head and head != want:
            waits.append(f"HEAD волны {head[:12]} ≠ headRefOid PR {want[:12]} (push не дошёл или HEAD сменился)")
        checks = facts.get("checks")
        if checks is not None:
            if not checks:
                waits.append(f"нет check-runs на {want[:12]}")
            pending = [c["name"] for c in checks if c.get("status") != "completed"]
            bad = [f"{c['name']}={c.get('conclusion')}" for c in checks
                   if c.get("status") == "completed" and c.get("conclusion") != "success"]
            if pending:
                more = f" и ещё {len(pending) - NAMES_LIMIT}" if len(pending) > NAMES_LIMIT else ""
                waits.append("check-runs не завершены: " + ", ".join(pending[:NAMES_LIMIT]) + more)
            if bad:
                more = f" и ещё {len(bad) - NAMES_LIMIT}" if len(bad) > NAMES_LIMIT else ""
                fails.append("check-runs не успешны: " + ", ".join(bad[:NAMES_LIMIT]) + more)
    if fails:
        return "fail", fails
    if waits:
        return "wait", waits
    if merged:
        return "merged", [f"PR #{p.get('number')} уже смержен"]
    if p is None or facts.get("checks") is None:   # защитно: pass только при собранных check-runs
        return "wait", ["факты неполны: нет PR или check-runs"]
    return "pass", []
