#!/usr/bin/env python3
"""SessionStart: workspace 하위 레포의 repo-map을 생성·캐시해 컨텍스트로 주입.

- git 레포: 스택·스크립트·디렉토리·git 상태 요약. 레포에 AGENTS.md/CLAUDE.md가 있으면
  구조는 그 문서가 담당하므로 트리·스크립트를 줄인 lean 모드로 주입한다(토큰 절약).
- git 레포가 아닌 그룹 디렉토리(예: linkive/): 트리 대신 하위 레포의 브랜치·변경 요약.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import time

import harness_lib as lib

TREE_EXCLUDES = {"node_modules", ".next", "dist", "build", ".turbo",
                 "coverage", ".vercel", "out", ".idea"}
KNOWN_DEPS = ("next", "react", "react-native", "vite", "webpack",
              "@nestjs/core", "express", "typescript")
MAX_AGE_SECONDS = 7 * 24 * 3600
GROUP_MAX_AGE_SECONDS = 15 * 60
MAX_TREE_LINES = 60
MAX_CHILDREN_PER_DIR = 8
MAX_STATUS_LINES = 12
MAX_GROUP_ROWS = 12
GROUP_BUDGET_SECONDS = 6.0
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


def git(repo, *args, timeout=5):
    try:
        proc = subprocess.run(
            ["git", "-C", repo] + list(args),
            capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


def git_root(cwd):
    """git 루트. 레포가 아니면 빈 문자열."""
    return git(cwd, "rev-parse", "--show-toplevel")


def repo_root(cwd):
    return git_root(cwd) or cwd


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cache_path(root):
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    return os.path.join(lib.MAP_CACHE_DIR, digest + ".json")


def load_cached_map(root, fingerprint, max_age=MAX_AGE_SECONDS):
    entry = read_json(cache_path(root))
    cached_fingerprint = entry.get("fingerprint", entry.get("head", ""))
    if cached_fingerprint == fingerprint and time.time() - entry.get("time", 0) < max_age:
        return entry.get("map")
    return None


def save_cached_map(root, fingerprint, text):
    os.makedirs(lib.MAP_CACHE_DIR, exist_ok=True)
    with open(cache_path(root), "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fingerprint, "time": time.time(), "map": text}, f,
                  ensure_ascii=False)


def repo_fingerprint(root):
    """컨텍스트에 영향을 주는 git/메타데이터 변경을 캐시 키에 포함한다.

    build_map()이 전체 git 상태를 보여 주므로 fingerprint도 같은 범위여야 한다. 일부
    디렉토리만 보면 루트 src/ 같은 변경이 최대 7일 동안 캐시에 가려질 수 있다.
    """
    pieces = [
        git(root, "rev-parse", "HEAD"),
        git(root, "branch", "--show-current"),
        git(root, "status", "--porcelain"),
    ]
    metadata = [
        os.path.join(root, "package.json"),
        os.path.join(root, "AGENTS.md"),
        os.path.join(root, "CLAUDE.md"),
    ]
    metadata += sorted(glob.glob(os.path.join(root, "apps", "*", "package.json")))
    metadata += sorted(glob.glob(os.path.join(root, "packages", "*", "package.json")))
    metadata += [os.path.join(root, name) for name in
                 ("pnpm-lock.yaml", "package-lock.json", "yarn.lock")]
    for path in metadata:
        try:
            stat = os.stat(path)
            pieces.append("%s:%d:%d" % (os.path.relpath(path, root), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return hashlib.sha256("\n".join(pieces).encode("utf-8")).hexdigest()


def detect_package_manager(root):
    if os.path.exists(os.path.join(root, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(root, "package-lock.json")):
        return "npm"
    if os.path.exists(os.path.join(root, "yarn.lock")):
        return "yarn"
    return ""


def stack_lines(root, pkg):
    lines = []
    pm = detect_package_manager(root)
    if pm:
        lines.append("- 패키지 매니저: " + pm)
    deps = {}
    deps.update(pkg.get("dependencies") or {})
    deps.update(pkg.get("devDependencies") or {})
    found = ["%s@%s" % (k, deps[k]) for k in KNOWN_DEPS if k in deps]
    if found:
        lines.append("- 주요 스택: " + ", ".join(found))
    return lines


def scripts_lines(root, pkg, limit=12):
    lines = []

    def add(label, scripts):
        if scripts:
            names = ", ".join("`%s`" % n for n in list(scripts)[:limit])
            lines.append("- %s 스크립트: %s" % (label, names))

    add("루트", pkg.get("scripts") or {})
    subs = sorted(
        glob.glob(os.path.join(root, "apps", "*", "package.json"))
        + glob.glob(os.path.join(root, "packages", "*", "package.json")))
    for sub in subs:
        rel = os.path.relpath(os.path.dirname(sub), root)
        add(rel, read_json(sub).get("scripts") or {})
    return lines


VERIFY_SCRIPT_NAMES = ("typecheck", "type-check", "tsc", "lint", "test", "build", "check")


def verify_lines(root, pkg):
    """계약 작성용 검증 명령 후보 한 줄. 레포를 읽지 않는 Claude가 verification_commands를 추측하지 않게 한다."""
    commands = []
    pm = detect_package_manager(root) or "npm"
    runner = pm if pm != "npm" else "npm run"
    scripts = pkg.get("scripts") or {}
    for name in VERIFY_SCRIPT_NAMES:
        if name in scripts:
            commands.append("%s %s" % (runner, name))
    if not scripts:
        if any(os.path.exists(os.path.join(root, f))
               for f in ("pytest.ini", "conftest.py", "setup.cfg")) \
                or glob.glob(os.path.join(root, "test_*.py")) \
                or os.path.isdir(os.path.join(root, "tests")):
            commands.append("python3 -m pytest -q")
        if os.path.exists(os.path.join(root, "go.mod")):
            commands.append("go build ./... && go test ./...")
        if os.path.exists(os.path.join(root, "Cargo.toml")):
            commands.append("cargo check && cargo test")
    if not commands:
        return []
    return ["- 검증 명령 후보: " + ", ".join("`%s`" % c for c in commands[:4])]


def tree_lines(root, max_depth=2):
    lines = []
    base = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, _ in os.walk(root):
        depth = dirpath.count(os.sep) - base
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in TREE_EXCLUDES and not d.startswith("."))
        if depth >= max_depth:
            dirnames[:] = []
            continue
        shown = dirnames[:MAX_CHILDREN_PER_DIR]
        hidden = len(dirnames) - len(shown)
        for d in shown:
            rel = os.path.relpath(os.path.join(dirpath, d), root)
            lines.append("- %s/" % rel)
            if len(lines) >= MAX_TREE_LINES:
                lines.append("- … (생략)")
                return lines
        if hidden > 0:
            rel_parent = os.path.relpath(dirpath, root)
            prefix = "" if rel_parent == "." else rel_parent + "/"
            lines.append("- %s… (+%d dirs)" % (prefix, hidden))
        dirnames[:] = shown  # 표시 안 된 디렉토리는 하위 탐색도 생략
    return lines


HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
CONTEXT_DIR = os.path.realpath(os.path.expanduser(os.environ.get(
    "WORKSPACE_HARNESS_CONTEXT_DIR",
    os.path.join(HARNESS_DIR, "context", "local"),
)))


STALE_COMMITS_THRESHOLD = 30
MAX_DEEP_CONTEXT_LINES = 14
MAX_DEEP_CONTEXT_CHARS = 2200
DEEP_PRIORITY_RE = re.compile(
    r"완료 기준|브랜치|\bmain\b|\bdev\b|\bpush\b|\bPR\b|배포|금지|주의|함정|worktree|private",
    re.IGNORECASE,
)


def deep_context_path(root):
    return os.path.join(CONTEXT_DIR, os.path.basename(root) + ".md")


def deep_context(root):
    """수동 관리 심층 컨텍스트(context/<레포명>.md). 없으면 None. 캐시와 무관하게 매번 읽음."""
    try:
        with open(deep_context_path(root), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def summarize_deep_context(root, content):
    """항상 필요한 안전·검증 정보만 주입하고 상세 아키텍처는 필요할 때 읽게 한다."""
    lines = [line.strip() for line in (content or "").splitlines()]
    selected = []

    def add(line):
        line = line.strip()
        if not line or line in selected:
            return
        if len(selected) >= MAX_DEEP_CONTEXT_LINES:
            return
        if sum(len(item) + 1 for item in selected) + len(line) > MAX_DEEP_CONTEXT_CHARS:
            return
        selected.append(line)

    # 제목 다음의 한 줄 정의는 레포 정체성을 잡는 데 유용하다.
    for line in lines:
        if line and not line.startswith("#"):
            add(line)
            break

    # 완료 기준은 뒤따르는 실제 명령 줄까지 먼저 보존한다.
    for index, line in enumerate(lines):
        if "완료 기준" not in line:
            continue
        add(line)
        for following in lines[index + 1:index + 4]:
            if following.startswith("#") or following.startswith("**"):
                break
            add(following)

    for line in lines:
        if DEEP_PRIORITY_RE.search(line):
            add(line)

    pointer = deep_context_path(root)
    return "\n".join([
        "### 레포 심층 컨텍스트 요약",
        "- 수동 요약이므로 현재 코드·workflow와 다르면 실제 구현을 우선한다.",
        *selected,
        "- 전체 문서(관련 있을 때만 읽기): `%s`" % pointer,
    ])


def staleness_warning(root, doc_path):
    """문서 작성(mtime) 이후 커밋이 임계치 이상 쌓였으면 경고 문자열, 아니면 None."""
    try:
        mtime = int(os.path.getmtime(doc_path))
    except OSError:
        return None
    count_str = git(root, "rev-list", "--count", "--since=@%d" % mtime, "HEAD")
    try:
        count = int(count_str)
    except ValueError:
        return None
    if count < STALE_COMMITS_THRESHOLD:
        return None
    return ("⚠️ 심층 컨텍스트 문서 이후 커밋 %d개 — 코드와 어긋나면 코드를 믿고, "
            "사용자에게 문서 재생성을 제안할 것." % count)


def has_instruction_file(root):
    return any(os.path.exists(os.path.join(root, name)) for name in INSTRUCTION_FILES)


def pointer_lines(root):
    lines = []
    has_agents = os.path.exists(os.path.join(root, "AGENTS.md"))
    has_claude = os.path.exists(os.path.join(root, "CLAUDE.md"))
    if has_agents:
        lines.append("- 레포 AGENTS.md 있음 — 공통 에이전트 규칙과 검증 명령을 우선 준수")
    if has_claude and has_agents:
        lines.append("- 레포 CLAUDE.md도 있음 — Claude 전용 보강일 수 있으며 Codex는 AGENTS.md와 자동 병합하지 않음")
    elif has_claude:
        lines.append("- 레포 CLAUDE.md 있음 — Claude 규칙이며 Codex도 configured fallback으로 사용")
    if os.path.isdir(os.path.join(root, "docs")):
        lines.append("- docs/ 디렉토리 있음 — 설계·계획 문서 참고")
    return lines


def build_map(root):
    pkg = read_json(os.path.join(root, "package.json"))
    lean = has_instruction_file(root)
    parts = ["## repo-map: %s (%s)" % (os.path.basename(root), root), ""]
    parts += stack_lines(root, pkg)
    parts += pointer_lines(root)
    parts += scripts_lines(root, pkg, limit=6 if lean else 12)
    parts += verify_lines(root, pkg)
    # 지침 파일이 구조를 설명하는 레포는 최상위 디렉토리만 보여 준다.
    tree = tree_lines(root, max_depth=1 if lean else 2)
    if tree:
        parts += ["", "### 디렉토리 (%d depth)" % (1 if lean else 2)] + tree
    status = git(root, "status", "--short", "--branch")
    if status:
        lines = status.splitlines()
        shown = ["- " + c for c in lines[:MAX_STATUS_LINES]]
        if len(lines) > MAX_STATUS_LINES:
            shown.append("- … 외 %d개" % (len(lines) - MAX_STATUS_LINES))
        parts += ["", "### 현재 git 상태"] + shown
    parts += ["", "검증 명령은 AGENTS.md/CLAUDE.md를 우선하고, 없으면 위 package script를 확인할 것."]
    return "\n".join(parts)


def group_rows(directory, budget_seconds=GROUP_BUDGET_SECONDS):
    """하위 git 레포별 브랜치·최근 커밋·변경 수. 최근 커밋순, 시간 예산 안에서만."""
    started = time.monotonic()

    def budgeted_git(path, *args):
        remaining = budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return None
        return git(path, *args, timeout=min(2, remaining))

    rows = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    truncated = False
    for name in names:
        path = os.path.join(directory, name)
        if name.startswith(".") or name in TREE_EXCLUDES:
            continue
        if not os.path.exists(os.path.join(path, ".git")):
            continue
        if time.monotonic() - started >= budget_seconds:
            truncated = True
            break
        branch = budgeted_git(path, "branch", "--show-current")
        last = budgeted_git(path, "log", "-1", "--format=%cs")
        dirty = budgeted_git(path, "status", "--porcelain")
        if None in (branch, last, dirty):
            truncated = True
            break
        branch = branch or "(detached)"
        dirty_count = len(dirty.splitlines()) if dirty else 0
        rows.append((last, "- %s/ — %s, 최근 커밋 %s%s" % (
            name, branch, last or "?",
            ", 변경 %d개" % dirty_count if dirty_count else "")))
    rows.sort(key=lambda row: row[0], reverse=True)
    lines = [text for _, text in rows[:MAX_GROUP_ROWS]]
    if truncated or len(rows) > MAX_GROUP_ROWS:
        lines.append("- … (나머지 생략)")
    return lines


def build_group_map(directory):
    rows = group_rows(directory)
    if not rows:
        return ""
    return "\n".join([
        "## 디렉토리 그룹: %s (%s) — git 레포 아님, 하위 레포 요약" % (
            os.path.basename(directory), directory),
        "",
        *rows,
        "",
        "작업할 레포를 정해 그 경로를 기준으로 진행할 것. 레포별 규칙은 그 레포의 AGENTS.md/CLAUDE.md.",
    ])


def should_inject(root):
    """workspace 루트 자체는 주입 생략 — CLAUDE.md가 이미 커버하고, 전체 트리는 잡음."""
    return os.path.realpath(root) != os.path.realpath(lib.WORKSPACE_ROOT)


def main():
    data = lib.read_hook_input()
    cwd = data.get("cwd", "")
    session_id = data.get("session_id", "")
    if not lib.in_workspace(cwd):
        return
    root = git_root(cwd)
    if not root:
        directory = os.path.realpath(cwd)
        if not should_inject(directory):
            return
        text = load_cached_map(directory, "group", GROUP_MAX_AGE_SECONDS)
        if text is None:
            text = build_group_map(directory)
            save_cached_map(directory, "group", text)
        if text:
            lib.event("map-inject", session_id, "%s:%d" % (os.path.basename(directory), len(text)))
            print(lib.hook_output("SessionStart", text))
        return
    root = os.path.realpath(root)
    if not should_inject(root):
        return
    fingerprint = repo_fingerprint(root)
    text = load_cached_map(root, fingerprint)
    if text is None:
        text = build_map(root)
        save_cached_map(root, fingerprint, text)
        lib.event("map-generate", session_id, root)
    deep = deep_context(root)
    if deep:
        warning = staleness_warning(root, deep_context_path(root))
        summary = summarize_deep_context(root, deep)
        if warning:
            summary = warning + "\n\n" + summary
        text = text + "\n\n" + summary
    lib.event("map-inject", session_id, "%s:%d" % (os.path.basename(root), len(text)))
    print(lib.hook_output("SessionStart", text))


if __name__ == "__main__":
    lib.run_fail_open(main)
