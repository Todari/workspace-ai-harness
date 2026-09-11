#!/usr/bin/env python3
"""하네스 이벤트 통계. 사용: python3 stats.py [일수=7] [--json] [--include-claude]

기본은 이벤트·상태·Codex worker 요약만 읽는다. 큰 Claude 원문 사용량 집계는
--include-claude를 명시했을 때만 실행한다. --json은 정기 운영 보고용이다.

읽는 법:
- map-inject의 주입 문자 수가 크면 repo-map이 토큰을 먹고 있다는 신호 → lean 모드·심층 문서 축소.
- verify-block 대비 검증 이행률이 낮으면 게이트가 무시되고 있다는 신호.
"""
import argparse
import datetime
import glob
import json
import os
import math
import time
from collections import Counter

import harness_lib as lib

VERIFY_DIR = os.path.join(lib.CACHE_DIR, "verify-gate")
HANDOFF_DIR = os.path.join(lib.CACHE_DIR, "claude-handoff")
CODEX_RUNS_DIR = lib.CODEX_RUNS_DIR
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


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


def handoff_summary(state_dir=HANDOFF_DIR, since_ts=0):
    """hard handoff 턴의 이행 지표: 위임 턴 수, run ID 회수율, 위반·background·wait 횟수.

    문서의 성공 기준(위임 턴당 폴링 0회, run 결과 회수)을 상태 파일에서 직접 센다.
    """
    summary = {
        "delegated": 0, "with_run": 0, "result_ready": 0, "violations": 0,
        "background_attempts": 0, "wait_calls": 0, "planner_tool_calls": 0,
        "routes": Counter(),
    }
    for path in glob.glob(os.path.join(state_dir, "*.json")):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            continue
        route = state.get("route", "direct")
        summary["routes"][route] += 1
        if route == "direct":
            continue
        summary["delegated"] += 1
        if state.get("run_ids"):
            summary["with_run"] += 1
        if state.get("phase") == "result_ready":
            summary["result_ready"] += 1
        for key in ("violations", "background_attempts", "wait_calls", "planner_tool_calls"):
            value = state.get(key, 0)
            if isinstance(value, int):
                summary[key] += value
    return summary


def codex_worker_summary(run_dir=CODEX_RUNS_DIR, since_ts=0):
    """Codex worker 기록에서 모델·상태·토큰·시간을 집계한다."""
    summary = {
        "runs": 0,
        "statuses": Counter(),
        "routes": Counter(),
        "usage": Counter(),
        "duration_seconds": 0.0,
    }
    for path in glob.glob(os.path.join(run_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
            created = record.get("created_at", "")
            ts = datetime.datetime.fromisoformat(created).timestamp() if created else 0
        except (OSError, ValueError, TypeError):
            continue
        if ts < since_ts:
            continue
        summary["runs"] += 1
        invocation = record.get("invocation_status", "unknown")
        status = (record.get("worker_status", "unknown")
                  if invocation == "completed" else invocation)
        summary["statuses"][status] += 1
        route = "%s/%s" % (record.get("model", "unknown"), record.get("effort", "unknown"))
        summary["routes"][route] += 1
        for key, value in record.get("usage", {}).items():
            if isinstance(value, int):
                summary["usage"][key] += value
        duration = record.get("duration_seconds", 0)
        if isinstance(duration, (int, float)):
            summary["duration_seconds"] += duration
    return summary


def claude_usage_summary(projects_dir=CLAUDE_PROJECTS_DIR, since_ts=0):
    """최근 파일의 실제 요청만 집계하며 파일간 복제·스트리밍은 최신 요청 ID로 합친다."""
    messages = {}
    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        continue
                    stamp = row.get("timestamp", "")
                    if not isinstance(stamp, str):
                        continue
                    ts = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if ts < since_ts:
                    continue
                message = row.get("message") or {}
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage") or {}
                model = message.get("model")
                if not isinstance(usage, dict) or not usage or not isinstance(model, str):
                    continue
                if not model or model.strip("<>").lower() == "synthetic" or row.get("isSynthetic"):
                    continue
                message_id = message.get("id")
                if isinstance(message_id, str) and message_id:
                    key = ("message", message_id)
                elif isinstance(row.get("uuid"), str) and row["uuid"]:
                    key = ("row", path, row["uuid"])
                else:
                    continue
                # 같은 timestamp의 streaming 조각은 누적 output이 큰 쪽을 보존한다.
                output_tokens = usage.get("output_tokens", 0)
                order = (ts, output_tokens if isinstance(output_tokens, int) else 0)
                previous = messages.get(key)
                if previous and previous["order"] > order:
                    continue
                messages[key] = {
                    "order": order,
                    "path": path,
                    "model": model,
                    "effort": row.get("effort") or "unknown",
                    "sidechain": bool(row.get("isSidechain")),
                    "usage": usage,
                }
    summary = {
        "requests": len(messages),
        "sessions": len({row["path"] for row in messages.values()}),
        "sidechain_requests": 0,
        "routes": Counter(),
        "usage": Counter(),
    }
    for row in messages.values():
        summary["routes"]["%s/%s" % (row["model"], row["effort"])] += 1
        if row["sidechain"]:
            summary["sidechain_requests"] += 1
        usage = row["usage"]
        for key in ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"):
            value = usage.get(key, 0)
            if isinstance(value, int):
                summary["usage"][key] += value
        details = usage.get("output_tokens_details") or {}
        thinking = details.get("thinking_tokens", 0) if isinstance(details, dict) else 0
        if isinstance(thinking, int):
            summary["usage"]["thinking_tokens"] += thinking
    return summary


def collect_report(days=7, include_claude=False):
    """운영 보고용 읽기 전용 수집. Claude 원문 접근은 명시적으로 선택한다."""
    if not isinstance(days, (int, float)) or not math.isfinite(days) or days <= 0:
        raise ValueError("days must be a positive finite number")
    now = time.time()
    since_ts = now - days * 86400
    try:
        with open(lib.EVENTS_PATH, encoding="utf-8") as f:
            events = summarize(f, since_ts)
    except OSError:
        events = summarize([], since_ts)
    prompted, verified = verify_compliance()
    return {
        "schema_version": 1,
        "days": days,
        "generated_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        "since": datetime.datetime.fromtimestamp(since_ts, datetime.timezone.utc).isoformat(),
        "events": events,
        "verification": {
            "prompted": prompted,
            "verified": verified,
            "state_ttl_days": lib.STATE_TTL_SECONDS / 86400,
        },
        "handoff": handoff_summary(since_ts=since_ts),
        "codex_workers": codex_worker_summary(since_ts=since_ts),
        "claude_included": bool(include_claude),
        "claude": claude_usage_summary(since_ts=since_ts) if include_claude else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("days", nargs="?", type=float, default=7)
    parser.add_argument("--json", action="store_true", help="운영 보고용 JSON 출력")
    parser.add_argument("--include-claude", action="store_true", help="Claude 원문 사용량 집계 포함")
    args = parser.parse_args(argv)
    if not math.isfinite(args.days) or args.days <= 0:
        parser.error("days must be a positive finite number")
    report = collect_report(args.days, include_claude=args.include_claude)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    days = report["days"]
    s = report["events"]
    print("최근 %g일 하네스 이벤트" % days)
    if not s["kinds"]:
        print("  (없음)")
    else:
        for kind, count in sorted(s["kinds"].items()):
            print("  %-16s %d" % (kind, count))
        print("  %-16s %d" % ("활성 세션", s["sessions"]))
    if s["inject_by_repo"]:
        print("  repo-map 주입 (건수 / 총 문자):")
        for name, n in s["inject_by_repo"]:
            chars = s["inject_chars"].get(name, 0)
            print("    %-24s %4d / %s" % (name, n, ("%d" % chars) if chars else "-"))
    prompted = report["verification"]["prompted"]
    verified = report["verification"]["verified"]
    if prompted:
        print("  검증 게이트 이행: 차단 %d세션 중 %d세션이 검증 실행 (%d%%)" % (
            prompted, verified, 100 * verified // prompted))
    handoff = report["handoff"]
    if handoff["routes"]:
        print("  라우트 분포: %s" % ", ".join("%s %d" % item
                                            for item in handoff["routes"].most_common()))
    if handoff["delegated"]:
        n = handoff["delegated"]
        print("  위임 턴 %d: run ID 회수 %d (%d%%), 결과 수신 %d, 위반 %.1f/턴, "
              "background 시도 %d, wait %.1f/턴, git 준비 호출 %.1f/턴" % (
                  n, handoff["with_run"], 100 * handoff["with_run"] // n,
                  handoff["result_ready"], handoff["violations"] / n,
                  handoff["background_attempts"], handoff["wait_calls"] / n,
                  handoff["planner_tool_calls"] / n))
    workers = report["codex_workers"]
    if workers["runs"]:
        print("  Codex worker: %d회 / 상태 %s" % (
            workers["runs"], ", ".join("%s %d" % item
                                       for item in workers["statuses"].most_common())))
        print("    라우트: %s" % ", ".join("%s %d" % item
                                           for item in workers["routes"].most_common()))
        usage = workers["usage"]
        input_tokens = usage.get("input_tokens", 0)
        cached_tokens = usage.get("cached_input_tokens", 0)
        print("    토큰: input %d (cached %d, uncached %d), output %d (reasoning %d)" % (
            input_tokens, cached_tokens, max(0, input_tokens - cached_tokens),
            usage.get("output_tokens", 0), usage.get("reasoning_output_tokens", 0)))
        print("    평균 시간: %.1f초" % (workers["duration_seconds"] / workers["runs"]))
    claude = report["claude"]
    if claude is None:
        print("  Claude 원문 사용량은 생략 (--include-claude로 포함)")
    elif claude["requests"]:
        print("  Claude: %d 요청 / %d 세션 파일 (sidechain %d)" % (
            claude["requests"], claude["sessions"], claude["sidechain_requests"]))
        print("    라우트: %s" % ", ".join("%s %d" % item
                                           for item in claude["routes"].most_common()))
        usage = claude["usage"]
        print("    토큰: input %d, cache-create %d, cache-read %d, output %d (thinking %d)" % (
            usage.get("input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("cache_read_input_tokens", 0),
            usage.get("output_tokens", 0), usage.get("thinking_tokens", 0)))


if __name__ == "__main__":
    main()
