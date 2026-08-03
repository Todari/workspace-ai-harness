#!/usr/bin/env python3
"""~/.claude/settings.json에 공용 workspace 하네스 훅을 멱등하게 등록."""
import json
import os

SETTINGS = os.path.expanduser("~/.claude/settings.json")
HARNESS = os.path.expanduser("~/.claude/hooks/workspace-harness")
INCOMPATIBLE_PLUGIN = "superpowers@claude-plugins-official"

ENTRIES = [
    ("SessionStart", None, "repo_map.py", 10),
    ("PostToolUse", "Edit|Write|NotebookEdit|Bash", "verify_gate.py", 5),
    ("Stop", None, "verify_gate.py", 5),
]

OWNED_SCRIPTS = {
    "repo_map.py", "quick_bypass.py", "plan_gate.py", "bash_gate.py",
    "plan_marker.py", "verify_gate.py", "obsidian_scheduler.py",
}


def is_owned_handler(handler):
    command = handler.get("command", "")
    return any(command.endswith("/" + script) for script in OWNED_SCRIPTS)


def update_settings(settings):
    """소유 handler만 교체하고 같은 matcher 그룹의 타 훅은 보존한다."""
    settings.setdefault("enabledPlugins", {})[INCOMPATIBLE_PLUGIN] = False
    hooks = settings.setdefault("hooks", {})
    removed = 0
    for event in list(hooks):
        kept_entries = []
        for entry in hooks[event]:
            handlers = entry.get("hooks", [])
            kept_handlers = [handler for handler in handlers if not is_owned_handler(handler)]
            removed += len(handlers) - len(kept_handlers)
            if kept_handlers:
                entry["hooks"] = kept_handlers
                kept_entries.append(entry)
        if kept_entries:
            hooks[event] = kept_entries
        else:
            del hooks[event]

    installed = []
    for event, matcher, script, timeout in ENTRIES:
        command = "python3 %s/%s" % (HARNESS, script)
        entry = {"hooks": [{
            "type": "command",
            "command": command,
            "timeout": timeout,
        }]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
        installed.append("%s:%s" % (event, script))
    return removed, installed


def main():
    with open(SETTINGS, encoding="utf-8") as f:
        settings = json.load(f)
    removed, installed = update_settings(settings)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("removed owned handlers:", removed, "| installed:", installed)


if __name__ == "__main__":
    main()
