#!/usr/bin/env python3
"""저장소의 공통 지침을 Claude/Codex 지침 문서에 동기화.

대상:
- workspace `CLAUDE.md` — Claude Code는 부모 디렉토리의 CLAUDE.md를 상속하므로 레포 세션에서도 보인다.
- workspace `AGENTS.md` — Codex는 git 루트 위의 AGENTS.md를 읽지 않으므로 workspace 루트 세션에서만 보인다.
- `~/.codex/AGENTS.md` — Codex 전역 지침. 레포 세션의 Codex가 공통 규칙을 보는 유일한 경로.

원본의 `<!-- workspace-harness: repo-table -->` 줄은 repos.json으로 만든 레포 표로 치환된다.
"""
import os
import re

import harness_lib as lib
import repos


HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_SOURCE = os.path.join(HARNESS_DIR, "context", "local", "workspace.md")
EXAMPLE_SOURCE = os.path.join(HARNESS_DIR, "context", "workspace.example.md")
SOURCE = os.path.realpath(os.path.expanduser(os.environ.get(
    "WORKSPACE_HARNESS_CONTEXT_FILE",
    LOCAL_SOURCE if os.path.exists(LOCAL_SOURCE) else EXAMPLE_SOURCE,
)))
CODEX_HOME = os.path.realpath(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")))
CODEX_GLOBAL = os.path.join(CODEX_HOME, "AGENTS.md")
TARGETS = (
    os.path.join(lib.WORKSPACE_ROOT, "CLAUDE.md"),
    os.path.join(lib.WORKSPACE_ROOT, "AGENTS.md"),
) + ((CODEX_GLOBAL,) if os.path.isdir(CODEX_HOME) else ())
BEGIN = "<!-- BEGIN workspace-harness: synced from context/workspace.md -->"
LEGACY_BEGIN_RE = re.compile(
    r"<!-- BEGIN workspace-harness: synced from .+?/CLAUDE\.md -->"
)
END = "<!-- END workspace-harness -->"
REPO_TABLE_PLACEHOLDER = "<!-- workspace-harness: repo-table -->"


def render_source(content):
    """원본의 레포 표 자리표시자를 레지스트리 표로 치환한다. 레지스트리가 없으면 줄을 지운다."""
    if REPO_TABLE_PLACEHOLDER not in content:
        return content
    table = repos.markdown_table()
    return content.replace(REPO_TABLE_PLACEHOLDER, table)


def replace_managed_block(current, content):
    block = "%s\n%s\n%s" % (BEGIN, content.rstrip(), END)
    if not current:
        return block + "\n"
    legacy = LEGACY_BEGIN_RE.search(current)
    starts = [pos for pos in (current.find(BEGIN), legacy.start() if legacy else -1)
              if pos >= 0]
    start = min(starts) if starts else -1
    finish = current.find(END, start) if start >= 0 else -1
    if (start >= 0) != (finish >= 0) or (start < 0 and END in current):
        raise ValueError("workspace-harness managed block markers are unbalanced")
    if start >= 0 and finish >= 0:
        finish += len(END)
        return (current[:start] + block + current[finish:]).rstrip() + "\n"
    # v3 최초 마이그레이션: 이전 동기화 결과가 source와 같으면 관리 블록으로 승격한다.
    if current.strip() == content.strip():
        return block + "\n"
    return current.rstrip() + "\n\n" + block + "\n"


def sync_target(target, content):
    content = target_content(target, content)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    current = None
    try:
        with open(target, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        pass
    updated = replace_managed_block(current, content)
    if current == updated:
        return False
    with open(target, "w", encoding="utf-8") as f:
        f.write(updated)
    return True


def rendered_source():
    with open(SOURCE, encoding="utf-8") as f:
        return render_source(f.read())


def target_content(target, content):
    """Codex global already carries the rules; don't inject them twice at workspace root."""
    if (os.path.realpath(target) == os.path.join(lib.WORKSPACE_ROOT, "AGENTS.md")
            and CODEX_GLOBAL in TARGETS):
        return ("# Workspace 진입 안내\n\n"
                "공통 규칙은 Codex 전역 AGENTS.md에 한 번만 둔다. 해당 규칙이 현재 문맥에 "
                "없으면 이 디렉터리의 CLAUDE.md를 읽는다. 레포별 지침은 해당 레포에서 확인한다.\n")
    return content


def is_synced(target, content=None):
    """대상의 관리 블록이 현재 원본과 같으면 True (doctor용, 쓰지 않음)."""
    content = rendered_source() if content is None else content
    content = target_content(target, content)
    try:
        with open(target, encoding="utf-8") as f:
            current = f.read()
    except OSError:
        return False
    try:
        return replace_managed_block(current, content) == current
    except ValueError:
        return False


def main():
    content = rendered_source()
    changed = [target for target in TARGETS if sync_target(target, content)]
    if not changed:
        print("instruction files already synchronized: %s" % ", ".join(TARGETS))
        return
    print("synchronized: %s" % ", ".join(changed))


if __name__ == "__main__":
    main()
