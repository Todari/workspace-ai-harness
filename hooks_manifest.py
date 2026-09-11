#!/usr/bin/env python3
"""Claude Code와 Codex에 등록할 훅의 단일 목록.

register_hooks.py(Claude)·register_codex.py(Codex)·doctor.py가 이 목록 하나를 읽는다.
목록을 바꾸면 두 등록 스크립트를 다시 실행하고 doctor.py로 실제 등록 상태를 확인한다.

각 항목의 ``clients``를 생략하면 양쪽에 등록한다. Claude 메인 세션에만 필요한 정책은
``("claude",)``로 제한해 Codex worker에 재귀적으로 주입하지 않는다.
"""
import ast
import hashlib
import os

HARNESS = os.path.dirname(os.path.abspath(__file__))

# Codex(0.151 문서 기준)는 셸 도구를 hook 입력에서 "Bash"로, 파일 편집을 "apply_patch"로 보고한다.
# matcher가 없으면 이벤트 전체에 걸린다.
HOOKS = (
    {"event": "SessionStart", "script": "claude_auto_route.py", "timeout": 5,
     "clients": ("claude",)},
    {"event": "UserPromptSubmit", "script": "claude_handoff_gate.py", "timeout": 5,
     "clients": ("claude",)},
    {"event": "PreToolUse", "script": "claude_handoff_gate.py", "timeout": 5,
     "clients": ("claude",)},
    {"event": "PostToolUse", "script": "claude_handoff_gate.py", "timeout": 5,
     "clients": ("claude",)},
    {"event": "PostToolUseFailure", "script": "claude_handoff_gate.py", "timeout": 5,
     "clients": ("claude",)},
    {"event": "Stop", "script": "claude_handoff_gate.py", "timeout": 5,
     "clients": ("claude",)},
    {"event": "SessionStart", "script": "repo_map.py", "timeout": 10},
    {"event": "SessionStart", "script": "obsidian_bridge.py", "timeout": 10},
    {"event": "SubagentStart", "script": "vault_subagent.py", "timeout": 10},
    {"event": "PostToolUse", "script": "verify_gate.py", "timeout": 5,
     "claude_matcher": "Edit|Write|NotebookEdit|Bash",
     "codex_matcher": "^(Bash|apply_patch|Edit|Write)$"},
    {"event": "Stop", "script": "verify_gate.py", "timeout": 5},
)

# 과거에 등록됐던 스크립트도 소유 목록에 남겨야 재등록 때 낡은 항목이 정리된다.
RETIRED_SCRIPTS = (
    "plan_gate.py", "bash_gate.py", "plan_marker.py", "quick_bypass.py",
    "obsidian_scheduler.py",
)
OWNED_SCRIPTS = tuple(sorted({h["script"] for h in HOOKS} | set(RETIRED_SCRIPTS)))


def hooks_for(client):
    if client not in ("claude", "codex"):
        raise ValueError("unknown hook client: %s" % client)
    return tuple(spec for spec in HOOKS
                 if client in spec.get("clients", ("claude", "codex")))


def claude_hooks():
    return hooks_for("claude")


def codex_hooks():
    return hooks_for("codex")


def script_path(script):
    return os.path.join(HARNESS, script)


def script_revision(script):
    # Follow local Python imports without executing them. A helper edit changes
    # the trust revision of every hook that imports it, including transitively.
    pending, sources = [script], {}
    while pending:
        name = pending.pop()
        if name in sources:
            continue
        with open(script_path(name), "rb") as f:
            sources[name] = f.read()
        for node in ast.walk(ast.parse(sources[name])):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                       else [])
            for module in modules:
                dependency = module.split(".")[0] + ".py"
                if os.path.isfile(script_path(dependency)):
                    pending.append(dependency)
    digest = hashlib.sha256()
    for name, body in sorted(sources.items()):
        digest.update(name.encode() + b"\0" + body + b"\0")
    return digest.hexdigest()[:16]


def claude_command(script):
    return "python3 %s" % script_path(script)


def codex_command(script):
    """Codex는 훅 정의 해시로 신뢰를 기록하므로 코드 리비전을 명령에 붙여 변경 시 재승인을 강제한다."""
    return "python3 %s --harness-rev=%s" % (script_path(script), script_revision(script))


def is_owned_command(command):
    base = command.split(" --harness-rev=")[0]
    return any(base.endswith("/" + script) for script in OWNED_SCRIPTS)
