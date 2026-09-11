#!/usr/bin/env python3
"""디스코드 인박스(📥 캡처) 조회/ack — inbox-sweep 스킬의 도구.

todari-ops 봇의 /task /idea 가 인박스 채널에 `📥 [task] (slug) 내용` 형태로
쌓아둔 메시지를 읽는다. 스위퍼(볼트에 정리하는 쪽)가 처리한 항목은 봇 계정의
✅ 리액션으로 ack 해서 재처리를 막는다.

    python3 inbox_fetch.py list          # 미처리 항목 JSON 배열
    python3 inbox_fetch.py count         # 미처리 개수만 (크론 사전 체크용)
    python3 inbox_fetch.py ack ID[,ID..] # 처리 완료 표시(✅)

토큰/채널은 ~/workspace/projects/todari-ops/.env.production 에서 읽는다
(INBOX_CHANNEL_ID 없으면 ALERTS_CHANNEL_ID 폴백 — 봇과 동일한 규칙).
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ops_report

ENV_FILES = [
    os.path.expanduser("~/workspace/projects/todari-ops/.env.production"),
    os.path.expanduser("~/workspace/projects/todari-ops/.env"),
]
API = "https://discord.com/api/v10"
PREFIX = "📥"
ITEM_RE = re.compile(r"^📥 \[(task|idea|til|checkin|note)\](?: \(([^)]+)\))? (.+)$", re.S)
MAX_PAGES = 20
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 30


class InboxError(Exception):
    """Only credential-free, controlled messages cross the CLI boundary."""


def load_env():
    cfg = {}
    for path in ENV_FILES:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg.setdefault(k.strip(), v.strip())
    return cfg


def api(cfg, method, path):
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        headers={
            "Authorization": f"Bot {cfg['DISCORD_BOT_TOKEN']}",
            # Discord/Cloudflare가 기본 Python-urllib UA를 403으로 막는다.
            "User-Agent": "DiscordBot (https://github.com/Todari/todari-ops, 1.0)",
        })
    for attempt in range(MAX_ATTEMPTS):
        delay = 2 ** attempt
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                body = res.read()
                return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code != 429 and not 500 <= code < 600:
                exc.close()
                raise InboxError(f"Discord HTTP {code}") from None
            if code == 429:
                delay = retry_delay(exc, delay)
            exc.close()
            if attempt == MAX_ATTEMPTS - 1:
                raise InboxError(f"Discord HTTP {code}: 재시도 {MAX_ATTEMPTS}회 실패") from None
            if delay > MAX_RETRY_DELAY:
                raise InboxError("Discord HTTP 429: 재시도 대기 상한 초과") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == MAX_ATTEMPTS - 1:
                raise InboxError(f"Discord 연결 실패: 재시도 {MAX_ATTEMPTS}회 실패") from None
        except (ValueError, UnicodeError):
            raise InboxError("Discord 응답 JSON 형식 오류") from None
        time.sleep(delay)


def retry_delay(error, fallback):
    """Honor numeric Retry-After without ever printing response bodies."""
    values = [error.headers.get("Retry-After") if error.headers else None]
    try:
        body = json.loads(error.read(16384))
        if isinstance(body, dict):
            values.append(body.get("retry_after"))
    except (ValueError, UnicodeError, OSError):
        pass
    delays = []
    for value in values:
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                delays.append(number)
        except (TypeError, ValueError):
            pass
    return max(delays) if delays else fallback


def channel_id(cfg):
    return cfg.get("INBOX_CHANNEL_ID") or cfg.get("ALERTS_CHANNEL_ID") or ""


def pending_items(cfg, max_pages=MAX_PAGES):
    if max_pages < 1:
        raise InboxError("max-pages는 1 이상이어야 합니다")
    profile = api(cfg, "GET", "/users/@me")
    if not isinstance(profile, dict) or not profile.get("id"):
        raise InboxError("Discord 사용자 응답 형식 오류")
    me = profile["id"]
    items = []
    seen = set()
    before = None
    for _ in range(max_pages):
        path = f"/channels/{channel_id(cfg)}/messages?limit=100"
        if before:
            path += f"&before={before}"
        msgs = api(cfg, "GET", path)
        if not isinstance(msgs, list) or len(msgs) > 100:
            raise InboxError("Discord 메시지 응답 형식 오류")
        for m in msgs:
            if not isinstance(m, dict) or not re.fullmatch(r"[0-9]+", str(m.get("id", ""))):
                raise InboxError("Discord 메시지 ID 형식 오류")
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            item = capture_item(m, me)
            if item:
                items.append(item)
        if len(msgs) < 100:
            return sorted(items, key=lambda item: int(item["id"]))
        cursor = str(min(int(m["id"]) for m in msgs))
        if before is not None and int(cursor) >= int(before):
            raise InboxError("Discord 페이지 커서가 진행하지 않아 조회 중단")
        before = cursor
    raise InboxError(f"인박스 조회 상한 {max_pages}페이지 도달: 부분 결과는 반환하지 않습니다; --max-pages를 늘려 재시도")


def capture_item(m, me):
    if m.get("author", {}).get("id") != me:
        return None
    content = m.get("content", "")
    if not isinstance(content, str) or not content.startswith(PREFIX):
        return None
    acked = any(
        r.get("emoji", {}).get("name") == "✅" and r.get("me")
        for r in m.get("reactions", []) or [])
    if acked:
        return None
    match = ITEM_RE.match(content)
    item = {
        "id": m["id"],
        "kind": match.group(1) if match else "quarantine",
        "project": match.group(2) if match else None,
        "text": match.group(3).strip() if match else content,
        "timestamp": m.get("timestamp", "")[:16],
    }
    if not match:
        item["reason"] = "unsupported_or_malformed_capture"
    return item


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("list", "count", "ack"))
    ap.add_argument("ids", nargs="?")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    args = ap.parse_args(argv)
    if args.max_pages < 1:
        ap.error("--max-pages는 1 이상이어야 합니다")
    if args.command == "ack" and not re.fullmatch(r"[0-9]+(?:\s*,\s*[0-9]+)*", args.ids or ""):
        ap.error("ack 대상은 쉼표로 구분한 메시지 ID여야 합니다")
    if args.command != "ack" and args.ids:
        ap.error("메시지 ID는 ack에서만 지정합니다")
    cfg = load_env()
    if not cfg.get("DISCORD_BOT_TOKEN") or not channel_id(cfg):
        print("DISCORD_BOT_TOKEN / 채널 ID 를 env 파일에서 찾지 못함", file=sys.stderr)
        return 1
    cmd = args.command
    if cmd == "ack":
        emoji = urllib.parse.quote("✅")
        cid = channel_id(cfg)
        mids = list(dict.fromkeys(mid.strip() for mid in args.ids.split(",")))
        failed = []
        for mid in mids:
            try:
                try:
                    recorded = ops_report.receipt(mid)
                except (OSError, sqlite3.Error):
                    raise InboxError("처리 원장을 읽을 수 없어 ack하지 않습니다") from None
                if not recorded:
                    raise InboxError("기록 영수증이 없어 ack하지 않습니다")
                api(cfg, "PUT", f"/channels/{cid}/messages/{mid}/reactions/{emoji}/@me")
            except InboxError as exc:
                failed.append(mid)
                print(f"ack 실패 ID {mid}: {exc}", file=sys.stderr)
        print(f"acked {len(mids) - len(failed)}, failed {len(failed)}")
        return 1 if failed else 0
    try:
        items = pending_items(cfg, max_pages=args.max_pages)
    except InboxError as exc:
        print(f"인박스 조회 실패: {exc}", file=sys.stderr)
        return 1
    if cmd == "count":
        print(len(items))
    else:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
