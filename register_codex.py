#!/usr/bin/env python3
"""~/.codex/config.toml에 공용 workspace 하네스 훅을 멱등하게 등록."""
import hashlib
import os
import re


CONFIG = os.path.expanduser("~/.codex/config.toml")
HARNESS = os.path.expanduser("~/.claude/hooks/workspace-harness")
BEGIN = "# BEGIN workspace-harness (managed by register_codex.py)"
END = "# END workspace-harness"


def hook(script, timeout):
    path = os.path.join(HARNESS, script)
    with open(path, "rb") as f:
        revision = hashlib.sha256(f.read()).hexdigest()[:16]
    command = "python3 %s --harness-rev=%s" % (path, revision)
    return ('{ type = "command", command = "%s", async = false, '
            'timeout = %d }' % (command, timeout))


# Codex 0.144 제약 두 가지:
#  - async 훅 미지원("async hooks are not supported yet") — 모델 컨텍스트와 직접 관련 없는
#    스케줄러는 SessionStart에 등록하지 않는다.
#  - subagent_start 이벤트 없음 — vault_subagent.py는 Claude 전용으로 남는다.
BLOCK = """%s
[hooks]
SessionStart = [
  { hooks = [%s, %s] }
]
PostToolUse = [
  { matcher = "^(Bash|apply_patch|Edit|Write)$", hooks = [%s] }
]
Stop = [
  { hooks = [%s] }
]
%s
""" % (BEGIN, hook("repo_map.py", 10), hook("obsidian_bridge.py", 10),
       hook("verify_gate.py", 5), hook("verify_gate.py", 5), END)


def remove_managed_block(text):
    start = text.find(BEGIN)
    if start < 0:
        return text
    finish = text.find(END, start)
    if finish < 0:
        raise ValueError("workspace-harness managed block end marker missing")
    finish += len(END)
    while finish < len(text) and text[finish] == "\n":
        finish += 1
    return text[:start].rstrip() + "\n\n" + text[finish:].lstrip()


def existing_state_section(text):
    """재등록 시 Codex가 저장한 훅 신뢰 해시를 보존한다."""
    start = text.find(BEGIN)
    finish = text.find(END, start) if start >= 0 else -1
    state = text.find("[hooks.state]", start, finish) if finish >= 0 else -1
    if state < 0:
        return ""
    return text[state:finish].strip()


def has_project_doc_fallback(text):
    return bool(re.search(
        r"(?m)^\s*project_doc_fallback_filenames\s*=", text))


def main():
    with open(CONFIG, encoding="utf-8") as f:
        text = f.read()
    state = existing_state_section(text)
    text = remove_managed_block(text)
    if re.search(r"(?m)^\[hooks(?:\.|\])", text):
        raise RuntimeError("existing unmanaged [hooks] config detected; merge manually")
    if not has_project_doc_fallback(text):
        lines = text.splitlines()
        insert_at = 0
        while insert_at < len(lines) and not lines[insert_at].startswith("["):
            insert_at += 1
        lines.insert(insert_at, 'project_doc_fallback_filenames = ["CLAUDE.md"]')
        text = "\n".join(lines).rstrip() + "\n"
    block = BLOCK
    if state:
        block = block.replace("\n%s" % END, "\n%s\n%s" % (state, END))
    first_table = text.find("\n[")
    if first_table < 0:
        text = text.rstrip() + "\n\n" + block
    else:
        pos = first_table + 1
        text = text[:pos] + block + "\n" + text[pos:]
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    print("Codex workspace harness registered")


if __name__ == "__main__":
    main()
