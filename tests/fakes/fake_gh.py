"""Подставной `gh` для офлайн-сквозного теста (tests/test_e2e_offline.py).

Отвечает ровно на те вызовы, что делают gate.collect_facts, wab._automerge и агент волны:
  gh pr list --repo R --head BR --base B --state all --limit N --json FIELDS
  gh api --paginate repos/R/commits/SHA/check-runs?per_page=100
  gh pr view N --repo R --json headRefOid,state,isDraft
  gh pr ready N --repo R
  gh pr merge N --repo R --squash --match-head-commit SHA
  gh pr create --base B --head BR --title T --body TEXT [--draft]
Всё остальное — код 1 и запись в errors.log (тест требует пустой errors.log).

Состояние — JSON в каталоге управления (ctl/gh-state.json), им управляет тест. headRefOid открытого
PR — настоящий sha ветки в bare-репозитории origin. `pr merge` делает настоящий squash-коммит в
main bare-репозитория (git plumbing: commit-tree + update-ref) и переводит PR в MERGED.
Каждый вызов пишется строкой JSON в ctl/gh.log (тест считает `pr merge`).

Запуск: fake_gh.py --fake-ctl <каталог> <аргументы gh>. Модуль импортируется тестом: merge_pr()
мерджит PR «как человек» — без записи в gh.log.
"""
import contextlib
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import time

FIELDS_ALL = ("number", "state", "headRefOid", "isDraft", "url", "mergeCommit", "headRepository",
              "headRepositoryOwner", "mergedAt")
GIT_ENV = {"GIT_AUTHOR_NAME": "fake-gh", "GIT_AUTHOR_EMAIL": "fake-gh@example.com",
           "GIT_COMMITTER_NAME": "fake-gh", "GIT_COMMITTER_EMAIL": "fake-gh@example.com"}


class Fail(Exception):
    """Отказ вызова: текст уходит в stderr, код 1."""


@contextlib.contextmanager
def locked(ctl):
    fd = os.open(os.path.join(ctl, "gh.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def state_path(ctl):
    return pathlib.Path(ctl) / "gh-state.json"


def load(ctl):
    return json.loads(state_path(ctl).read_text(encoding="utf-8"))


def save(ctl, st):
    tmp = state_path(ctl).with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path(ctl))


def update(ctl, fn):
    """Тесту: изменить состояние под блокировкой (fn(st) правит словарь на месте)."""
    with locked(ctl):
        st = load(ctl)
        fn(st)
        save(ctl, st)


def git(st, *args):
    r = subprocess.run(["git", "--git-dir", st["origin"], *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, **GIT_ENV})
    if r.returncode != 0:
        raise Fail(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def head_of(st, pr):
    if pr["state"] == "MERGED":
        return pr["mergedHead"]
    try:
        return git(st, "rev-parse", "--verify", f"refs/heads/{pr['head']}^{{commit}}")
    except Fail:
        return None


def pr_json(st, pr):
    merged = pr["state"] == "MERGED"
    return {"number": pr["number"], "state": pr["state"], "headRefOid": head_of(st, pr) or "",
            "isDraft": pr["isDraft"],
            "url": f"https://github.com/{st['owner']}/{st['name']}/pull/{pr['number']}",
            "mergeCommit": {"oid": pr["mergeCommit"]} if merged else None,
            "headRepository": {"name": st["name"]}, "headRepositoryOwner": {"login": st["owner"]},
            "mergedAt": pr.get("mergedAt") if merged else None}


def find(st, number):
    for pr in st["prs"]:
        if str(pr["number"]) == str(number):
            return pr
    raise Fail(f"no pull requests found for {number}")


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def merge_pr(ctl, number, match_head=None, _locked=False):
    """Squash-мердж PR в main bare-репозитория. Тест зовёт напрямую — «человек нажал Merge»."""
    def do():
        st = load(ctl)
        pr = find(st, number)
        if pr["state"] != "OPEN":
            raise Fail(f"Pull request #{number} is not open ({pr['state']})")
        if pr["isDraft"]:
            raise Fail(f"Pull request #{number} is still a draft")
        head = head_of(st, pr)
        if head is None:
            raise Fail(f"ветки {pr['head']} нет в origin")
        if match_head and match_head != head:
            raise Fail(f"Head branch was modified. Review and try the merge again ({head[:12]})")
        base_ref = f"refs/heads/{pr['base']}"
        base = git(st, "rev-parse", "--verify", base_ref)
        r = subprocess.run(["git", "--git-dir", st["origin"], "merge-base", "--is-ancestor", base, head])
        if r.returncode != 0:
            raise Fail(f"Pull request #{number} is not mergeable: ветка отстала от {pr['base']}")
        tree = git(st, "rev-parse", f"{head}^{{tree}}")
        oid = git(st, "commit-tree", tree, "-p", base, "-m", f"{pr['title']} (#{number})")
        git(st, "update-ref", base_ref, oid, base)
        pr.update(state="MERGED", mergeCommit=oid, mergedHead=head,
                  mergedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        save(ctl, st)
        return oid
    if _locked:
        return do()
    with locked(ctl):
        return do()


def checks_pages(st, sha):
    runs = [{"name": c["name"], "status": c.get("status", "completed"),
             "conclusion": c.get("conclusion")} for c in st["checks"]]
    total = len(runs)
    if not runs:
        return [{"total_count": 0, "check_runs": []}]
    # по одному run на страницу: гейт обязан собрать все страницы и сверить total_count
    return [{"total_count": total, "check_runs": [r]} for r in runs]


def handle(ctl, args):
    st = load(ctl)
    if args[:2] == ["pr", "list"]:
        head, base = opt(args, "--head"), opt(args, "--base")
        fields = (opt(args, "--json") or "").split(",")
        out = []
        for pr in st["prs"]:
            if pr["head"] == head and (base is None or pr["base"] == base):
                full = pr_json(st, pr)
                out.append({k: full[k] for k in fields if k in full})
        return json.dumps(out)
    if args[:2] == ["pr", "view"]:
        pr = find(st, args[2])
        full = pr_json(st, pr)
        return json.dumps({k: full[k] for k in (opt(args, "--json") or "").split(",") if k in full})
    if args[:2] == ["pr", "ready"]:
        pr = find(st, args[2])
        pr["isDraft"] = False
        save(ctl, st)
        return f"✓ Pull request #{pr['number']} is marked as \"ready for review\""
    if args[:2] == ["pr", "merge"]:
        if "--squash" not in args:
            raise Fail("подставной gh умеет только --squash")
        merge_pr(ctl, args[2], opt(args, "--match-head-commit"), _locked=True)
        return ""
    if args[:2] == ["pr", "create"]:
        head, base = opt(args, "--head"), opt(args, "--base", "main")
        if not head:
            raise Fail("подставной gh: нужен --head")
        if any(p["head"] == head and p["state"] == "OPEN" for p in st["prs"]):
            raise Fail(f"a pull request for branch \"{head}\" already exists")
        number = st["next"]
        st["next"] += 1
        st["prs"].append({"number": number, "head": head, "base": base, "state": "OPEN",
                          "isDraft": "--draft" in args, "title": opt(args, "--title", head),
                          "mergeCommit": None, "mergedHead": None, "mergedAt": None})
        save(ctl, st)
        return f"https://github.com/{st['owner']}/{st['name']}/pull/{number}"
    if args[:1] == ["api"] and "--paginate" in args:
        path = next((a for a in args[1:] if a.startswith("repos/")), "")
        parts = path.split("?")[0].split("/")
        if len(parts) == 6 and parts[3] == "commits" and parts[5] == "check-runs":
            return "".join(json.dumps(p) for p in checks_pages(st, parts[4]))
    raise Fail("подставной gh: неизвестный вызов " + " ".join(args))


def main(argv):
    if len(argv) < 2 or argv[0] != "--fake-ctl":
        print("fake_gh: нужен --fake-ctl <каталог>", file=sys.stderr)
        return 2
    ctl, args = argv[1], argv[2:]
    with locked(ctl):
        with open(os.path.join(ctl, "gh.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": args, "caller": os.environ.get("FAKE_GH_CALLER", "")},
                               ensure_ascii=False) + "\n")
        try:
            out = handle(ctl, args)
        except Fail as e:
            if "неизвестный вызов" in str(e):
                with open(os.path.join(ctl, "errors.log"), "a", encoding="utf-8") as f:
                    f.write(f"gh: {e}\n")
            print(str(e), file=sys.stderr)
            return 1
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
