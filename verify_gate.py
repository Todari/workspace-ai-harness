#!/usr/bin/env python3
"""편집-후-검증 게이트.

PreToolUse: 검증을 시작할 때 워크트리별 편집 세대를 기록.
PostToolUse: 소스 편집과 확정된 검증 결과 기록.
Stop: 마지막 소스 편집 이후 검증이 없으면 편집 세대당 1회만 검증을 지시한다.
"""
from datetime import datetime
import fcntl
import hashlib
import json
import os
import re
import shlex
import tempfile
import time

import context_watch
import edit_detect
import harness_lib as lib
import repos

VERIFY_DIR = os.path.join(lib.CACHE_DIR, "verify-gate")
PATH_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
PATCH_EDIT_TOOLS = ("apply_patch", "ApplyPatch")
SHELL_TOOLS = ("Bash", "exec_command", "shell", "shell_command", "unified_exec")
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
PACKAGE_RUNNERS = {"npx", "bunx"}
VERIFY_SCRIPT_RE = re.compile(
    r"^(?:test|build|lint|type-?check|typecheck|check|verify|ci)(?::|$)", re.IGNORECASE)
DIRECT_VERIFY_TOOLS = {
    "tsc", "vitest", "jest", "playwright", "pytest", "py.test", "mypy",
    "eslint", "biome", "golangci-lint",
}
OPTIONS_WITH_VALUES = {
    "--filter", "-F", "--dir", "-C", "--workspace", "-w", "--prefix",
    "--cwd", "--directory", "--project", "-p", "--config",
}
NON_SOURCE_DOC_NAMES = {
    "agents.md", "claude.md", "readme.md", "changelog.md", "license",
    "license.md", "contributing.md",
}
NON_SOURCE_DOC_SUFFIXES = (".md", ".txt", ".rst", ".adoc")
VERIFY_MESSAGE = (
    "[workspace harness] 마지막 소스 변경 뒤 검증 성공 기록을 찾지 못했습니다. "
    "이미 실행했다면 해당 변경 이후의 완료 결과·종료 코드를 확인하세요. "
    "성공이 확인되면 재실행 없이 결과를 보고하고 종료해도 됩니다. "
    "결과가 없거나 실패했다면 레포 완료 기준 또는 관련 typecheck·test·build를 실행하세요."
)


def _state_path(session_id):
    return os.path.join(VERIFY_DIR, session_id.replace(os.sep, "_") + ".json")


def load_state(session_id):
    try:
        with open(_state_path(session_id), encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("roots", {})
    state.setdefault("pending", {})
    state.setdefault("finished", {})
    return state


def save_state(session_id, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=VERIFY_DIR, encoding="utf-8", delete=False) as f:
        tmp = f.name
        try:
            json.dump(state, f)
            f.close()
            os.replace(tmp, _state_path(session_id))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def is_verify_command(command):
    return any(_is_verify_argv(argv) for argv, _ in _command_segments(command))


def _command_segments(command):
    lines = edit_detect.strip_heredoc_bodies(command).splitlines()
    for line in lines:
        current = []
        for token in edit_detect.tokens(line):
            if token in ("|", "||", "&&", ";", "&"):
                if current:
                    yield current, token
                    current = []
            else:
                current.append(token)
        if current:
            yield current, ";"


def _without_options(args):
    remaining = list(args)
    while remaining and remaining[0].startswith("-"):
        option = remaining.pop(0)
        if "=" not in option and option in OPTIONS_WITH_VALUES and remaining:
            remaining.pop(0)
    return remaining


def _without_prefixes(argv):
    args = list(argv)
    if args and args[0] == "env":
        args.pop(0)
        args = _without_options(args)
    while args and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[0]):
        args.pop(0)
    while args and os.path.basename(args[0]) in ("command", "time", "sudo"):
        args.pop(0)
        args = _without_options(args)
    return args


def _is_verify_argv(argv):
    args = _without_prefixes(argv)
    if not args:
        return False
    tool = os.path.basename(args[0]).lower()
    rest = args[1:]

    if tool in ("sh", "bash", "zsh"):
        return is_verify_command(_shell_wrapper(args))
    if tool in ("uv", "poetry", "pipenv"):
        rest = _without_options(rest)
        if rest and rest[0] == "run":
            return _is_verify_argv(_without_options(rest[1:]))
        return False

    if tool in PACKAGE_MANAGERS:
        rest = _without_options(rest)
        if not rest:
            return False
        action = rest[0].lower()
        if action == "run" and len(rest) > 1:
            return bool(VERIFY_SCRIPT_RE.match(rest[1]))
        if VERIFY_SCRIPT_RE.match(action):
            return True
        if action in DIRECT_VERIFY_TOOLS:
            return True
        if action in ("exec", "dlx", "x"):
            return _is_verify_argv(_without_options(rest[1:]))
        return False

    if tool in PACKAGE_RUNNERS:
        return _is_verify_argv(_without_options(rest))
    if tool == "turbo":
        rest = _without_options(rest)
        if rest and rest[0] == "run":
            rest = rest[1:]
        return bool(rest) and bool(VERIFY_SCRIPT_RE.match(rest[0]))
    if tool in DIRECT_VERIFY_TOOLS:
        return True
    if tool.startswith("python"):
        return any(rest[i:i + 2] and rest[i] == "-m"
                   and rest[i + 1] in ("pytest", "unittest", "compileall")
                   for i in range(len(rest) - 1))
    if tool == "go":
        return bool(rest) and rest[0] in ("test", "vet", "build")
    if tool == "cargo":
        rest = _without_options(rest)
        return bool(rest) and rest[0] in ("test", "check", "clippy", "build")
    if tool in ("gradle", "gradlew") or tool.endswith("gradlew"):
        return any(VERIFY_SCRIPT_RE.match(part.rsplit(":", 1)[-1]) for part in rest)
    if tool in ("mvn", "mvnw") or tool.endswith("mvnw"):
        return any(part in ("test", "verify", "package") for part in rest)
    if tool in ("flutter", "dart"):
        return bool(rest) and rest[0] in ("test", "analyze", "build")
    if tool in ("swift", "dotnet", "mix", "make", "rake"):
        return any(VERIFY_SCRIPT_RE.match(part.rsplit(":", 1)[-1]) for part in rest)
    if tool in ("next", "vite"):
        return bool(rest) and rest[0] == "build"
    if tool == "ruff":
        return bool(rest) and rest[0] == "check"
    if tool == "prettier":
        return "--check" in rest
    return False


# Codex는 실패한 셸 명령도 PostToolUse를 발화한다. 이 마커들은
# `tsc || true` 류 합성 명령(전체 exit 0)의 실패 출력을 걸러내는 보조선이다.
FAILURE_MARKERS = ("error TS", "npm ERR!", "Traceback (most recent call last)",
                   "FAILED", "FAIL ", "ELIFECYCLE")


STATUS_RE = re.compile(
    r"\A(?:Chunk ID: [^\n]+\n)?(?:Wall time: [0-9.]+ seconds\n)?"
    r"(?:Process exited with code |Exit code: ?)(-?\d+)(?:\r?\n|$)")
RUNNING_RE = re.compile(
    r"\A(?:Chunk ID: [^\n]+\n)?(?:Wall time: [0-9.]+ seconds\n)?"
    r"Process running with session ID ([^\s]+)")
ROLLOUT_HEADER_RE = re.compile(
    r"\A(?:Chunk ID: [^\n]+\n)?Wall time: [0-9.]+ seconds\nProcess (?:exited with code |running with session ID )")


def _response_text(response):
    if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict):
        return response[0].get("text")
    return response


def response_status(response):
    """Return (exit status, process id). Unknown output has neither."""
    response = _response_text(response)
    if isinstance(response, str):
        if response.lstrip().startswith("{"):
            try:
                decoded = json.loads(response)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                return response_status(decoded)
        running = RUNNING_RE.match(response)
        if running:
            return None, running[1]
        match = STATUS_RE.match(response)
        if any(marker in response for marker in FAILURE_MARKERS):
            return 1, None
        return (int(match[1]), None) if match else (None, None)
    if not isinstance(response, dict):
        return None, None
    if any(response.get(flag) for flag in ("interrupted", "timed_out", "signal", "is_error", "isError")):
        return 1, None
    status = response.get("exit_code", response.get("exitCode"))
    if isinstance(status, str) and re.fullmatch(r"-?\d+", status):
        status = int(status)
    running = response.get("session_id") or response.get("backgroundTaskId") or response.get("background_task_id")
    if status is None and running:
        return None, str(running)
    output = "".join(str(response.get(key) or "") for key in (
        "stdout", "stderr", "output", "aggregated_output", "content", "text"))
    if any(marker in output for marker in FAILURE_MARKERS):
        return 1, None
    if type(status) is int:
        return status, None
    # Claude's successful Bash PostToolUse uses this schema without an exit code.
    if ("exit_code" not in response and "exitCode" not in response
            and response.get("interrupted") is False
            and isinstance(response.get("stdout"), str) and isinstance(response.get("stderr"), str)):
        return 0, None
    return None, None


def is_successful_response(tool_response):
    return response_status(tool_response)[0] == 0


def _has_script(pkg, name):
    return isinstance(pkg.get("scripts"), dict) and name in pkg["scripts"]


def _pkg_manager(root):
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("bun.lock", "bun")):
        if os.path.exists(os.path.join(root, lock)):
            return pm
    return "npm"


def suggest_verify_commands(root):
    """레포 루트의 설정 파일로 검증 명령을 추정한다. repos.json의 `verify`가 있으면 그것을 우선한다."""
    if not root:
        return []
    real = os.path.realpath(root)
    try:
        known = repos.load()
    except Exception:
        known = []
    for repo in known if isinstance(known, list) else known.get("repos", []):
        if not isinstance(repo, dict):
            continue
        if os.path.realpath(repos.abs_path(repo)) == real and repo.get("verify"):
            verify = repo["verify"]
            return list(verify) if isinstance(verify, list) else [str(verify)]
    cmds = []
    pkg_path = os.path.join(real, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
        except (OSError, ValueError):
            pkg = {}
        pm = _pkg_manager(real)
        for name in ("typecheck", "type-check", "lint", "test", "build"):
            if _has_script(pkg, name):
                cmds.append("%s test" % pm if name == "test" else "%s run %s" % (pm, name))
    py = ".venv/bin/python" if os.path.exists(os.path.join(real, ".venv", "bin", "python")) else "python3"
    has_pytest = any(os.path.exists(os.path.join(real, n)) for n in ("pytest.ini", "conftest.py")) or \
        _file_contains(os.path.join(real, "pyproject.toml"), "pytest")
    if has_pytest:
        cmds.append("%s -m pytest -q" % py)
    elif os.path.isdir(os.path.join(real, "tests")) and any(
            n.startswith("test") and n.endswith(".py") for n in _listdir(os.path.join(real, "tests"))):
        cmds.append("%s -m unittest discover -s tests" % py)
    if _file_contains(os.path.join(real, "Makefile"), "\ntest:"):
        cmds.append("make test")
    if os.path.exists(os.path.join(real, "Cargo.toml")):
        cmds.append("cargo test")
    if os.path.exists(os.path.join(real, "go.mod")):
        cmds.append("go test ./...")
    return cmds


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _file_contains(path, needle):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return needle in f.read()
    except OSError:
        return False


def repo_root_of(path):
    """path에서 위로 올라가며 workspace 안의 가장 가까운 git 루트를 찾는다."""
    if not path:
        return ""
    cur = os.path.realpath(path)
    if not os.path.isdir(cur):
        cur = os.path.dirname(cur)
    while lib.in_workspace(cur):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def edit_target_path(tool_name, tool_input, cwd=""):
    tool_input = tool_input or {}
    try:
        path = edit_detect.target_path(tool_input)
    except Exception:
        path = None
    if not path and tool_name in SHELL_TOOLS:
        described = edit_detect.find_write_target(command_from(tool_input), cwd) or ""
        path = described.split("→", 1)[1].strip() if "→" in described else ""
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(cwd or "", path)


def block_reason(*roots):
    lines = [VERIFY_MESSAGE]
    for root in filter(None, roots):
        lines.append("\n" + root)
        lines.extend("  - `%s`" % cmd for cmd in suggest_verify_commands(root))
    return "\n".join(lines)


def command_from(tool_input):
    tool_input = tool_input or {}
    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(command, list) and all(isinstance(arg, str) for arg in command):
        return shlex.join(command)
    return command if isinstance(command, str) else ""


def tool_cwd(tool_input, cwd):
    workdir = tool_input.get("workdir") or tool_input.get("cwd")
    return os.path.realpath(os.path.join(cwd, workdir)) if isinstance(workdir, str) else cwd


def patch_input(tool_input):
    return tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or ""


def patch_paths(patch):
    if not isinstance(patch, str):
        return []
    return re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.MULTILINE)


def is_verification_relevant_path(path):
    if not path or not edit_detect.is_workspace_source(os.path.realpath(path)):
        return False
    name = os.path.basename(path).lower()
    return name not in NON_SOURCE_DOC_NAMES and not name.endswith(NON_SOURCE_DOC_SUFFIXES)


def _shell_wrapper(argv):
    if not argv or os.path.basename(argv[0]) not in ("sh", "bash", "zsh"):
        return ""
    for i, arg in enumerate(argv[1:-1], start=1):
        if not arg.startswith("-") or arg == "--":
            break
        if re.fullmatch(r"-[a-z]*c[a-z]*", arg):
            return argv[i + 1]
    return ""


def _package_cwd(argv, cwd):
    if not argv or os.path.basename(argv[0]) not in (PACKAGE_MANAGERS | {"make", "uv"}):
        return cwd
    options = {"--cwd", "--dir", "--prefix", "--directory", "-C"}
    for i, arg in enumerate(argv[1:], start=1):
        flag, equal, value = arg.partition("=")
        if flag in options:
            target = value if equal else (argv[i + 1] if i + 1 < len(argv) else "")
            return edit_detect.resolve(target, cwd)
    return cwd


def shell_operations(command, cwd):
    """Find static shell edit targets and checks whose status propagates to the result."""
    edits, verifies = [], []
    segments = list(_command_segments(command))
    # A successful outer shell establishes the final command and preceding && chain.
    last = len(segments) - 1
    first = last
    while first > 0 and segments[first - 1][1] == "&&":
        first -= 1
    if first > 0 and segments[first - 1][1] in ("|", "||", "&"):
        first = len(segments)
    if segments and segments[-1][1] in ("&", "&&", "||", "|"):
        first = len(segments)

    for i, (segment, _) in enumerate(segments):
        argv = _without_prefixes(segment)
        if argv and argv[0] in ("pushd", "popd"):
            cwd = None
            continue
        if argv[:1] == ["cd"] and len(argv) == 2:
            previous = segments[i - 1][1] if i else ";"
            if segments[i][1] in ("|", "&") or previous == "|":
                continue
            if previous in ("&&", "||") and i < first:
                cwd = None
            cwd = edit_detect.resolve(argv[1], cwd) if cwd else None
            if cwd and not os.path.isdir(cwd):
                cwd = None
            continue
        if not cwd:
            continue
        edits.extend(((i,), path) for _, path in edit_detect.write_targets(segment, cwd))
        nested = _shell_wrapper(argv)
        if nested:
            nested_edits, nested_verifies = shell_operations(nested, cwd)
            edits.extend(((i,) + pos, path) for pos, path in nested_edits)
            if i >= first:
                verifies.extend(((i,) + pos, target) for pos, target in nested_verifies)
        elif i >= first and _is_verify_argv(argv):
            target = _package_cwd(argv, cwd)
            if target:
                verifies.append(((i,), target))
    return edits, verifies


def _scope(path, cwd):
    root = repo_root_of(path)
    if root:
        return root
    if not lib.in_workspace(path):
        return ""
    if path == cwd or path.startswith(cwd + os.sep):
        return cwd
    return os.path.dirname(path)


def _tool_operations(tool, options, cwd):
    if tool in SHELL_TOOLS:
        edits, checks = shell_operations(command_from(options), cwd)
        edits = [(pos, path or cwd) for pos, path in edits
                 if not path or is_verification_relevant_path(path)]
    else:
        paths = patch_paths(patch_input(options)) if tool in PATCH_EDIT_TOOLS else [
            edit_detect.target_path(options)] if tool in PATH_EDIT_TOOLS else []
        paths = [os.path.realpath(os.path.join(cwd, path)) for path in paths if path]
        edits = [((0,), path) for path in paths if is_verification_relevant_path(path)]
        checks = []
    return ({scope: pos for pos, path in edits if (scope := _scope(path, cwd))},
            {scope: pos for pos, path in checks if (scope := _scope(path, cwd))})


def session_key(data):
    return (data.get("session_id") or data.get("thread_id")
            or data.get("conversation_id") or "")


def _finish(roots, launch, status, started=None):
    if status != 0:
        return
    for root, generation in launch["checks"].items():
        state = roots.get(root, {})
        if "pre_edit_times" in launch and (started is None or started < launch["pre_edit_times"][root]):
            continue
        if state.get("edit") == generation:
            state["verified"] = generation


def _edit_generations(roots, edits):
    for root in edits:
        state = roots.setdefault(root, {})
        state["edit"] = state.get("edit", 0) + 1
        state["edited_at"] = time.time()


def _save_begin(pending, key, roots, checks, tool_input):
    pending[key] = {
        "checks": {root: roots.get(root, {}).get("edit", 0) for root in checks},
        "at": time.time(),
    }
    if tool_input.get("run_in_background"):
        pending[key]["background"] = True


def _command_id(data, options, cwd):
    if data.get("tool_use_id"):
        return "call:" + data["tool_use_id"]
    command = command_from(options)
    if command:
        return "command:" + hashlib.sha256((cwd + "\0" + command).encode()).hexdigest()
    return ""


def _post_tool(data, options, cwd, edits, checks, roots, pending, finished):
    tool = data["tool_name"]
    key = _command_id(data, options, cwd)
    if tool in ("write_stdin", "TaskOutput"):
        process = options.get("session_id") or options.get("task_id")
        key = next((k for k, launch in pending.items()
                    if launch.get("process") == str(process)), "")
    if key in finished:
        return
    launch = pending.get(key)
    response = data.get("tool_response")
    transcript = data.get("transcript_path") or ""
    codex_bash = tool == "Bash" and (data.get("turn_id") or context_watch.is_codex_transcript(transcript))
    reconcile = key.startswith("call:") and transcript and codex_bash
    # Canonical Codex Bash strings are stdout, even when they look like a status envelope.
    if (codex_bash and not isinstance(response, dict)) or tool not in SHELL_TOOLS + ("write_stdin",):
        status, process = None, None
    else:
        status, process = response_status(response)
    if tool == "TaskOutput" and isinstance(response, dict):
        task = response.get("task")
        if response.get("retrieval_status") == "success" and isinstance(task, dict):
            status, _ = response_status(task)
    if not launch and checks and (status is not None or process or reconcile):
        # Older synchronous hooks have no PreToolUse. Duration, when reported, bounds overlap.
        duration = data.get("duration_ms")
        started = time.time() - duration / 1000 if isinstance(duration, (int, float)) else None
        _save_begin(pending, key, roots, {
            root: pos for root, pos in checks.items()
            if started is None or roots.get(root, {}).get("edited_at", 0) <= started
        }, options)
        launch = pending[key]
        if reconcile and not process:
            launch["pre_edit_times"] = {root: roots.get(root, {}).get("edited_at", 0)
                                        for root in launch["checks"]}
    if not launch or not launch.get("edits_recorded"):
        generations = {root: roots.get(root, {}).get("edit", 0) for root in edits}
        _edit_generations(roots, edits)
        if launch:
            launch["edits_recorded"] = True
            for root, edit_pos in edits.items():
                if launch["checks"].get(root) == generations[root] and checks.get(root, ()) > edit_pos:
                    launch["checks"][root] = roots[root]["edit"]
    if not launch:
        return
    if process:
        launch["process"] = process
        return
    if launch.get("background") and not launch.get("process") and status == 0:
        status = None
    if (status is None or (status == 0 and "pre_edit_times" in launch)) \
            and key.startswith("call:") and data.get("transcript_path"):
        return  # Codex can deliver only stdout here. Stop can reconcile the matching rollout result.
    _finish(roots, launch, status)
    pending.pop(key, None)
    if key.startswith("call:"):
        finished[key] = time.time()


def _rollout_results(path, pending):
    """Resolve terminal Codex results from the tail, including writes to an original PTY."""
    keys = {key[5:]: key for key in pending if key.startswith("call:")}
    if not path or not keys:
        return
    sessions = {}
    polls = {}
    started = {}
    try:
        with open(path, "rb") as f:
            # shortcut: ignore results beyond the recent tail, widen the window if real sessions miss checks.
            offset = max(0, os.fstat(f.fileno()).st_size - 2 * 1024 * 1024)
            f.seek(offset)
            if offset:
                f.readline()
            for line in f:
                try:
                    row = json.loads(line)
                    payload = row.get("payload", {})
                    kind = payload.get("type")
                    call = payload.get("call_id")
                    if kind == "function_call" and call in keys and payload.get("name") in SHELL_TOOLS:
                        timestamp = row.get("timestamp")
                        if isinstance(timestamp, str):
                            started[keys[call]] = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                    elif kind == "function_call" and payload.get("name") == "write_stdin":
                        options = json.loads(payload.get("arguments", "{}"))
                        original = sessions.get(str(options.get("session_id")))
                        if original:
                            polls[call] = original
                    elif kind in ("function_call_output", "exec_command_end"):
                        key = keys.get(call) or polls.get(call)
                        if not key:
                            continue
                        response = payload
                        if kind == "function_call_output":
                            response = _response_text(payload.get("output"))
                            if not isinstance(response, str) or not ROLLOUT_HEADER_RE.match(response):
                                continue
                        status, process = response_status(response)
                        if process:
                            sessions[process] = key
                        if status is not None:
                            yield key, status, started.get(key)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        return


def _stop(data, roots, pending, finished):
    for key, status, started in _rollout_results(data.get("transcript_path"), pending):
        launch = pending.pop(key, None)
        if launch:
            _finish(roots, launch, status, started)
            finished[key] = time.time()
    blocked = []
    for root, state in roots.items():
        if state["edit"] > max(state.get("verified", 0), state.get("prompted", 0)):
            blocked.append(root)
            state["prompted"] = state["edit"]
    if blocked:
        return {"decision": "block", "reason": block_reason(*blocked)}


def handle(data):
    """Record a completed check for the worktree and generation it actually verified."""
    session_id = session_key(data)
    event = data.get("hook_event_name", "")
    cwd = data.get("cwd") or ""
    if not session_id or not cwd:
        return None
    options = data.get("tool_input") or {}
    if not isinstance(options, dict):
        options = {"input": options}
    cwd = tool_cwd(options, cwd)
    tool = data.get("tool_name", "")
    edits, checks = _tool_operations(tool, options, cwd)
    if event == "Stop":
        if not lib.in_workspace(cwd) or data.get("stop_hook_active") or not os.path.exists(_state_path(session_id)):
            return None
    elif event == "PreToolUse":
        if not checks or not data.get("tool_use_id"):
            return None
    elif event != "PostToolUse" or not (edits or checks or tool in ("write_stdin", "TaskOutput")):
        return None

    os.makedirs(VERIFY_DIR, exist_ok=True)
    with open(os.path.join(VERIFY_DIR, ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(session_id)
        roots, pending, finished = state["roots"], state["pending"], state["finished"]
        if state.get("last_edit"):
            # Legacy success was session-wide and cannot be attributed to a worktree.
            root = state.get("last_edit_root") or _scope(cwd, cwd)
            if root and root not in roots:
                roots[root] = {"edit": 1, "edited_at": state["last_edit"],
                               "prompted": int(state.get("last_prompted_edit", 0) >= state["last_edit"])}
        for key in ("last_edit", "last_verify", "last_edit_root", "last_prompted_edit"):
            state.pop(key, None)
        if event == "PreToolUse":
            _save_begin(pending, _command_id(data, options, cwd), roots, checks, options)
        elif event == "PostToolUse":
            _post_tool(data, options, cwd, edits, checks, roots, pending, finished)
        else:
            decision = _stop(data, roots, pending, finished)
            save_state(session_id, state)
            return decision
        cutoff = time.time() - lib.STATE_TTL_SECONDS
        state["pending"] = {key: value for key, value in pending.items() if value["at"] >= cutoff}
        state["finished"] = {key: value for key, value in finished.items() if value >= cutoff}
        save_state(session_id, state)
        for name in os.listdir(VERIFY_DIR):
            if name.endswith(".json"):
                path = os.path.join(VERIFY_DIR, name)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
    return None


def main():
    data = lib.read_hook_input()
    decision = handle(data)
    if decision:
        lib.event("verify-block", session_key(data))
        print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
