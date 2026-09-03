#!/usr/bin/env python3
"""편집 감지 헬퍼 — 어떤 경로가 소스인지, 셸 명령이 workspace 파일을 쓰는지 판정한다.

verify_gate가 "소스가 바뀌었는가"를 판단할 때 쓴다. 완벽한 파서가 아니라 흔한 패턴
(리다이렉션·sed -i·tee·patch)을 잡는 것이 목적이며, 판단 불가는 "편집 아님"으로 둔다.
"""
import os
import re
import shlex

import harness_lib as lib

# 문서·메타 경로는 소스 편집으로 세지 않는다.
META_SEGMENTS = ("/docs/", "/.claude/")
META_PREFIXES = (
    "/private/tmp/claude-",
    "/tmp/claude-",
    os.path.expanduser("~/.claude") + os.sep,
)


def target_path(tool_input):
    tool_input = tool_input or {}
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def is_meta_path(path):
    """지침 파일·docs·.claude·스크래치 경로면 True."""
    if not path:
        return False
    real = os.path.realpath(path)
    if os.path.basename(real) in ("CLAUDE.md", "AGENTS.md"):
        return True
    if any(real.startswith(prefix) for prefix in META_PREFIXES):
        return True
    return any(segment in real for segment in META_SEGMENTS)


def is_workspace_source(path):
    if path is None:
        return False
    in_ws = path == lib.WORKSPACE_ROOT or path.startswith(lib.WORKSPACE_ROOT + os.sep)
    return in_ws and not is_meta_path(path)


def resolve(target, cwd):
    target = target.strip("'\"")
    if not target or target.startswith("$"):
        return None  # 변수 확장은 판단 불가
    if target.startswith("~"):
        expanded = os.path.expanduser(target)
        if expanded == target:
            return None
        target = expanded
    path = target if os.path.isabs(target) else os.path.join(cwd or "", target)
    return os.path.realpath(path)


# heredoc 시작(`<<EOF`, `<<-'EOF'`)만 잡고 herestring(`<<<`)은 제외한다.
HEREDOC_RE = re.compile(
    r"(?<!<)<<-?\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))(?!<)")


def heredoc_delimiter(line):
    """이 줄이 heredoc을 여는가. 열면 종료 구분자를, 아니면 None을 준다."""
    match = HEREDOC_RE.search(line)
    if not match:
        return None
    return match.group(1) or match.group(2) or match.group(3)


def strip_heredoc_bodies(command):
    """heredoc 본문과 종료 구분자를 제거한다 (여는 줄은 남긴다).

    본문은 셸 구문이 아니라 하위 프로세스 입력이다. 토큰화하면 커밋 메시지의
    `Co-Authored-By: ... <a@b>` 같은 텍스트가 리다이렉션으로 오인된다.
    """
    lines = command.splitlines()
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        delimiter = heredoc_delimiter(line)
        if delimiter is not None:
            i += 1
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
        i += 1
    return "\n".join(kept)


def tokens(command):
    if "<<" in command:
        command = strip_heredoc_bodies(command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []  # 불완전한 quoting은 판단하지 않는다


SEPARATORS = ("|", "||", "&&", ";")


def command_segments(toks):
    segments, current = [], []
    for tok in toks:
        if tok in SEPARATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def is_git_apply(segment):
    if not segment or os.path.basename(segment[0]) != "git":
        return False
    i = 1
    while i < len(segment):
        token = segment[i]
        if token in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return token == "apply"
    return False


def is_in_place_option(token):
    if token.startswith("--"):
        return token == "--in-place" or token.startswith("--in-place=")
    return token.startswith("-") and "i" in token[1:].split("=", 1)[0]


def find_write_target(command, cwd):
    """workspace 소스를 쓰는 셸 명령이면 설명 문자열, 아니면 None."""
    toks = tokens(command)
    for i, tok in enumerate(toks[:-1]):
        if tok in (">", ">>"):
            real = resolve(toks[i + 1], cwd)
            if is_workspace_source(real):
                return "리다이렉션 → " + real
    segments = command_segments(toks)
    if any(is_git_apply(segment) or
           (segment and os.path.basename(segment[0]) == "patch")
           for segment in segments):
        return "patch/git apply"
    for i, tok in enumerate(toks):
        if os.path.basename(tok) not in ("sed", "perl"):
            continue
        segment = []
        for t in toks[i + 1:]:
            if t in SEPARATORS:
                break
            segment.append(t)
        if not any(is_in_place_option(t) for t in segment):
            continue
        for target in segment:
            if target.startswith("-"):
                continue
            real = resolve(target, cwd)
            if real and os.path.isfile(real) and is_workspace_source(real):
                return "in-place 편집 → " + real
    for i, tok in enumerate(toks):
        if tok == "tee":
            for t in toks[i + 1:]:
                if t.startswith("-"):
                    continue
                if t in SEPARATORS:
                    break
                real = resolve(t, cwd)
                if is_workspace_source(real):
                    return "tee → " + real
    return None
