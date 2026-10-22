#!/usr/bin/env python3
"""옵시디언 볼트 검색 — Claude/Codex/서브에이전트 공용 읽기 인터페이스.

이 볼트는 iCloud Drive에 있어서 BSD `grep -r`이 조용히 0건을 반환한다(재귀 탐색 실패).
한글 파일명도 NFD로 저장돼 있어 문자열 비교 전 NFC 정규화가 필요하다. 이 스크립트는
두 함정을 모두 처리하므로, 볼트를 뒤질 때는 grep 대신 이것을 쓴다.

    python3 vault_search.py "타임아웃"              # 전체 검색
    python3 vault_search.py "hydration" -t 트러블슈팅  # 타입 필터
    python3 vault_search.py "배낭" --full           # 매칭 노트 전문 출력
    python3 vault_search.py --list 프로젝트          # 특정 타입 노트 목록

출력은 컴팩트하다(노트당 경로 + 매칭 줄 발췌). 읽기 실패는 부분 결과와 exit 1로 알린다.
"""
import argparse
import os
import re
import sys
import unicodedata

import harness_lib as lib

VAULT = lib.VAULT_ROOT
SKIP = {".obsidian", ".trash", ".git"}
MAX_NOTES = 20
MAX_HITS_PER_NOTE = 3
CONTEXT_CHARS = 160


def nfc(s):
    return unicodedata.normalize("NFC", s)


def iter_notes(include_archive, onerror=None):
    for root, dirs, files in os.walk(VAULT, onerror=onerror):
        dirs[:] = [d for d in dirs if d not in SKIP]
        rel_root = os.path.relpath(root, VAULT)
        if not include_archive and rel_root.split(os.sep)[0] == "Archive":
            continue
        for f in sorted(files):
            if f.endswith(".md"):
                yield os.path.join(root, f)


def note_type(body):
    m = re.match(r"---\n(.*?)\n---", body, re.S)
    if not m:
        return ""
    t = re.search(r"^type:\s*(.+)$", m.group(1), re.M)
    return t.group(1).strip() if t else ""


def read(path, onerror=None):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError as exc:
        if onerror:
            onerror(exc)
        return None


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("query", nargs="?", default="", help="검색어 (대소문자 무시)")
    ap.add_argument("-t", "--type", default="", help="frontmatter type 필터")
    ap.add_argument("--list", dest="list_type", default="",
                    help="해당 type의 노트 목록만 출력")
    ap.add_argument("--full", action="store_true", help="매칭 노트 전문 출력")
    ap.add_argument("--archive", action="store_true", help="Archive/ 포함")
    args = ap.parse_args()

    if not args.query and not args.list_type:
        ap.print_help()
        return 2
    if not os.path.isdir(VAULT):
        print("볼트 접근 불가 — 경로·권한·iCloud 상태를 확인하세요", file=sys.stderr)
        return 1

    failures = 0

    def onerror(exc):
        nonlocal failures
        failures += 1

    def finish():
        if failures:
            print(f"볼트 검색 불완전 — 파일/폴더 읽기 실패 {failures}건. "
                  "표시된 결과는 부분 결과이며 경로·권한·iCloud 상태를 확인하세요.",
                  file=sys.stderr)
        return int(bool(failures))

    # --list: 타입별 목록
    if args.list_type:
        rows = []
        for p in iter_notes(args.archive, onerror):
            body = read(p, onerror)
            if body is not None and nfc(note_type(body)) == nfc(args.list_type):
                rows.append(os.path.relpath(p, VAULT))
        print(f"type={args.list_type}: {len(rows)}건" + (" (부분 결과)" if failures else ""))
        for r in sorted(rows):
            print(f"  {r}")
        return finish()

    q = nfc(args.query).lower()
    results = []
    for p in iter_notes(args.archive, onerror):
        body = read(p, onerror)
        if body is None:
            continue
        if args.type and nfc(note_type(body)) != nfc(args.type):
            continue
        hay = nfc(body).lower()
        name = nfc(os.path.basename(p)).lower()
        if q not in hay and q not in name:
            continue
        hits = []
        for m in re.finditer(re.escape(q), hay):
            s = max(0, m.start() - CONTEXT_CHARS // 2)
            snippet = " ".join(nfc(body)[s:s + CONTEXT_CHARS].split())
            hits.append(snippet)
            if len(hits) >= MAX_HITS_PER_NOTE:
                break
        results.append((os.path.relpath(p, VAULT), note_type(body), hits, body))

    if not results:
        if failures:
            print(f'"{args.query}" 관련 노트 없음 — 읽기 실패로 전체 결과를 확인하지 못했습니다.')
        else:
            print(f'"{args.query}" 관련 노트 0건.')
        return finish()

    print(f'"{args.query}" 관련 노트 {len(results)}건'
          + (" (부분 결과)" if failures else "")
          + (f" (상위 {MAX_NOTES}건 표시)" if len(results) > MAX_NOTES else ""))
    for rel, typ, hits, body in results[:MAX_NOTES]:
        print(f"\n## {rel}" + (f"  [type: {typ}]" if typ else ""))
        if args.full:
            print(body.rstrip())
        else:
            for h in hits:
                print(f"  … {h} …")
    return finish()


if __name__ == "__main__":
    sys.exit(main())
