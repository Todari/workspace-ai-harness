#!/usr/bin/env python3
"""하네스 이벤트 통계. 사용: python3 stats.py [일수=7]

bypass 비중이 높으면 게이트가 과하다는 신호, plan-deny 후 plan-marker가 따라오면
게이트가 계획을 유도하고 있다는 신호로 읽는다.
"""
import json
import sys
import time
from collections import Counter

import harness_lib as lib


def summarize(lines, since_ts=0):
    kinds = Counter()
    sessions = set()
    denied = Counter()
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
        if e["kind"] in ("plan-deny", "bash-deny") and e.get("detail"):
            denied[e["detail"]] += 1
    return {"kinds": dict(kinds), "sessions": len(sessions),
            "top_denied": denied.most_common(5)}


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
        print("  %-14s %d" % (kind, count))
    print("  %-14s %d" % ("활성 세션", s["sessions"]))
    if s["top_denied"]:
        print("  차단 상위:")
        for detail, n in s["top_denied"]:
            print("    %2d× %s" % (n, detail))
    bypass = s["kinds"].get("quick-bypass", 0)
    deny = s["kinds"].get("plan-deny", 0) + s["kinds"].get("bash-deny", 0)
    if bypass + deny:
        print("  힌트: 차단 %d / !quick %d — bypass 비중이 높으면 게이트 완화를 검토" % (deny, bypass))


if __name__ == "__main__":
    main()
