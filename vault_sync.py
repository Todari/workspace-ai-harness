#!/usr/bin/env python3
"""볼트 → todari-ops 봇 동기화: '다음 할 일' 체크박스와 📅 일정만 추출해 push.

전체 볼트는 절대 업로드하지 않는다 — 열린 태스크 라인과 날짜 항목만 JSON으로
추려 HMAC 서명과 함께 봇의 /webhook/vault-sync 로 POST 한다. 봇은 이 데이터로
아침 다이제스트의 "다가오는 마감 / 다음 할 일 / [▶ 시작] 버튼"을 만든다.

    python3 vault_sync.py            # 파싱 + POST
    python3 vault_sync.py --dry-run  # 파싱 요약만 출력 (전송 안 함)
    python3 vault_sync.py --dump     # 전체 payload JSON 출력 (전송 안 함)

크론: 55 7,12,18 * * * (다이제스트 08:30 KST 전에 최신화).
설정: ops.env 의 VAULT_SYNC_URL / VAULT_SYNC_SECRET (서버 .env.production 과 동일 값).

파싱 규칙:
- `프로젝트/*.md` + `이정표/이정표.md` 의 `## 다음 할 일` 섹션에서 `- [ ]` 라인.
- 태스크 라인/`## 일정` 섹션 불릿의 `📅 YYYY-MM-DD` 또는 `📅 M/D` (KST 연도 추론).
- `이정표/대회/*.md` 는 `## 일정` 만 본다.
"""
import argparse
import datetime
import glob
import hashlib
import hmac
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request

import harness_lib as lib

VAULT = lib.VAULT_ROOT
OPS_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops.env")

# 볼트 노트 이름 → 봇 카탈로그 slug (다르면 여기 추가; 기본은 노트 이름 그대로)
NOTE_TO_SLUG = {
    "이정표": "jeongpyo",
}

CHECKBOX_RE = re.compile(r"^\s*[-*] \[ \] (.+)$")
DATE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})")
SECTION_RE = re.compile(r"^##\s+")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def today_kst():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


def normalize_date(raw, today=None):
    """'2026-08-05' 또는 '8/14' → 'YYYY-MM-DD'. M/D는 45일 이상 지났으면 내년."""
    today = today or today_kst()
    if "-" in raw:
        return raw
    m, d = (int(x) for x in raw.split("/"))
    try:
        candidate = datetime.date(today.year, m, d)
    except ValueError:
        return None
    if (today - candidate).days > 45:
        candidate = datetime.date(today.year + 1, m, d)
    return candidate.isoformat()


def extract_section(text, title):
    """`## <title>` 섹션 본문 라인들 (다음 ## 전까지)."""
    lines = text.splitlines()
    out, inside = [], False
    for line in lines:
        if SECTION_RE.match(line):
            inside = nfc(line).strip() == f"## {title}"
            continue
        if inside:
            out.append(line)
    return out


def strip_md(s):
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\[\[(.+?)\]\]", r"\1", s)
    s = DATE_RE.sub("", s)
    return " ".join(s.split()).strip(" -·")


def parse_note(path, today=None):
    """노트 하나 → (tasks, deadlines). tasks=[{text, due?}], deadlines=[{text, date}]."""
    with open(path, encoding="utf-8") as f:
        text = nfc(f.read())
    tasks, deadlines = [], []
    for line in extract_section(text, "다음 할 일"):
        m = CHECKBOX_RE.match(line)
        if not m:
            continue
        raw = m.group(1)
        task = {"text": strip_md(raw)[:300]}
        dm = DATE_RE.search(raw)
        if dm:
            due = normalize_date(dm.group(1), today)
            if due:
                task["due"] = due
        if task["text"]:
            tasks.append(task)
    for line in extract_section(text, "일정"):
        dm = DATE_RE.search(line)
        if not dm or not line.strip().startswith(("-", "*")):
            continue
        date = normalize_date(dm.group(1), today)
        text_ = strip_md(line.strip().lstrip("-*"))[:300]
        if date and text_:
            deadlines.append({"text": text_, "date": date})
    return tasks, deadlines


def collect(vault=VAULT, today=None):
    """볼트 전체 → notes 리스트. 이정표는 허브+대회 노트를 하나로 합친다."""
    notes = {}

    def bucket(name):
        name = nfc(name)
        if name not in notes:
            entry = {"note": name, "tasks": [], "deadlines": []}
            slug = NOTE_TO_SLUG.get(name, name)
            entry["slug"] = slug
            notes[name] = entry
        return notes[name]

    for path in sorted(glob.glob(os.path.join(vault, "프로젝트", "*.md"))):
        name = nfc(os.path.splitext(os.path.basename(path))[0])
        tasks, deadlines = parse_note(path, today)
        if tasks or deadlines:
            b = bucket(name)
            b["tasks"] += tasks
            b["deadlines"] += deadlines

    jeongpyo_files = [os.path.join(vault, "이정표", "이정표.md")]
    jeongpyo_files += sorted(glob.glob(os.path.join(vault, "이정표", "대회", "*.md")))
    for path in jeongpyo_files:
        if not os.path.isfile(path):
            continue
        tasks, deadlines = parse_note(path, today)
        if tasks or deadlines:
            b = bucket("이정표")
            b["tasks"] += tasks
            b["deadlines"] += deadlines

    return sorted(notes.values(), key=lambda n: n["note"])


def load_ops_env(path=OPS_ENV):
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


def post(payload, cfg):
    url = cfg.get("VAULT_SYNC_URL", "")
    secret = cfg.get("VAULT_SYNC_SECRET", "")
    if not url or not secret:
        print("ops.env 에 VAULT_SYNC_URL / VAULT_SYNC_SECRET 필요", file=sys.stderr)
        return 2
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-vault-signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            print(f"POST {res.status}: {res.read().decode()[:200]}")
            return 0
    except urllib.error.HTTPError as e:
        print(f"POST 실패 HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — 크론 로그에 원인만 남기면 된다
        print(f"POST 실패: {e}", file=sys.stderr)
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(VAULT):
        print("볼트 접근 불가 (전체 디스크 접근 권한?)", file=sys.stderr)
        return 1

    notes = collect()
    payload = {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "notes": notes,
    }
    n_tasks = sum(len(n["tasks"]) for n in notes)
    n_deadlines = sum(len(n["deadlines"]) for n in notes)
    summary = f"{len(notes)} notes, {n_tasks} tasks, {n_deadlines} deadlines"

    if args.dump:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        print(summary)
        for n in notes:
            heads = [t["text"][:40] for t in n["tasks"][:2]]
            dls = [f'{d["date"]} {d["text"][:30]}' for d in n["deadlines"][:3]]
            print(f'- {n["note"]} ({n["slug"]}): tasks={len(n["tasks"])} {heads} deadlines={dls}')
        return 0

    rc = post(payload, load_ops_env())
    if rc == 0:
        print(summary)
    return rc


if __name__ == "__main__":
    sys.exit(main())
