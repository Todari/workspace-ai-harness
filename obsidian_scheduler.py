#!/usr/bin/env python3
"""SessionStart(async): 옵시디언 볼트 자동화의 anacron식 스케줄러.

cron은 iCloud 볼트 접근에 전체 디스크 권한이 필요해서 쓰지 않는다. 대신 이미
FDA를 가진 경로(Ghostty 아래 Claude 세션)의 SessionStart에서 조건을 확인하고,
밀린 작업이 있으면 백그라운드로 분리 실행한다. 중복은 파일 존재/마커로 방지.

- 주간 회고: 금 17시 이후~일요일, 회고/YYYY-Wnn.md가 없으면 weekly_review.sh
- 월간 점검: 매월 1~7일, 이번 달 마커가 없으면 vault_health.py
"""
import datetime
import os
import subprocess
import sys

import harness_lib as lib

VAULT = lib.VAULT_ROOT
HARNESS = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.expanduser("~/.claude/scripts")
LOG_DIR = os.path.expanduser("~/.claude/logs")
MARKER = os.path.join(LOG_DIR, ".vault_health_last")
INBOX_MARKER = os.path.join(LOG_DIR, ".inbox_check_last")
INBOX_CHECK_THROTTLE = 30 * 60  # 인박스 확인은 30분에 한 번으로 제한


def spawn(cmd, log_name):
    os.makedirs(LOG_DIR, exist_ok=True)
    log = open(os.path.join(LOG_DIR, log_name), "a")
    env = dict(os.environ, OBSIDIAN_SCHED="1")
    subprocess.Popen(cmd, stdout=log, stderr=log,
                     start_new_session=True, env=env)


def throttled(marker, seconds, now):
    try:
        return os.path.exists(marker) and now.timestamp() - os.path.getmtime(marker) < seconds
    except OSError:
        return False


def touch(marker):
    os.makedirs(LOG_DIR, exist_ok=True)
    open(marker, "w").close()


def inbox_pending():
    """디스코드 인박스 미처리 개수. 실패/네트워크 문제면 0으로 간주(fail-open)."""
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(HARNESS, "inbox_fetch.py"), "count"],
            capture_output=True, text=True, timeout=15)
        return int((out.stdout or "0").strip() or "0")
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0


def main():
    dry = "--dry-run" in sys.argv
    # 자동화가 띄운 헤드리스 세션 안에서는 재귀 실행 금지
    if os.environ.get("OBSIDIAN_SCHED"):
        return
    # 볼트 접근 불가(권한 없는 컨텍스트)면 조용히 종료
    if not os.path.isdir(os.path.join(VAULT, "회고")):
        if dry:
            print("skip: 볼트 접근 불가")
        return

    now = datetime.datetime.now()

    # 주간 회고 (금 17시~일)
    in_window = now.weekday() == 4 and now.hour >= 17 or now.weekday() >= 5
    week = now.strftime("%G-W%V")
    review = os.path.join(VAULT, "회고", f"{week}.md")
    if in_window and not os.path.exists(review):
        if dry:
            print(f"launch: weekly_review ({week})")
        else:
            spawn([os.path.join(SCRIPTS, "weekly_review.sh")], "weekly_review.log")
    elif dry:
        print(f"skip: weekly ({'창 밖' if not in_window else week + ' 존재'})")

    # 월간 점검 (1~7일, 월 1회)
    month = now.strftime("%Y-%m")
    last = ""
    if os.path.exists(MARKER):
        last = open(MARKER).read().strip()
    if now.day <= 7 and last != month:
        if dry:
            print(f"launch: vault_health ({month})")
        else:
            # 마커는 vault_health.py가 스스로 기록한다 (cron과 공존 시 중복 방지)
            spawn([sys.executable, os.path.join(SCRIPTS, "vault_health.py")],
                  "vault_health.log")
    elif dry:
        print(f"skip: monthly ({'날짜 아님' if now.day > 7 else month + ' 완료'})")

    # 인박스 따라잡기 — 09:10 크론은 맥이 자면 못 돈다. 세션 시작 시 미처리가 있으면 보충.
    # 확인 자체를 30분에 한 번으로 제한(네트워크·헤드리스 중복 방지). 스윕은 자체적으로
    # 0건이면 스킵하고 처리분만 ack하므로 중복 실행돼도 안전하다.
    if throttled(INBOX_MARKER, INBOX_CHECK_THROTTLE, now):
        if dry:
            print("skip: inbox (throttled <30m)")
    else:
        if not dry:
            touch(INBOX_MARKER)
        pending = inbox_pending()
        if pending > 0:
            if dry:
                print(f"launch: inbox_sweep ({pending} pending)")
            else:
                spawn([os.path.join(SCRIPTS, "inbox_sweep.sh")], "inbox_sweep.log")
        elif dry:
            print("skip: inbox (0 pending)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 스케줄러 실패가 세션을 방해해선 안 됨
