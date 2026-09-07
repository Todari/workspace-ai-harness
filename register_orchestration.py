#!/usr/bin/env python3
"""Claude용 Fable→Codex 오케스트레이션 자산을 workspace에 동기화한다.

관리 마커가 없는 기존 파일은 덮어쓰지 않는다. 최초 도입 시에는 파일을 직접 검토한 뒤
마커를 추가하거나 대상 파일을 치워야 한다.
"""
import json
import os
import shutil
import sys

import harness_lib as lib
import codex_worker

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.join(HERE, "orchestration", "claude")
TARGET_ROOT = os.path.join(lib.WORKSPACE_ROOT, ".claude")
MANAGED_MARKER = "<!-- workspace-harness: managed orchestration -->"
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
ASSETS = (
    ("agents/fable-architect.md", "agents/fable-architect.md"),
    ("commands/delegate-codex.md", "commands/delegate-codex.md"),
    ("commands/bounded-ultracode.md", "commands/bounded-ultracode.md"),
)


def asset_paths(source_root=SOURCE_ROOT, target_root=TARGET_ROOT):
    return [(os.path.join(source_root, src), os.path.join(target_root, dst))
            for src, dst in ASSETS]


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def is_synced(source, target):
    try:
        return read_text(source) == read_text(target)
    except OSError:
        return False


def sync_asset(source, target):
    content = read_text(source)
    if MANAGED_MARKER not in content:
        raise ValueError("관리 마커가 없는 소스: %s" % source)
    existed = os.path.exists(target)
    if existed:
        current = read_text(target)
        if current == content:
            return "unchanged"
        if MANAGED_MARKER not in current:
            raise ValueError("관리되지 않는 기존 파일 보존: %s" % target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target)
    return "updated" if existed else "created"


def update_planner_effort(settings, config):
    """메인 Fable의 실제 기본 effort를 추적 설정과 맞춘다.

    모델별 저장값이 top-level effort보다 우선하므로 canonical 모델과 현재 선택된
    1M 변형을 함께 갱신한다. 다른 모델의 사용자 설정은 보존한다.
    """
    planner = config["planner"]
    effort = planner["default_effort"]
    runtime_model = planner["runtime_model"]
    changed = []
    if settings.get("effortLevel") != effort:
        settings["effortLevel"] = effort
        changed.append("effortLevel")
    models = settings.setdefault("modelSettings", {})
    targets = [runtime_model]
    current = settings.get("model", "")
    if isinstance(current, str) and current.startswith(runtime_model):
        targets.append(current)
    for model in targets:
        row = models.setdefault(model, {})
        if row.get("effortLevel") != effort:
            row["effortLevel"] = effort
            changed.append("modelSettings.%s" % model)
    return changed


def sync_planner_effort(settings_path=CLAUDE_SETTINGS, config=None):
    config = config or codex_worker.load_config()
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    changed = update_planner_effort(settings, config)
    if changed:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return changed


def main():
    failed = False
    for source, target in asset_paths():
        try:
            state = sync_asset(source, target)
            print("%-9s %s" % (state, target))
        except (OSError, ValueError) as exc:
            failed = True
            print("blocked   %s" % exc, file=sys.stderr)
    try:
        changed = sync_planner_effort()
        print("%-9s Claude planner effort: %s" % (
            "updated" if changed else "unchanged",
            codex_worker.load_config()["planner"]["default_effort"]))
    except (OSError, ValueError, codex_worker.WorkerError) as exc:
        failed = True
        print("blocked   Claude planner effort: %s" % exc, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
