#!/usr/bin/env python3
"""하네스 이벤트 통계. 사용: python3 stats.py [일수=7]

읽는 법:
- map-inject의 주입 문자 수가 크면 repo-map이 토큰을 먹고 있다는 신호 → lean 모드·심층 문서 축소.
- verify-block 대비 검증 이행률이 낮으면 게이트가 무시되고 있다는 신호.
"""
import glob
import json
import os
import sys
import time
from collections import Counter

import harness_lib as lib

VERIFY_DIR = os.path.join(lib.CACHE_DIR, "verify-gate")


def summarize(lines, since_ts=0):
    kinds = Counter()
    sessions = set()
    inject_by_repo = Counter()
    inject_chars = Counter()
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        t = e.get("t", "")
        if t:
            try:
                ts = time.mktime(time.strptime(t, "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                ts = 0
            if ts < since_ts:
                continue
        if not e.get("kind"):
            continue
        kinds[e["kind"]] += 1
        if e.get("session"):
            sessions.add(e["session"])
        if e["kind"] == "map-inject" and e.get("detail"):
            name, _, chars = e["detail"].partition(":")
            inject_by_repo[name] += 1
            if chars.isdigit():
                inject_chars[name] += int(chars)
    return {"kinds": dict(kinds), "sessions": len(sessions),
            "inject_by_repo": inject_by_repo.most_common(8),
            "inject_chars": dict(inject_chars)}


def verify_compliance(state_dir=VERIFY_DIR):
    """Stop 차단을 받은 세션 중 이후 검증을 실행한 비율 (상태 파일 TTL 안의 세션만)."""
    prompted = verified = 0
    cutoff = time.time() - lib.STATE_TTL_SECONDS
    for path in glob.glob(os.path.join(state_dir, "*.json")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        prompted_at = state.get("last_prompted_edit", 0)
        if not prompted_at:
            continue
        prompted += 1
        if state.get("last_verify", 0) >= prompted_at:
            verified += 1
    return prompted, verified


def main():
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 7
    try:
        with open(lib.EVENTS_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    s = summarize(lines, time.time() - days * 86400)
    print("최근 %g일 하네스 이벤트" % days)
    if not s["kinds"]:
        print("  (없음)")
        return
    for kind, count in sorted(s["kinds"].items()):
        print("  %-16s %d" % (kind, count))
    print("  %-16s %d" % ("활성 세션", s["sessions"]))
    if s["inject_by_repo"]:
        print("  repo-map 주입 (건수 / 총 문자):")
        for name, n in s["inject_by_repo"]:
            chars = s["inject_chars"].get(name, 0)
            print("    %-24s %4d / %s" % (name, n, ("%d" % chars) if chars else "-"))
    prompted, verified = verify_compliance()
    if prompted:
        print("  검증 게이트 이행: 차단 %d세션 중 %d세션이 검증 실행 (%d%%)" % (
            prompted, verified, 100 * verified // prompted))


if __name__ == "__main__":
    main()
