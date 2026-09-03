#!/usr/bin/env python3
"""~/.claude/settings.json에 하네스 훅을 멱등하게 등록한다. 목록은 hooks_manifest.py."""
import json
import os

import hooks_manifest as manifest

SETTINGS = os.path.expanduser("~/.claude/settings.json")
HARNESS = manifest.HARNESS


def is_owned_handler(handler):
    return manifest.is_owned_command(handler.get("command", ""))


def remove_owned(hooks):
    """소유 handler만 제거하고 같은 matcher 그룹의 타 훅은 보존한다."""
    removed = 0
    for event in list(hooks):
        kept_entries = []
        for entry in hooks[event]:
            handlers = entry.get("hooks", [])
            kept_handlers = [h for h in handlers if not is_owned_handler(h)]
            removed += len(handlers) - len(kept_handlers)
            if kept_handlers:
                entry["hooks"] = kept_handlers
                kept_entries.append(entry)
        if kept_entries:
            hooks[event] = kept_entries
        else:
            del hooks[event]
    return removed


def update_settings(settings):
    hooks = settings.setdefault("hooks", {})
    removed = remove_owned(hooks)
    installed = []
    for spec in manifest.HOOKS:
        entry = {"hooks": [{
            "type": "command",
            "command": manifest.claude_command(spec["script"]),
            "timeout": spec["timeout"],
        }]}
        if spec.get("claude_matcher"):
            entry["matcher"] = spec["claude_matcher"]
        hooks.setdefault(spec["event"], []).append(entry)
        installed.append("%s:%s" % (spec["event"], spec["script"]))
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
