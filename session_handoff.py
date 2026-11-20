#!/usr/bin/env python3
"""세션 인계 자동화.

긴 세션의 문맥이 한계에 가까워지면 Stop 훅이 한 번 차단해 인계 노트를 쓰게 하고,
같은 경로에서 열리는 다음 세션(Claude·Codex)의 SessionStart가 그 노트를 주입한다.

CLI:
  session_handoff.py write [--cwd C] [--session S] [--client claude|codex] [--file F]  # 기본 stdin
  session_handoff.py show [--cwd C] [--note ID]
  session_handoff.py done [--cwd C] --note ID  # 인계 완료·폐기
  session_handoff.py list
훅 입력(JSON)이 stdin으로 오면 훅 모드로 동작한다.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import uuid

import context_watch
import harness_lib as lib

HANDOFF_DIR = os.path.join(lib.CACHE_DIR, "session-handoff")
HANDOFF_TTL_SECONDS = 72 * 3600
RESTART_TOKENS = context_watch.RESTART_TOKENS
# Codex: 창 25.8만, 자동 압축 16만 설정. 압축을 넘겨 문맥이 커졌거나 누적 입력이 큰 세션은 인계한다.
CODEX_RESTART_TOKENS = 180_000
CODEX_TOTAL_INPUT_TOKENS = 40_000_000
INJECT_SOURCES = ("", "startup", "clear")  # resume·compact 재개에는 다시 주입하지 않는다
SECTIONS = ("목표", "결정", "진행", "검증 상태", "다음 할 일", "주의")
SCRIPT = os.path.abspath(__file__)


def key_for(cwd):
    real = os.path.realpath(cwd or os.getcwd())
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:12]


@contextmanager
def locked_notes():
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    with open(os.path.join(HANDOFF_DIR, ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _store(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=HANDOFF_DIR, encoding="utf-8", delete=False) as f:
        tmp = f.name
        try:
            json.dump(data, f, ensure_ascii=False)
            f.close()
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _notes(cwd=None):
    """Read new notes and legacy MD/JSON pairs without rewriting legacy state."""
    base = Path(HANDOFF_DIR)
    pattern = key_for(cwd) + "*.json" if cwd is not None else "*.json"
    notes = []
    for path in base.glob(pattern):
        if path.name.endswith((".stop.json", ".used.json")):
            continue
        legacy = "-" not in path.stem
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            when = float(meta["written_at"])
            body = path.with_suffix(".md").read_text(encoding="utf-8") if legacy else meta["body"]
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            continue
        if cwd is not None and meta.get("cwd") != os.path.realpath(cwd):
            continue
        note_id = path.stem if not legacy else "%s-legacy-%s" % (path.stem, when.hex())
        notes.append((body, dict(meta, note_id=note_id, written_at=when), path, legacy))
    return sorted(notes, key=lambda note: note[1]["written_at"], reverse=True)


def _current(meta):
    return time.time() - float(meta["written_at"]) <= HANDOFF_TTL_SECONDS


def load(cwd, *, session_id=None, note_id=None):
    for body, meta, _, _ in _notes(cwd):
        if _current(meta) and (session_id is None or meta.get("session_id") == session_id) \
                and (note_id is None or meta["note_id"] == note_id):
            return body, meta
    return None, {}


def _remove(path, legacy):
    path.unlink()
    if legacy:
        path.with_suffix(".md").unlink(missing_ok=True)


def write(cwd, body, session_id="", client="claude", auto=False):
    body = (body or "").strip()
    if not body:
        raise ValueError("인계 내용이 비어 있다")
    with locked_notes():
        old_notes = [(meta, path, legacy) for _, meta, path, legacy in _notes(cwd)
                     if session_id and meta.get("session_id") == session_id]
        if auto and any(_current(meta) and not meta.get("auto") for meta, _, _ in old_notes):
            return ""
        path = Path(HANDOFF_DIR) / (key_for(cwd) + "-" + uuid.uuid4().hex + ".json")
        meta = {"cwd": os.path.realpath(cwd), "session_id": session_id, "client": client,
                "written_at": time.time(), "consumed_by": [], "auto": auto, "body": body + "\n"}
        _store(path, meta)
        for _, previous, legacy in old_notes:
            _remove(previous, legacy)
        cutoff = time.time() - HANDOFF_TTL_SECONDS
        for cached in Path(HANDOFF_DIR).iterdir():
            try:
                if cached.suffix in (".md", ".json") and cached.stat().st_mtime < cutoff:
                    cached.unlink()
            except OSError:
                pass
    lib.event("handoff-auto" if auto else "handoff-write", session_id, str(len(body)))
    return str(path)


def done(cwd, note_id):
    with locked_notes():
        for _, meta, path, legacy in _notes(cwd):
            if meta["note_id"] == note_id:
                _remove(path, legacy)
                return True
    return False


def missing_sections(body):
    return [s for s in SECTIONS if s not in body]


def write_instruction(cwd, session_id, tokens, client="claude", reason=""):
    return (
        "[SESSION HANDOFF] %s. 이 세션은 여기서 끝내야 한다. 지금 다음을 한다.\n"
        "1. 인계 노트를 저장한다(heredoc, 절 제목은 그대로): "
        "`python3 %s write --cwd %s --session %s --client %s <<'EOF'` … `EOF`\n"
        "   절: %s. 다음 할 일은 명령·파일 경로까지 구체적으로, 검증 상태는 실행한 테스트와 결과.\n"
        "2. 메모리·볼트에 남길 결정이 있으면 남긴다.\n"
        "3. 사용자에게 새 세션을 열면 인계 노트가 자동 주입된다고 한 줄로 알린다."
    ) % (reason or "직전 요청 문맥 %dk 토큰" % (tokens // 1000), shlex.quote(SCRIPT), shlex.quote(cwd),
         shlex.quote(session_id or "unknown"), client, " / ".join(SECTIONS))


def stop_reason(data, session_id):
    """(client, 문맥 토큰, 사유 문자열). 인계가 필요 없으면 사유는 빈 문자열."""
    path = data.get("transcript_path", "")
    codex = context_watch.is_codex_transcript(path) or (not path and bool(
        context_watch.find_codex_transcript(session_id)))
    if codex:
        path = path or context_watch.find_codex_transcript(session_id)
        ctx, total = context_watch.codex_usage(path)
        if ctx >= CODEX_RESTART_TOKENS:
            return "codex", ctx, "직전 턴 문맥 %dk 토큰(창 %dk)" % (
                ctx // 1000, CODEX_RESTART_TOKENS // 1000)
        if total >= CODEX_TOTAL_INPUT_TOKENS:
            return "codex", ctx, "세션 누적 입력 %dM 토큰" % (total // 1_000_000)
        return "codex", ctx, ""
    tokens = context_watch.last_context_tokens(path)
    if tokens >= RESTART_TOKENS:
        return "claude", tokens, "직전 요청 문맥 %dk 토큰" % (tokens // 1000)
    return "claude", tokens, ""


def inject_text(body, meta):
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(meta.get("written_at", 0))))
    return (
        "[SESSION HANDOFF] %s에 %s 세션(%s)이 남긴 %s인계 노트. 시작할 때 이어갈지 사용자에게 한 줄로 "
        "확인하고, 이어가거나 폐기하면 `python3 %s done --cwd %s --note %s`를 실행한다. "
        "현재 코드·git 상태가 노트와 다르면 현재 상태를 우선한다.\n---\n%s"
    ) % (when, meta.get("client", "claude"), (meta.get("session_id") or "")[:8],
         "자동 생성 " if meta.get("auto") else "", shlex.quote(SCRIPT),
         shlex.quote(meta.get("cwd", "")), shlex.quote(meta.get("note_id", "")), body.strip())


def _session_marker(cwd, session_id, kind):
    suffix = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:12]
    return Path(HANDOFF_DIR) / ("%s-%s.%s.json" % (key_for(cwd), suffix, kind))


def _already_prompted(cwd, session_id):
    if _session_marker(cwd, session_id, "stop").exists():
        return True
    try:
        legacy = Path(HANDOFF_DIR) / (key_for(cwd) + ".stop.json")
        return session_id in json.loads(legacy.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False


def _select_for_session(cwd, session_id):
    with locked_notes():
        marker = _session_marker(cwd, session_id, "used")
        if marker.exists():
            return None
        notes = [note for note in _notes(cwd) if _current(note[1])]
        if any(session_id in meta.get("consumed_by", []) for _, meta, _, _ in notes):
            return None
        for body, meta, path, _ in notes:
            if session_id == meta.get("session_id"):
                continue
            meta.setdefault("consumed_by", []).append(session_id)
            _store(path, {key: value for key, value in meta.items() if key != "note_id"})
            _store(marker, {})
            return body, meta
    return None


def handle(data, now=None):
    """SessionStart → 주입 텍스트(str) 또는 None. Stop → block dict 또는 None."""
    cwd = data.get("cwd", "")
    if not lib.in_workspace(cwd):
        return None
    event = data.get("hook_event_name", "")
    session_id = data.get("session_id") or data.get("thread_id") or data.get("conversation_id")
    if not session_id:
        return None
    if event == "SessionStart":
        if data.get("source", "") not in INJECT_SOURCES:
            return None
        selected = _select_for_session(cwd, session_id) if session_id else None
        if selected:
            body, meta = selected
            lib.event("handoff-inject", session_id, meta.get("session_id", "")[:8])
            return inject_text(body, meta)
        return None
    if event == "PreCompact":
        return pre_compact(data, cwd, session_id)
    if event == "Stop":
        if data.get("stop_hook_active"):
            return None
        client, tokens, reason = stop_reason(data, session_id)
        if not reason:
            return None
        with locked_notes():
            if _already_prompted(cwd, session_id):
                return None
            _, meta = load(cwd, session_id=session_id)
            if meta and not meta.get("auto"):
                return None
            _store(_session_marker(cwd, session_id, "stop"), {})
        lib.event("handoff-block", session_id, str(tokens))
        return {"decision": "block",
                "reason": write_instruction(cwd, session_id, tokens, client, reason)}
    return None


def pre_compact(data, cwd, session_id):
    """압축 직전: 이 세션의 수동 노트가 없으면 트랜스크립트에서 자동 노트를 만들어 둔다."""
    import handoff_extract
    _, meta = load(cwd, session_id=session_id)
    if meta and not meta.get("auto"):
        return None
    path = data.get("transcript_path", "")
    if not path:
        path = context_watch.find_codex_transcript(session_id)
    client = "codex" if context_watch.is_codex_transcript(path) else "claude"
    body = handoff_extract.build_note(path, cwd, client)
    if not body:
        return None
    write(cwd, body, session_id, client, auto=True)
    return None


def hook_main(data):
    result = handle(data)
    if isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False))
    elif result:
        print(lib.hook_output("SessionStart", result))


def cli_main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--cwd", default=os.getcwd())
    w.add_argument("--session", default="")
    w.add_argument("--client", default="claude")
    w.add_argument("--file")
    s = sub.add_parser("show")
    s.add_argument("--cwd", default=os.getcwd())
    s.add_argument("--note")
    d = sub.add_parser("done")
    d.add_argument("--cwd", default=os.getcwd())
    d.add_argument("--note", required=True)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.cmd == "write":
        body = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        path = write(args.cwd, body, args.session, args.client)
        gaps = missing_sections(body)
        print("saved:", path)
        if gaps:
            print("경고: 빠진 절 -", ", ".join(gaps))
    elif args.cmd == "show":
        body, meta = load(args.cwd, note_id=args.note)
        print(inject_text(body, meta) if body else "인계 노트 없음")
    elif args.cmd == "done":
        print("removed" if done(args.cwd, args.note) else "인계 노트 없음")
    elif args.cmd == "list":
        for _, meta, _, _ in _notes():
            if not _current(meta):
                continue
            age_h = (time.time() - float(meta["written_at"])) / 3600
            print("%s  %.1fh  %s  consumed=%d" % (meta["note_id"], age_h, meta["cwd"],
                                                 len(meta.get("consumed_by", []))))


def cli_args():
    # Registration revision is hook metadata, not a CLI subcommand.
    return [arg for arg in sys.argv[1:] if not arg.startswith("--harness-rev=")]


def main():
    args = cli_args()
    if args:
        cli_main(args)
        return
    hook_main(lib.read_hook_input())


if __name__ == "__main__":
    if cli_args():
        main()
    else:
        lib.run_fail_open(main)
