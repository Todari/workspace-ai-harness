#!/usr/bin/env python3
"""~/.codex/config.toml에 하네스 훅을 멱등하게 등록한다. 목록은 hooks_manifest.py.

Codex는 훅 정의 해시로 신뢰를 기록한다. 스크립트가 바뀌면 `--harness-rev`가 달라져
재승인을 요구하므로, 코드 수정 뒤에는 이 스크립트를 다시 실행해야 한다(doctor.py가 검사).
"""
import os
import re

import hooks_manifest as manifest

CONFIG = os.path.expanduser("~/.codex/config.toml")
HARNESS = manifest.HARNESS
BEGIN = "# BEGIN workspace-harness (managed by register_codex.py)"
END = "# END workspace-harness"


def hook_toml(script, timeout):
    return ('{ type = "command", command = "%s", async = false, timeout = %d }'
            % (manifest.codex_command(script), timeout))


def build_block():
    by_event = {}
    for spec in manifest.codex_hooks():
        by_event.setdefault(spec["event"], {}).setdefault(
            spec.get("codex_matcher"), []).append(spec)
    lines = [BEGIN, "[hooks]"]
    for event, groups in by_event.items():
        items = []
        for matcher, specs in groups.items():
            hooks = ", ".join(hook_toml(s["script"], s["timeout"]) for s in specs)
            if matcher:
                items.append('  { matcher = "%s", hooks = [%s] }' % (matcher, hooks))
            else:
                items.append("  { hooks = [%s] }" % hooks)
        lines.append("%s = [" % event)
        lines.append(",\n".join(items))
        lines.append("]")
    lines.append(END)
    return "\n".join(lines) + "\n"


BLOCK = build_block()


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
