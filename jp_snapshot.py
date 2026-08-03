#!/usr/bin/env python3
"""봇 POST /jp/export → 볼트 공부/일본어/ 스냅샷 재생성 (DB가 원본, 볼트는 생성물).

    python3 jp_snapshot.py             # POST /jp/export → 볼트 갱신
    python3 jp_snapshot.py --selftest  # 목 데이터로 write()만 검증 (네트워크 없음)

크론: 20 8 * * * (~/.claude/scripts/jp_snapshot.sh, 매일 push 직후).
설정: ops.env 의 VAULT_SYNC_URL / VAULT_SYNC_SECRET — vault_sync.py 와 완전히 동일한
파일·키를 재사용한다(별도 시크릿 없음). 인증도 vault_sync.py가 POST할 때 쓰는 것과
동일: 바디 HMAC-SHA256(hex), 헤더 `x-vault-signature`, 키는 VAULT_SYNC_SECRET.
export URL은 VAULT_SYNC_URL(.../webhook/vault-sync)에서 경로만 바꿔 유도한다.
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request

import harness_lib as lib

VAULT_ROOT = lib.VAULT_ROOT
JP_DIR = os.path.join(VAULT_ROOT, "공부", "일본어")
OPS_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops.env")
HEADER = "> 이 폴더는 봇에서 생성됩니다. 손으로 고치지 마세요 (jp_snapshot.py)."


def nfc(s):
    return unicodedata.normalize("NFC", s)


def esc(v):
    """테이블 셀 안전화: 파이프·개행 제거."""
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def load_ops_env(path=OPS_ENV):
    """vault_sync.py 의 파서와 동일 — VAULT_SYNC_URL / VAULT_SYNC_SECRET 재사용."""
    cfg = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def export_url(cfg):
    """VAULT_SYNC_URL(.../webhook/vault-sync) → .../jp/export."""
    url = cfg.get("VAULT_SYNC_URL", "")
    if not url:
        return ""
    base = url.rsplit("/webhook/", 1)[0]
    return base + "/jp/export"


def fetch(cfg):
    """POST /jp/export, 바디 HMAC-SHA256 서명 (vault_sync.py의 post()와 동일 스킴).

    반환: {"cards": [...], "mistakes": [...], "stats": {...}} — 실패 시 예외를 던진다
    (호출부가 stderr 로깅 + non-zero exit 처리).
    """
    url = export_url(cfg)
    secret = cfg.get("VAULT_SYNC_SECRET", "")
    if not url or not secret:
        raise RuntimeError("ops.env 에 VAULT_SYNC_URL / VAULT_SYNC_SECRET 필요")
    body = b"{}"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-vault-signature": sig})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def write(data, vault=JP_DIR):
    """data({cards, mistakes, stats}) → 공부/일본어/*.md 덮어쓰기 (생성물, DB가 원본).

    fetch()와 분리되어 있어 목 데이터로도 독립적으로 검증(dry-test)할 수 있다.
    """
    os.makedirs(vault, exist_ok=True)

    # 단어장.md — cards 테이블
    lines = [
        "---\ntype: 자료\n주제: 일본어\n---\n",
        HEADER, "",
        "# 일본어 단어장", "",
        "| 표현 | 읽기 | 뜻 | 예문 | 출처 | 배운날 |",
        "|---|---|---|---|---|---|",
    ]
    for c in data.get("cards", []):
        lines.append(
            f"| {esc(c.get('front'))} | {esc(c.get('reading'))} | {esc(c.get('meaning'))} "
            f"| {esc(c.get('example'))} | {esc(c.get('source'))} "
            f"| {esc(c.get('createdAt'))[:10]} |")
    _write_file(vault, "단어장.md", "\n".join(lines) + "\n")

    # 틀린 것 모음.md — mistakes 목록
    ml = ["---\ntype: 자료\n주제: 일본어\n---\n", HEADER, "", "# 틀린 것 모음", ""]
    for m in data.get("mistakes", []):
        ml.append(
            f"- {esc(m.get('createdAt'))[:10]} — ~~{esc(m.get('original'))}~~ → "
            f"**{esc(m.get('corrected'))}** ({esc(m.get('reason'))})")
    _write_file(vault, "틀린 것 모음.md", "\n".join(ml) + "\n")

    # _일본어.md — 허브(MOC)
    s = data.get("stats", {}) or {}
    hub = [
        "---\ntype: MOC\n주제: 일본어\n---\n",
        HEADER, "",
        "# 일본어", "",
        f"진도: 총 {s.get('total', 0)} · 복습 대기 {s.get('due', 0)} · "
        f"최근 7일 {s.get('learned7d', 0)}", "",
        "- [[단어장]]",
        "- [[틀린 것 모음]]",
    ]
    _write_file(vault, "_일본어.md", "\n".join(hub) + "\n")


def _write_file(vault, name, content):
    path = os.path.join(vault, nfc(name))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _mock_data():
    return {
        "cards": [
            {"front": "食べる", "reading": "たべる", "meaning": "먹다",
             "example": "ご飯を食べる。", "exampleKo": "밥을 먹다.", "note": "",
             "kind": "동사", "source": "daily", "createdAt": "2026-07-20T00:00:00.000Z"},
            {"front": "美味しい", "reading": "おいしい", "meaning": "맛있다",
             "example": "これは美味しい|ですね。", "exampleKo": "이거 맛있네요.", "note": "",
             "kind": "형용사", "source": "review", "createdAt": "2026-07-22T00:00:00.000Z"},
        ],
        "mistakes": [
            {"original": "私は学生だ", "corrected": "私は学生です",
             "reason": "정중체 필요", "createdAt": "2026-07-21T00:00:00.000Z"},
        ],
        "stats": {"total": 42, "due": 5, "learned7d": 7},
    }


def main():
    if "--selftest" in sys.argv:
        d = tempfile.mkdtemp(prefix="jp_snapshot_selftest_")
        write(_mock_data(), vault=d)
        print(f"selftest: {d} 에 목 데이터로 스냅샷 생성 완료 (임시 디렉토리, 실제 볼트 아님)")
        return 0

    if not os.path.isdir(VAULT_ROOT):
        print("볼트 접근 불가 (전체 디스크 접근 권한?)", file=sys.stderr)
        return 1

    try:
        data = fetch(load_ops_env())
    except urllib.error.HTTPError as e:
        print(f"/jp/export 요청 실패 HTTP {e.code}: {e.read().decode()[:200]}",
              file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — 크론 로그에 원인만 남기면 된다
        print(f"/jp/export 요청 실패: {e}", file=sys.stderr)
        return 1

    write(data)
    s = data.get("stats", {}) or {}
    print(f"스냅샷 갱신: 총 {s.get('total', 0)} · 복습 대기 {s.get('due', 0)} · "
          f"최근 7일 {s.get('learned7d', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
