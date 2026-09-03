#!/usr/bin/env python3
"""레포 레지스트리(context/local/repos.json) — 규칙 표·볼트 매핑·회고 대상의 단일 소스.

    python3 repos.py list               # 등록된 레포
    python3 repos.py active [--days 7]  # 최근 N일 커밋이 있는 레포 (미등록 레포도 표시)
    python3 repos.py table              # CLAUDE.md/AGENTS.md에 들어갈 마크다운 표

레지스트리 항목: {"name", "path"(workspace 기준), "rule"(브랜치·배포 규칙 한 줄),
"vault_note"(볼트 노트 상대 경로, 선택), "aliases"(디렉토리명 별칭, 선택)}.
"""
import json
import os
import subprocess
import sys

import harness_lib as lib

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.realpath(os.path.expanduser(os.environ.get(
    "WORKSPACE_HARNESS_REPOS",
    os.path.join(HARNESS_DIR, "context", "local", "repos.json"))))
SCAN_DIRS = ("projects", "linkive", ".")


def load(path=None):
    try:
        with open(path or REGISTRY, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    repos = data.get("repos", []) if isinstance(data, dict) else data
    return [r for r in repos if isinstance(r, dict) and r.get("name") and r.get("path")]


def abs_path(repo):
    return os.path.join(lib.WORKSPACE_ROOT, repo["path"])


def by_dirname(repos=None):
    """git 루트 디렉토리명(별칭 포함) → 레포 항목."""
    index = {}
    for repo in (load() if repos is None else repos):
        index[os.path.basename(repo["path"].rstrip("/"))] = repo
        for alias in repo.get("aliases", []):
            index[alias] = repo
    return index


def vault_note_for(dirname, repos=None):
    """디렉토리명에 맞는 볼트 노트 경로. `forcletter-seo` 같은 워크트리 이름도 매칭."""
    index = by_dirname(repos)
    if dirname in index:
        return index[dirname].get("vault_note", "")
    for name, repo in index.items():
        if dirname.startswith(name + "-"):
            return repo.get("vault_note", "")
    return ""


def git(path, *args, timeout=5):
    try:
        proc = subprocess.run(["git", "-C", path] + list(args),
                              capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def commits_since(path, days):
    out = git(path, "rev-list", "--count", "--since=%d days ago" % days, "HEAD")
    try:
        return int(out)
    except ValueError:
        return 0


def scan_git_dirs():
    """workspace 바로 아래·projects·linkive의 git 레포 (realpath → 상대 경로)."""
    found = {}
    for rel in SCAN_DIRS:
        base = os.path.join(lib.WORKSPACE_ROOT, rel)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if name.startswith(".") or not os.path.exists(os.path.join(path, ".git")):
                continue
            found[os.path.realpath(path)] = os.path.relpath(path, lib.WORKSPACE_ROOT)
    return found


def active(days=7):
    registered = {os.path.realpath(abs_path(r)): r for r in load()}
    paths = dict(scan_git_dirs())
    for real, repo in registered.items():
        paths.setdefault(real, repo["path"])
    rows = []
    for real, rel in paths.items():
        count = commits_since(real, days)
        if count <= 0:
            continue
        repo = registered.get(real)
        rows.append({
            "name": repo["name"] if repo else os.path.basename(rel),
            "path": rel,
            "commits": count,
            "registered": bool(repo),
        })
    rows.sort(key=lambda row: (-row["commits"], row["path"]))
    return rows


def markdown_table(repos=None):
    """규칙이 있는 레포만 표에 넣는다 — 규칙 없는 행은 토큰만 쓴다."""
    rows = [r for r in (load() if repos is None else repos) if r.get("rule")]
    if not rows:
        return ""
    lines = ["| 레포 | 경로 | 기본 규칙 |", "|---|---|---|"]
    for repo in rows:
        lines.append("| %s | `%s` | %s |" % (repo["name"], repo["path"], repo["rule"]))
    return "\n".join(lines)


def main(argv):
    command = argv[1] if len(argv) > 1 else "list"
    if command == "list":
        for repo in load():
            print("%-18s %-32s %s" % (repo["name"], repo["path"], repo.get("vault_note", "")))
    elif command == "active":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
        for row in active(days):
            print("%-24s %-36s 커밋 %3d %s" % (
                row["name"], row["path"], row["commits"],
                "" if row["registered"] else "(미등록)"))
    elif command == "table":
        print(markdown_table())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
