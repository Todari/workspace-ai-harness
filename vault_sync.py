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
실전송은 깨끗한 Markdown과 Git upstream의 실제 원격 HEAD 일치를 확인한다.
확인 후 전송 사이의 원격 변경은 수신 측 revision 검사 없이는 완전히 막을 수 없다.

파싱 규칙:
- 레포 레지스트리의 vault_note와 기존 `프로젝트/*.md`, 이정표 허브를 수집한다.
- 태스크 라인/`## 일정` 섹션 불릿의 `📅 YYYY-MM-DD` 또는 `📅 M/D` (KST 연도 추론).
- `이정표/대회/*.md` 는 `## 일정` 만 본다.
"""
import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request

import harness_lib as lib
import repos

VAULT = lib.VAULT_ROOT
OPS_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops.env")

# 기존 봇 카탈로그의 별칭. 레지스트리의 명시적 vault_slug가 있으면 우선한다.
NOTE_TO_SLUG = {
    "이정표": "jeongpyo",
}

CHECKBOX_RE = re.compile(r"^\s*[-*] \[ \] (.+)$")
DATE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})")
SECTION_RE = re.compile(r"^##\s+")


class PreflightError(Exception):
    """Safe errors: never include remote URLs, credentials, or Git stderr."""


def git_output(vault, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", vault, *args], capture_output=True, text=True,
            timeout=20, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        raise PreflightError("Git 확인을 완료하지 못했습니다") from None
    if proc.returncode:
        raise PreflightError("Git 확인 실패: 저장소 또는 upstream 상태를 확인하세요")
    return proc.stdout.strip()


def markdown_clean(vault):
    status = git_output(vault, "status", "--porcelain=v1", "-z",
                        "--untracked-files=all", "--", ":(glob)**/*.md")
    if status:
        raise PreflightError("미커밋 Markdown 변경이 있어 전송을 보류합니다")


def preflight(vault, expected_revision):
    """Compare actual upstream remote, then recheck local state before sending."""
    if git_output(vault, "rev-parse", "--verify", "HEAD") != expected_revision:
        raise PreflightError("수집 중 볼트 HEAD가 변경되어 전송을 보류합니다")
    markdown_clean(vault)
    branch = git_output(vault, "branch", "--show-current")
    if not branch:
        raise PreflightError("볼트 브랜치가 detached HEAD 상태입니다")
    remote = git_output(vault, "config", "--get", f"branch.{branch}.remote")
    ref = git_output(vault, "config", "--get", f"branch.{branch}.merge")
    if not remote or remote == "." or remote.startswith("-") or not ref.startswith("refs/heads/"):
        raise PreflightError("볼트 upstream 원격 브랜치를 확인할 수 없습니다")
    rows = git_output(vault, "ls-remote", "--exit-code", remote, ref).splitlines()
    revisions = [row.split()[0] for row in rows
                 if len(row.split()) == 2 and row.split()[1] == ref]
    if revisions != [expected_revision]:
        raise PreflightError("볼트 HEAD와 upstream 원격 HEAD가 달라 전송을 보류합니다")
    if git_output(vault, "rev-parse", "--verify", "HEAD") != expected_revision:
        raise PreflightError("확인 중 볼트 HEAD가 변경되어 전송을 보류합니다")
    markdown_clean(vault)
    return expected_revision


def nfc(s):
    return unicodedata.normalize("NFC", s)


def today_kst():
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=9)).date()


def normalize_date(raw, today=None):
    """'2026-08-05' 또는 '8/14' → 'YYYY-MM-DD'. M/D는 45일 이상 지났으면 내년."""
    today = today or today_kst()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.date.fromisoformat(raw).isoformat()
        if not re.fullmatch(r"\d{1,2}/\d{1,2}", raw):
            return None
        m, d = (int(x) for x in raw.split("/"))
        candidate = datetime.date(today.year, m, d)
        if (today - candidate).days > 45:
            candidate = datetime.date(today.year + 1, m, d)
    except ValueError:
        return None
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


def resolve_vault_path(vault, relative):
    """Resolve NFC/NFD names component by component; never silently ignore read errors."""
    if os.path.isabs(relative) or any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("invalid registry vault_note path")
    current = os.path.realpath(vault)
    for part in relative.split("/"):
        try:
            matches = [entry for entry in os.listdir(current) if nfc(entry) == nfc(part)]
        except FileNotFoundError:
            return None
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("ambiguous normalized vault path")
        current = os.path.join(current, matches[0])
        if os.path.commonpath((os.path.realpath(vault), os.path.realpath(current))) != os.path.realpath(vault):
            raise ValueError("vault path escapes root")
    return current


def note_slug(name, repo=None):
    if repo and repo.get("vault_slug"):
        return repo["vault_slug"]
    if name in NOTE_TO_SLUG:
        return NOTE_TO_SLUG[name]
    if repo and not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return os.path.basename(repo["path"].rstrip("/"))
    return name


def collect(vault=VAULT, today=None, registry=None):
    """Registry hubs plus legacy sources; each resolved path is read once."""
    sources = {}

    def add(path, name=None, slug=None):
        if path is None:
            return
        name = name or nfc(os.path.splitext(os.path.basename(path))[0])
        key = nfc(os.path.realpath(path))
        sources.setdefault(key, (path, name, slug or note_slug(name)))

    for repo in repos.load() if registry is None else registry:
        relative = repo.get("vault_note")
        if relative:
            path = resolve_vault_path(vault, relative)
            name = nfc(os.path.splitext(os.path.basename(relative))[0])
            add(path, name, note_slug(name, repo))

    projects = resolve_vault_path(vault, "프로젝트")
    if projects:
        for entry in sorted(os.listdir(projects)):
            if entry.endswith(".md"):
                add(resolve_vault_path(vault, "프로젝트/" + entry))

    add(resolve_vault_path(vault, "이정표/이정표.md"), "이정표", "jeongpyo")
    competitions = resolve_vault_path(vault, "이정표/대회")
    if competitions:
        for entry in sorted(os.listdir(competitions)):
            if entry.endswith(".md"):
                add(resolve_vault_path(vault, "이정표/대회/" + entry), "이정표", "jeongpyo")

    notes = {}
    for path, name, slug in sources.values():
        tasks, deadlines = parse_note(path, today)
        if tasks or deadlines:
            b = notes.setdefault((name, slug), {
                "note": name, "slug": slug, "tasks": [], "deadlines": [],
            })
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
    try:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "x-vault-signature": sig})
        with urllib.request.urlopen(req, timeout=20) as res:
            print(f"POST HTTP {res.status}")
            return 0
    except urllib.error.HTTPError as e:
        print(f"POST 실패 HTTP {e.code}", file=sys.stderr)
        e.close()
        return 1
    except Exception as e:  # URL·응답 본문·예외 원문은 인증정보를 포함할 수 있다.
        print(f"POST 실패 ({type(e).__name__})", file=sys.stderr)
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(VAULT):
        print("볼트 접근 불가 (전체 디스크 접근 권한?)", file=sys.stderr)
        return 1

    preview = args.dry_run or args.dump
    try:
        snapshot_revision = None
        if not preview:
            snapshot_revision = git_output(VAULT, "rev-parse", "--verify", "HEAD")
            markdown_clean(VAULT)
        notes = collect()
    except PreflightError as exc:
        print(f"볼트 동기화 보류: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"볼트 수집 실패 ({type(exc).__name__}): 부분 스냅샷을 전송하지 않습니다", file=sys.stderr)
        return 1
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

    try:
        payload["sourceRevision"] = preflight(VAULT, snapshot_revision)
    except PreflightError as exc:
        print(f"볼트 동기화 보류: {exc}", file=sys.stderr)
        return 1
    rc = post(payload, load_ops_env())
    if rc == 0:
        print(summary)
    return rc


if __name__ == "__main__":
    sys.exit(main())
