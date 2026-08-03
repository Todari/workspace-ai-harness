#!/usr/bin/env python3
"""저장소의 공통 지침을 workspace 로컬 Claude/Codex 문서에 동기화."""
import os
import re

import harness_lib as lib


HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_SOURCE = os.path.join(HARNESS_DIR, "context", "local", "workspace.md")
EXAMPLE_SOURCE = os.path.join(HARNESS_DIR, "context", "workspace.example.md")
SOURCE = os.path.realpath(os.path.expanduser(os.environ.get(
    "WORKSPACE_HARNESS_CONTEXT_FILE",
    LOCAL_SOURCE if os.path.exists(LOCAL_SOURCE) else EXAMPLE_SOURCE,
)))
TARGETS = (
    os.path.join(lib.WORKSPACE_ROOT, "CLAUDE.md"),
    os.path.join(lib.WORKSPACE_ROOT, "AGENTS.md"),
)
BEGIN = "<!-- BEGIN workspace-harness: synced from context/workspace.md -->"
LEGACY_BEGIN_RE = re.compile(
    r"<!-- BEGIN workspace-harness: synced from .+?/CLAUDE\.md -->"
)
END = "<!-- END workspace-harness -->"


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


def main():
    with open(SOURCE, encoding="utf-8") as f:
        content = f.read()
    changed = [target for target in TARGETS if sync_target(target, content)]
    if not changed:
        print("workspace CLAUDE.md and AGENTS.md already synchronized")
        return
    print("synchronized: %s" % ", ".join(changed))


if __name__ == "__main__":
    main()
