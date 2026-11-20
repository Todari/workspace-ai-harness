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


FILE_ENVELOPE = "📥 [file] inbox-capture.txt"
MAX_FILE_BYTES = 1024 * 1024


def init_index(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inbox_cursors(
            channel TEXT, bot TEXT, last_id TEXT NOT NULL, PRIMARY KEY(channel, bot));
        CREATE TABLE IF NOT EXISTS inbox_pending(
            channel TEXT, bot TEXT, message_id TEXT, PRIMARY KEY(channel, bot, message_id));
    """)


def refresh_index(cfg, conn, me, max_pages):
    scope = (channel_id(cfg), me)
    for _ in range(max_pages):
        row = conn.execute("SELECT last_id FROM inbox_cursors WHERE channel=? AND bot=?", scope).fetchone()
        cursor = row[0] if row else "0"
        msgs = api(cfg, "GET", f"/channels/{scope[0]}/messages?limit=100&after={cursor}")
        if not isinstance(msgs, list) or len(msgs) > 100:
            raise InboxError("Discord 메시지 응답 형식 오류")
        if not msgs:
            return
        for msg in msgs:
            if not isinstance(msg, dict) or not re.fullmatch(r"[0-9]+", str(msg.get("id", ""))):
                raise InboxError("Discord 메시지 ID 형식 오류")
        newest = str(max(int(m["id"]) for m in msgs))
        if int(newest) <= int(cursor):
            raise InboxError("Discord 페이지 커서가 진행하지 않아 조회 중단")
        # Commit IDs and the high-water mark together. A competing reader/ack must
        # never resurrect a message from an older page. Bodies stay on Discord.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute("SELECT last_id FROM inbox_cursors WHERE channel=? AND bot=?", scope).fetchone()
            if latest != row:
                continue
            for msg in msgs:
                if int(msg["id"]) > int(cursor) and capture_item(msg, me):
                    conn.execute("INSERT OR IGNORE INTO inbox_pending VALUES(?,?,?)", (*scope, str(msg["id"])))
            conn.execute("INSERT OR REPLACE INTO inbox_cursors VALUES(?,?,?)", (*scope, newest))
        if len(msgs) < 100:
            return
    checkpoint, = conn.execute("SELECT last_id FROM inbox_cursors WHERE channel=? AND bot=?", scope).fetchone()
    more = api(cfg, "GET", f"/channels/{scope[0]}/messages?limit=1&after={checkpoint}")
    if not isinstance(more, list):
        raise InboxError("Discord 메시지 응답 형식 오류")
    if more:
        if conn.execute("SELECT 1 FROM inbox_pending WHERE channel=? AND bot=? LIMIT 1", scope).fetchone():
            print(f"인박스 수집 위치 {checkpoint} 저장. 남은 이력은 다음 조회에서 이어갑니다", file=sys.stderr)
            return
        raise InboxError(f"조회 상한 {max_pages}페이지: 수집 위치 저장 완료. 재시도하면 이어집니다")


class InvalidAttachment(InboxError):
    """The original envelope cannot be decoded; leave it for manual recovery."""


class NoAttachmentRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def attachment_text(msg):
    files = msg.get("attachments")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise InvalidAttachment("캡처 첨부가 없거나 잘못되었습니다")
    file = files[0]
    url = str(file.get("url", ""))
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        raise InvalidAttachment("캡처 첨부 URL 형식 오류") from None
    size = file.get("size")
    if (file.get("filename") != "inbox-capture.txt" or parsed.scheme != "https"
            or parsed.netloc != "cdn.discordapp.com" or not parsed.path.startswith("/attachments/")
            or type(size) is not int or not 0 < size <= MAX_FILE_BYTES):
        raise InvalidAttachment("캡처 첨부 정보가 잘못되었습니다")
    # Signed Discord CDN URL; never send the bot Authorization header to the CDN.
    req = urllib.request.Request(url, headers={"User-Agent": "DiscordBot (https://github.com/Todari/todari-ops, 1.0)"})
    try:
        with urllib.request.build_opener(NoAttachmentRedirect).open(req, timeout=20) as response:
            body = response.read(MAX_FILE_BYTES + 1)
        if len(body) != size:
            raise InboxError("캡처 첨부 길이가 일치하지 않습니다")
        try:
            return body.decode("utf-8")
        except UnicodeError:
            raise InvalidAttachment("첨부 UTF-8 형식 오류") from None
    except (OSError, ValueError):
        raise InboxError("캡처 첨부 조회 실패") from None


def pending_items(cfg, max_pages=MAX_PAGES, *, count_only=False):
    if max_pages < 1:
        raise InboxError("max-pages는 1 이상이어야 합니다")
    profile = api(cfg, "GET", "/users/@me")
    if not isinstance(profile, dict) or not profile.get("id"):
        raise InboxError("Discord 사용자 응답 형식 오류")
    me = profile["id"]
    cid = channel_id(cfg)
    with ops_report.database(ops_report.DB) as conn:
        init_index(conn)
        refresh_index(cfg, conn, me, max_pages)
        ids = conn.execute("SELECT message_id FROM inbox_pending WHERE channel=? AND bot=? "
                           "ORDER BY length(message_id), message_id", (cid, me)).fetchall()
        if count_only:
            return len(ids)
        items = []
        for (mid,) in ids:
            try:
                msg = api(cfg, "GET", f"/channels/{cid}/messages/{mid}")
            except InboxError as exc:
                if str(exc) != "Discord HTTP 404":
                    raise
                # Leave a visible recovery item when the original was deleted after indexing.
                items.append({"id": mid, "kind": "quarantine", "project": None, "text": "",
                              "timestamp": "", "reason": "original_message_missing"})
                continue
            if not isinstance(msg, dict) or msg.get("id") != mid:
                raise InboxError("Discord 캡처 조회 응답 형식 오류")
            item = capture_item(msg, me)
            if item is None:
                # A reaction may have succeeded just before a process restart.
                with conn:
                    conn.execute("DELETE FROM inbox_pending WHERE channel=? AND bot=? AND message_id=?", (cid, me, mid))
                continue
            if msg["content"] == FILE_ENVELOPE:
                try:
                    decoded = capture_item({**msg, "content": attachment_text(msg)}, me)
                    if decoded is None:
                        raise InvalidAttachment("첨부 캡처 형식 오류")
                    item = decoded
                except InvalidAttachment:
                    item["reason"] = "invalid_capture_attachment"
            items.append(item)
        return items


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
                try:
                    api(cfg, "PUT", f"/channels/{cid}/messages/{mid}/reactions/{emoji}/@me")
                except InboxError as exc:
                    if str(exc) != "Discord HTTP 404":
                        raise
                    # A confirmed receipt and a deleted original need no further Discord action.
                with ops_report.database(ops_report.DB) as conn:
                    init_index(conn)
                    conn.execute("DELETE FROM inbox_pending WHERE channel=? AND message_id=?", (cid, mid))
            except (InboxError, sqlite3.Error, OSError) as exc:
                failed.append(mid)
                print(f"ack 실패 ID {mid}: {exc}", file=sys.stderr)
        print(f"acked {len(mids) - len(failed)}, failed {len(failed)}")
        return 1 if failed else 0
    try:
        items = pending_items(cfg, max_pages=args.max_pages, count_only=cmd == "count")
    except (InboxError, sqlite3.Error, OSError) as exc:
        print(f"인박스 조회 실패: {exc}", file=sys.stderr)
        return 1
    if cmd == "count":
        print(items)
    else:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
