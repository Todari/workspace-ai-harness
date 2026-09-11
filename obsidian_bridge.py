#!/usr/bin/env python3
"""SessionStart: 레포 작업 시 옵시디언 프로젝트 노트의 '다음 할 일'을 컨텍스트로 주입."""
import os
import re
import signal
import subprocess

import harness_lib as lib
import repos

VAULT = lib.VAULT_ROOT


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
    """레포 디렉토리명 → 볼트 노트 상대 경로. 매핑은 repos.json의 vault_note가 정본."""
    return repos.vault_note_for(base) if base else ""


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


def project_focus(body):
    """허브가 명시한 현재 초점만 읽는다. 과거 로그·mtime에서 현재 상태를 추정하지 않는다."""
    match = re.search(r"^## 현재 초점\s*\n(.*?)(?=^## |\Z)", body, re.S | re.M)
    if not match:
        return ""
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    return "\n".join(line[:240] for line in lines[:3])


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
    if not body:
        # 읽기 실패를 '할 일 없음'으로 전달하지 않는다.
        print(lib.hook_output("SessionStart",
              f"[옵시디언 브리지] `{rel}` 읽기 실패 또는 빈 노트. 상태·할 일은 확인되지 않았습니다. "
              "현재 요청은 레포 문맥으로 진행하고, 과거 결정이 필요할 때만 다시 읽으세요."))
        return
    tasks = next_tasks(body)

    parts = [f"[옵시디언 브리지] 이 레포의 볼트 노트: `{rel}`"]
    definition = one_liner(body)
    if definition:
        parts.append(f"한 줄 정의: {definition}")
    focus = project_focus(body)
    if focus:
        parts.append("현재 초점 (노트에 적힌 확인 시점 기준):\n" + focus)
    if tasks:
        parts.append(
            f"미완료 다음 할 일 {len(tasks)}건 — 현재 요청과 관련 있을 때만 참고:\n"
            + "\n".join(task_preview(tasks)))
    else:
        parts.append("'다음 할 일'은 비어 있음.")

    try:
        index = knowledge_index(note_path)
    except OSError as exc:
        # iCloud 폴더 권한·dataless 문제로 인덱스만 실패해도 나머지 컨텍스트는 내보낸다.
        lib.log("obsidian_bridge: knowledge_index skipped: %s" % exc)
        index = ""
    if index:
        parts.append(
            "이 프로젝트는 볼트에도 기록을 둔다. 무엇이 볼트 담당이고 무엇이 레포 담당인지는 "
            "허브 노트가 정의하므로, 관련이 있으면 허브 노트를 먼저 읽을 것:\n" + index
            + "\n검색: python3 ~/.claude/hooks/workspace-harness/vault_search.py \"키워드\" "
              "(이 iCloud 경로에서 grep -r은 조용히 0건을 반환하므로 반드시 이 도구를 쓴다)")

    parts.append(
        "볼트는 사용자가 기록을 요청했거나, 이번 작업으로 제품 의사결정·프로젝트 상태·다음 할 일이 "
        "실제로 바뀐 경우에만 갱신한다. 일반 코드 수정·검증·질문만으로는 쓰지 않는다. 기록이 필요하면 "
        "종료 전에 기존 항목을 갱신하고, 새 결정에는 이유·확인 날짜·근거 링크를 남긴다. "
        "같은 할 일을 중복 추가하지 않고, 완료 근거가 있을 때만 `- [x]`로 바꾼다. "
        "로컬 기록과 원격 동기화·Discord 반영은 따로 확인해 한 줄로 보고한다.\n"
        f"노트 경로: {note_path}")

    text = "\n\n".join(parts)
    lib.event("obsidian-bridge", data.get("session_id", ""), rel)
    print(lib.hook_output("SessionStart", text))


if __name__ == "__main__":
    lib.run_fail_open(main)
