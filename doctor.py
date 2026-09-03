#!/usr/bin/env python3
"""하네스 자가 진단. 사용: python3 doctor.py

등록 스크립트·매니페스트·실제 설정 사이의 드리프트를 한 번에 잡는다.
문제가 하나라도 있으면 exit 1.
"""
import json
import os
import re
import subprocess
import sys
import time

import harness_lib as lib
import hooks_manifest as manifest
import repo_map
import repos
import sync_context

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_CONFIG = os.path.join(sync_context.CODEX_HOME, "config.toml")
PROBLEM = "✗"
OK = "✓"
WARN = "△"


class Report:
    def __init__(self):
        self.problems = 0

    def ok(self, msg):
        print("  %s %s" % (OK, msg))

    def warn(self, msg):
        print("  %s %s" % (WARN, msg))

    def bad(self, msg):
        self.problems += 1
        print("  %s %s" % (PROBLEM, msg))


def check_python(r):
    print("[python]")
    version = sys.version_info
    if version < (3, 8):
        r.bad("python3 %d.%d — 3.8 이상 필요" % version[:2])
    else:
        r.ok("python3 %d.%d (%s)" % (version[0], version[1], sys.executable))


def check_claude(r):
    print("[claude hooks] %s" % CLAUDE_SETTINGS)
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            hooks = json.load(f).get("hooks", {})
    except (OSError, ValueError) as exc:
        r.bad("settings.json 읽기 실패: %s" % exc)
        return
    registered = {}
    for event, entries in hooks.items():
        for entry in entries:
            for handler in entry.get("hooks", []):
                command = handler.get("command", "")
                if manifest.is_owned_command(command):
                    registered[(event, command, entry.get("matcher"))] = True
    expected = set()
    for spec in manifest.HOOKS:
        key = (spec["event"], manifest.claude_command(spec["script"]), spec.get("claude_matcher"))
        expected.add(key)
        if key in registered:
            r.ok("%s %s" % (spec["event"], spec["script"]))
        else:
            r.bad("%s %s 미등록 — python3 register_hooks.py" % (spec["event"], spec["script"]))
    for key in registered:
        if key not in expected:
            r.bad("낡은 등록 %s %s — python3 register_hooks.py 로 정리" % (key[0], os.path.basename(key[1])))


def check_codex(r):
    print("[codex hooks] %s" % CODEX_CONFIG)
    try:
        with open(CODEX_CONFIG, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        r.warn("config.toml 없음 — Codex 미사용이면 정상")
        return
    if "# BEGIN workspace-harness" not in text:
        r.bad("관리 블록 없음 — python3 register_codex.py")
        return
    for spec in manifest.HOOKS:
        command = manifest.codex_command(spec["script"])
        if command in text:
            r.ok("%s %s (rev 일치)" % (spec["event"], spec["script"]))
        else:
            stale = re.search(r"%s --harness-rev=([0-9a-f]+)" % re.escape(manifest.script_path(spec["script"])), text)
            if stale:
                r.bad("%s %s rev 불일치(등록 %s) — python3 register_codex.py 후 Codex에서 재승인" % (
                    spec["event"], spec["script"], stale.group(1)))
            else:
                r.bad("%s %s 미등록 — python3 register_codex.py" % (spec["event"], spec["script"]))
    if not sync_context.is_synced(sync_context.CODEX_GLOBAL):
        r.bad("~/.codex/AGENTS.md 가 원본과 다름 — python3 sync_context.py")
    else:
        r.ok("~/.codex/AGENTS.md 동기화됨")


def check_sync(r):
    print("[instruction sync] source=%s" % sync_context.SOURCE)
    try:
        content = sync_context.rendered_source()
    except OSError as exc:
        r.bad("원본 읽기 실패: %s" % exc)
        return
    for target in sync_context.TARGETS:
        if sync_context.is_synced(target, content):
            r.ok(target)
        else:
            r.bad("%s 어긋남 — python3 sync_context.py" % target)


def check_hooks_run(r):
    """각 훅을 빈 입력으로 실행해 fail-open(exit 0, 출력 없음)을 확인한다."""
    print("[hook smoke]")
    for script in sorted({spec["script"] for spec in manifest.HOOKS}):
        try:
            proc = subprocess.run([sys.executable, manifest.script_path(script)],
                                  input="{}", capture_output=True, text=True, timeout=15)
        except (subprocess.SubprocessError, OSError) as exc:
            r.bad("%s 실행 실패: %s" % (script, exc))
            continue
        if proc.returncode != 0 or proc.stdout.strip():
            r.bad("%s exit=%d stdout=%r" % (script, proc.returncode, proc.stdout[:80]))
        else:
            r.ok(script)


def check_context(r):
    print("[repo registry & context] %s" % repos.REGISTRY)
    registry = repos.load()
    if not registry:
        r.warn("repos.json 없음 — 레포 표·볼트 매핑 비활성")
    for repo in registry:
        root = repos.abs_path(repo)
        if not os.path.isdir(root):
            r.bad("%s 경로 없음: %s" % (repo["name"], repo["path"]))
            continue
        doc = repo_map.deep_context_path(root)
        if os.path.exists(doc):
            warning = repo_map.staleness_warning(root, doc)
            if warning:
                r.warn("%s 심층 문서 노후: %s" % (repo["name"], warning.split(" — ")[0].lstrip("⚠️ ")))
        note = repo.get("vault_note")
        if note and not os.path.exists(os.path.join(lib.VAULT_ROOT, note)):
            r.warn("%s 볼트 노트 없음: %s" % (repo["name"], note))
    unregistered = [row for row in repos.active(30) if not row["registered"]]
    for row in unregistered[:8]:
        r.warn("최근 30일 활동 %d커밋인데 미등록: %s" % (row["commits"], row["path"]))
    if registry and not unregistered:
        r.ok("활동 중인 레포는 모두 등록됨")


def check_logs(r):
    print("[logs]")
    cutoff = time.time() - 7 * 86400
    recent = 0
    try:
        with open(lib.LOG_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ts = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    continue
                if ts >= cutoff and "fail-open" in line:
                    recent += 1
    except OSError:
        pass
    if recent:
        r.warn("최근 7일 fail-open %d회 — tail %s" % (recent, lib.LOG_PATH))
    else:
        r.ok("최근 7일 fail-open 없음")


def main():
    r = Report()
    check_python(r)
    check_claude(r)
    check_codex(r)
    check_sync(r)
    check_hooks_run(r)
    check_context(r)
    check_logs(r)
    print("문제 %d개" % r.problems)
    return 1 if r.problems else 0


if __name__ == "__main__":
    sys.exit(main())
