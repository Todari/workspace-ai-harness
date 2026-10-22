#!/usr/bin/env python3
"""하네스 이벤트 통계. 사용: python3 stats.py [일수=7] [--json] [--include-claude]

기본은 이벤트·상태 요약만 읽는다. 큰 Claude 원문 사용량 집계는
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
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CONTEXT_WARN_TOKENS = 250_000


def _percentile(values, ratio):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(math.ceil(ratio * len(values))) - 1)]


def context_summary(rows):
    """요청별 문맥(input+cache) 분포와 세션별 최대 문맥. rows: {path, usage}."""
    per_request = []
    per_session = {}
    for row in rows:
        u = row["usage"]
        ctx = sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if ctx <= 0:
            continue
        per_request.append(ctx)
        name = os.path.basename(row["path"])[:8]
        per_session[name] = max(per_session.get(name, 0), ctx)
    top = sorted(per_session.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "requests": len(per_request),
        "median": _percentile(per_request, 0.5),
        "p90": _percentile(per_request, 0.9),
        "max": max(per_request) if per_request else 0,
        "over_warn": sum(1 for c in per_request if c >= CONTEXT_WARN_TOKENS),
        "top_sessions": top,
    }


def codex_session_summary(sessions_dir=CODEX_SESSIONS_DIR, since_ts=0):
    """Codex rollout의 마지막 token_count로 세션별 직전 문맥·누적 입력을 모은다."""
    import context_watch
    sessions = []
    for path in glob.glob(os.path.join(sessions_dir, "*", "*", "*", "rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
        except OSError:
            continue
        ctx, total = context_watch.codex_usage(path)
        if total:
            sessions.append((os.path.basename(path)[-17:-6], ctx, total))
    sessions.sort(key=lambda t: t[2], reverse=True)
    return {
        "sessions": len(sessions),
        "total_input": sum(t[2] for t in sessions),
        "over_warn": sum(1 for t in sessions if t[1] >= 180_000),
        "top": sessions[:5],
    }


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
        "context": context_summary(list(messages.values())),
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
        "claude_included": bool(include_claude),
        "claude": claude_usage_summary(since_ts=since_ts) if include_claude else None,
        "codex_sessions": codex_session_summary(since_ts=since_ts) if include_claude else None,
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
        ctx = claude.get("context") or {}
        if ctx.get("requests"):
            print("    요청당 문맥: 중앙값 %dk / p90 %dk / 최대 %dk, %dk 이상 %d건" % (
                ctx["median"] // 1000, ctx["p90"] // 1000, ctx["max"] // 1000,
                CONTEXT_WARN_TOKENS // 1000, ctx["over_warn"]))
            print("    최대 문맥 세션: %s" % ", ".join(
                "%s %dk" % (name, value // 1000) for name, value in ctx["top_sessions"]))
    codex = report.get("codex_sessions")
    if codex and codex["sessions"]:
        print("  Codex 세션 %d개: 누적 입력 %dM, 직전 문맥 180k 이상 %d개" % (
            codex["sessions"], codex["total_input"] // 1_000_000, codex["over_warn"]))
        print("    상위 세션: %s" % ", ".join(
            "%s ctx %dk/입력 %dM" % (n, c // 1000, t // 1_000_000) for n, c, t in codex["top"]))


if __name__ == "__main__":
    main()
