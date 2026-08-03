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
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ENV_FILES = [
    os.path.expanduser("~/workspace/projects/todari-ops/.env.production"),
    os.path.expanduser("~/workspace/projects/todari-ops/.env"),
]
API = "https://discord.com/api/v10"
PREFIX = "📥"
ITEM_RE = re.compile(r"^📥 \[(task|idea|til|checkin|note)\](?: \(([^)]+)\))? (.+)$", re.S)


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
    with urllib.request.urlopen(req, timeout=20) as res:
        body = res.read()
        return json.loads(body) if body.strip() else None


def channel_id(cfg):
    return cfg.get("INBOX_CHANNEL_ID") or cfg.get("ALERTS_CHANNEL_ID") or ""


def pending_items(cfg):
    me = api(cfg, "GET", "/users/@me")["id"]
    msgs = api(cfg, "GET", f"/channels/{channel_id(cfg)}/messages?limit=100")
    items = []
    for m in msgs:
        if m.get("author", {}).get("id") != me:
            continue
        content = m.get("content", "")
        if not content.startswith(PREFIX):
            continue
        acked = any(
            r.get("emoji", {}).get("name") == "✅" and r.get("me")
            for r in m.get("reactions", []) or [])
        if acked:
            continue
        match = ITEM_RE.match(content)
        items.append({
            "id": m["id"],
            "kind": match.group(1) if match else "task",
            "project": match.group(2) if match else None,
            "text": (match.group(3) if match else content[len(PREFIX):]).strip(),
            "timestamp": m.get("timestamp", "")[:16],
        })
    items.reverse()  # 오래된 것부터
    return items


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "count", "ack"):
        print(__doc__, file=sys.stderr)
        return 2
    cfg = load_env()
    if not cfg.get("DISCORD_BOT_TOKEN") or not channel_id(cfg):
        print("DISCORD_BOT_TOKEN / 채널 ID 를 env 파일에서 찾지 못함", file=sys.stderr)
        return 1
    cmd = sys.argv[1]
    if cmd == "ack":
        if len(sys.argv) < 3:
            print("ack 대상 메시지 ID 필요", file=sys.stderr)
            return 2
        emoji = urllib.parse.quote("✅")
        cid = channel_id(cfg)
        for mid in sys.argv[2].split(","):
            api(cfg, "PUT", f"/channels/{cid}/messages/{mid.strip()}/reactions/{emoji}/@me")
        print(f"acked {len(sys.argv[2].split(','))}")
        return 0
    items = pending_items(cfg)
    if cmd == "count":
        print(len(items))
    else:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
