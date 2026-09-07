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

import codex_worker
import harness_lib as lib
import hooks_manifest as manifest
import register_orchestration
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
    for spec in manifest.claude_hooks():
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
    for spec in manifest.codex_hooks():
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


def check_orchestration(r):
    print("[Fable → Codex orchestration] %s" % codex_worker.CONFIG_PATH)
    try:
        config = codex_worker.load_config()
    except codex_worker.WorkerError as exc:
        r.bad(str(exc))
        return
    worker = config["worker"]
    routing = config["routing"]
    planner = config["planner"]
    r.ok("active worker %s/%s (reviewed %s)" % (
        worker["model"], worker["default_effort"], config.get("last_reviewed", "unknown")))
    r.ok("Claude routing %s — nontrivial implementation → Codex" % routing["mode"])
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            claude_settings = json.load(f)
        runtime_model = planner["runtime_model"]
        saved = claude_settings.get("modelSettings", {}).get(runtime_model, {}).get("effortLevel")
        if claude_settings.get("effortLevel") == planner["default_effort"] \
                and saved == planner["default_effort"]:
            r.ok("Claude planner 실제 기본 effort %s" % saved)
        else:
            r.bad("Claude planner effort 드리프트 — python3 register_orchestration.py")
    except (OSError, ValueError) as exc:
        r.bad("Claude planner effort 확인 실패: %s" % exc)
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        claude_version = proc.stdout.strip() if proc.returncode == 0 else "unavailable"
    except (OSError, subprocess.SubprocessError):
        claude_version = "unavailable"
    if claude_version == "unavailable":
        r.bad("claude CLI를 실행할 수 없음")
    elif planner["tested_cli_version"] not in claude_version:
        r.warn("claude %s, 검증 기준 %s — 훅/effort 동작 재검토" % (
            claude_version, planner["tested_cli_version"]))
    else:
        r.ok("claude %s (검증 기준 일치)" % claude_version)
    try:
        reviewed = time.mktime(time.strptime(config["last_reviewed"], "%Y-%m-%d"))
        review_age = int((time.time() - reviewed) / 86400)
        if review_age > config["review_interval_days"]:
            r.warn("모델 라우팅 검토 후 %d일 — 업그레이드 체크리스트 실행" % review_age)
    except (KeyError, TypeError, ValueError):
        r.bad("last_reviewed 또는 review_interval_days 형식 오류")
    for source, target in register_orchestration.asset_paths():
        if register_orchestration.is_synced(source, target):
            r.ok("Claude asset %s" % os.path.relpath(target, register_orchestration.TARGET_ROOT))
        else:
            r.bad("Claude asset 어긋남: %s — python3 register_orchestration.py" % target)
    if not os.path.exists(codex_worker.RESULT_SCHEMA_PATH):
        r.bad("Codex 결과 스키마 없음: %s" % codex_worker.RESULT_SCHEMA_PATH)
    else:
        try:
            with open(codex_worker.RESULT_SCHEMA_PATH, encoding="utf-8") as f:
                schema = json.load(f)
            if schema.get("type") == "object" and schema.get("additionalProperties") is False:
                r.ok("Codex 결과 스키마")
            else:
                r.bad("Codex 결과 스키마가 strict object가 아님")
        except (OSError, ValueError) as exc:
            r.bad("Codex 결과 스키마 읽기 실패: %s" % exc)
    version = codex_worker.codex_version()
    if version == "unavailable":
        r.bad("codex CLI를 실행할 수 없음")
    elif worker["tested_cli_version"] not in version:
        r.warn("codex %s, 검증 기준 %s — 업그레이드 체크리스트 실행" % (
            version, worker["tested_cli_version"]))
    else:
        r.ok("%s (검증 기준 일치)" % version)


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
    check_orchestration(r)
    check_hooks_run(r)
    check_context(r)
    check_logs(r)
    print("문제 %d개" % r.problems)
    return 1 if r.problems else 0


if __name__ == "__main__":
    sys.exit(main())
