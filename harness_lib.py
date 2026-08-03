#!/usr/bin/env python3
"""workspace 하네스 훅 공용 헬퍼. stdlib만 사용, 항상 fail-open."""
import json
import os
import sys
import time
import traceback

def configured_path(env_name, default):
    """환경변수 경로를 `~` 확장·정규화해 반환한다."""
    return os.path.realpath(os.path.expanduser(os.environ.get(env_name, default)))


WORKSPACE_ROOT = configured_path("WORKSPACE_HARNESS_ROOT", "~/workspace")
VAULT_ROOT = configured_path(
    "OBSIDIAN_VAULT_PATH",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/docs",
)
CACHE_DIR = configured_path("WORKSPACE_HARNESS_CACHE_DIR", "~/.claude/cache")
LOG_PATH = os.path.join(CACHE_DIR, "harness.log")
MARKER_DIR = os.path.join(CACHE_DIR, "plan-gate")
MAP_CACHE_DIR = os.path.join(CACHE_DIR, "repo-maps")
MARKER_TTL_SECONDS = 7 * 24 * 3600
MAX_LOG_BYTES = 512 * 1024
LOG_KEEP_LINES = 200
EVENTS_PATH = os.path.join(CACHE_DIR, "harness-events.jsonl")
MAX_EVENTS_BYTES = 2 * 1024 * 1024
EVENTS_KEEP_LINES = 2000


def log(msg):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-LOG_KEEP_LINES:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def event(kind, session_id="", detail=""):
    """관측용 이벤트를 JSONL로 기록. 통계는 stats.py로 조회."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > MAX_EVENTS_BYTES:
            with open(EVENTS_PATH, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-EVENTS_KEEP_LINES:]
            with open(EVENTS_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "kind": kind,
                "session": (session_id or "")[:8],
                "detail": detail,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_hook_input():
    try:
        return json.load(sys.stdin)
    except (ValueError, OSError):
        return {}


def in_workspace(cwd):
    if not cwd:
        return False
    real = os.path.realpath(cwd)
    return real == WORKSPACE_ROOT or real.startswith(WORKSPACE_ROOT + os.sep)


def _marker_path(session_id):
    safe = session_id.replace(os.sep, "_")
    return os.path.join(MARKER_DIR, safe)


def set_marker(session_id, reason):
    if not session_id:
        return
    os.makedirs(MARKER_DIR, exist_ok=True)
    with open(_marker_path(session_id), "w", encoding="utf-8") as f:
        f.write(reason)
    prune_old_markers()


def prune_old_markers():
    """TTL 지난 세션 마커 삭제 (마커 생성 시점마다 호출돼 축적을 막음)."""
    prune_old_files(MARKER_DIR, MARKER_TTL_SECONDS)


def prune_old_files(dirpath, ttl_seconds):
    try:
        names = os.listdir(dirpath)
    except OSError:
        return
    cutoff = time.time() - ttl_seconds
    for name in names:
        path = os.path.join(dirpath, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def has_marker(session_id):
    return bool(session_id) and os.path.exists(_marker_path(session_id))


def marker_reason(session_id):
    """마커 파일 내용(사유 문자열). 없으면 None."""
    if not session_id:
        return None
    try:
        with open(_marker_path(session_id), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def run_fail_open(fn):
    """훅 엔트리포인트 래퍼: 어떤 오류가 나도 exit 0 (세션을 깨뜨리지 않음)."""
    try:
        fn()
    except Exception:
        log("fail-open: " + traceback.format_exc().replace("\n", " | "))
    sys.exit(0)
