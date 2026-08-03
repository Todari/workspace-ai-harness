#!/usr/bin/env python3
"""SessionStart: 레포 작업 시 옵시디언 프로젝트 노트의 '다음 할 일'을 컨텍스트로 주입."""
import json
import os
import re
import signal
import subprocess

import harness_lib as lib

VAULT = lib.VAULT_ROOT

# git 루트 디렉토리명 → 볼트 노트 경로 (볼트 루트 기준)
REPO_TO_NOTE = {
    "todari": "프로젝트/todari.md",
    "lovetype": "프로젝트/lvti.md",
    "2024-haeng-dong": "프로젝트/haengdong.md",
    "metro-nomedeul": "프로젝트/metronomdeul.md",
    "react-pixel-ui": "프로젝트/react-pixel-ui.md",
    "trade-tower": "프로젝트/trade-tower.md",
    # 전용 지식베이스를 가진 프로젝트는 그 폴더의 허브 노트가 대상이다.
    "forcletter": "포크레터/포크레터.md",
    # 이정표(모두의 창업 출품작) — 전용 폴더의 허브 노트가 대상.
    # 레포는 basetie에서 jeongpyo로 리브랜딩 중이라 로컬 디렉토리명이 둘 다 존재할 수 있다.
    "jeongpyo": "이정표/이정표.md",
    "basetie": "이정표/이정표.md",
}


def repo_basename(cwd):
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5)
        root = proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        root = ""
    return os.path.basename(root) if root else ""


def note_path_for(base):
    if base in REPO_TO_NOTE:
        return REPO_TO_NOTE[base]
    # forcletter-seo 등 워크트리/사본 디렉토리
    for prefix, rel in REPO_TO_NOTE.items():
        if base.startswith(prefix + "-"):
            return rel
    return ""


def read_with_timeout(path, seconds=3):
    """iCloud dataless 파일에서 열기가 멈출 수 있어 alarm으로 가드."""
    def _raise(signum, frame):
        raise TimeoutError

    old = signal.signal(signal.SIGALRM, _raise)
    signal.alarm(seconds)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, TimeoutError):
        return ""
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def one_liner(body):
    """허브 노트의 '> **한 줄 정의:**' 인용문을 뽑는다 (플레이스홀더면 무시).

    인용 블록이 여러 줄로 이어지면 뒤따르는 `>` 줄까지 합친다.
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^>\s*\*\*한 줄 정의:\*\*\s*(.*)$", line)
        if not m:
            continue
        parts = [m.group(1).strip()]
        for nxt in lines[i + 1:]:
            cont = re.match(r"^>\s*(.*)$", nxt)
            if not cont or not cont.group(1).strip():
                break
            parts.append(cont.group(1).strip())
        text = " ".join(p for p in parts if p)
        return "" if "채워넣기" in text else text
    return ""


def knowledge_index(note_path, limit=3):
    """허브 노트가 전용 폴더에 있으면 그 폴더의 지식 인덱스를 만든다.

    내용이 아니라 '무엇이 있는지'만 준다 — 에이전트가 vault_search.py로 파고들 단서.
    """
    folder = os.path.dirname(note_path)
    if os.path.realpath(folder) == os.path.realpath(os.path.join(VAULT, "프로젝트")):
        return ""  # 공용 프로젝트 폴더는 전용 지식베이스가 아니다

    lines = []
    for entry in sorted(os.listdir(folder)):
        sub = os.path.join(folder, entry)
        if not os.path.isdir(sub):
            continue
        notes = []
        for root, dirs, files in os.walk(sub):
            for f in files:
                if f.endswith(".md"):
                    p = os.path.join(root, f)
                    try:
                        notes.append((os.path.getmtime(p), f[:-3]))
                    except OSError:
                        pass
        if not notes:
            continue
        notes.sort(reverse=True)
        recent = ", ".join(n for _, n in notes[:limit])
        more = f" 외 {len(notes) - limit}건" if len(notes) > limit else ""
        lines.append(f"- {entry}/ ({len(notes)}건): {recent}{more}")

    glossary = os.path.join(folder, "_용어집.md")
    if os.path.exists(glossary):
        lines.insert(0, "- _용어집.md — **네이밍·도메인 용어는 코드 작성 전 여기서 확인**")
    return "\n".join(lines)


def next_tasks(note_body):
    m = re.search(r"^## 다음 할 일\n(.*?)(?=^## |\Z)", note_body, re.S | re.M)
    if not m:
        return []
    lines = [l.rstrip() for l in m.group(1).splitlines()]
    return [l for l in lines if re.match(r"\s*- \[ \] \S", l)]


def task_preview(tasks, limit=3):
    shown = tasks[:limit]
    more = len(tasks) - len(shown)
    if more > 0:
        shown.append(f"- … 외 {more}건 — 전체 목록은 필요할 때 노트에서 확인")
    return shown


def main():
    data = lib.read_hook_input()
    cwd = data.get("cwd", "")
    if not lib.in_workspace(cwd):
        return
    rel = note_path_for(repo_basename(cwd))
    if not rel:
        return
    note_path = os.path.join(VAULT, rel)
    if not os.path.exists(note_path):
        return
    body = read_with_timeout(note_path)
    tasks = next_tasks(body)

    parts = [f"[옵시디언 브리지] 이 레포의 볼트 노트: `{rel}`"]
    definition = one_liner(body)
    if definition:
        parts.append(f"한 줄 정의: {definition}")
    if tasks:
        parts.append(
            f"미완료 다음 할 일 {len(tasks)}건 — 현재 요청과 관련 있을 때만 참고:\n"
            + "\n".join(task_preview(tasks)))
    else:
        parts.append("'다음 할 일'은 비어 있음.")

    index = knowledge_index(note_path)
    if index:
        parts.append(
            "이 프로젝트는 볼트에도 기록을 둔다. 무엇이 볼트 담당이고 무엇이 레포 담당인지는 "
            "허브 노트가 정의하므로, 관련이 있으면 허브 노트를 먼저 읽을 것:\n" + index
            + "\n검색: python3 ~/.claude/hooks/workspace-harness/vault_search.py \"키워드\" "
              "(이 iCloud 경로에서 grep -r은 조용히 0건을 반환하므로 반드시 이 도구를 쓴다)")

    parts.append(
        "볼트는 사용자가 기록을 요청했거나, 이번 작업으로 제품 의사결정·프로젝트 상태·다음 할 일이 "
        "실제로 바뀐 경우에만 갱신한다. 일반 코드 수정·검증·질문만으로는 쓰지 않는다. 기록이 필요하면 "
        "완료는 `- [x]`, 신규는 `- [ ]`로 적고 한 줄로 보고한다.\n"
        f"노트 경로: {note_path}")

    text = "\n\n".join(parts)
    lib.event("obsidian-bridge", data.get("session_id", ""), rel)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
