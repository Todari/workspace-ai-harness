#!/usr/bin/env python3
"""트랜스크립트(Claude jsonl·Codex rollout)에서 인계 노트 초안을 기계적으로 뽑는다.

PreCompact 훅이 모델 개입 없이 쓰는 안전망이다. 사용자 요청·편집 파일·검증 명령·마지막 응답만 모으며
추측을 넣지 않는다.
"""
import json
import os
import re
import subprocess

import edit_detect
import verify_gate

MAX_PROMPT_CHARS = 240
MAX_PROMPTS = 6
MAX_FILES = 25
MAX_COMMANDS = 8
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
SKIP_PREFIXES = ("<", "# AGENTS.md", "[Request interrupted", "Caveat:", "This session is being continued")


def _clip(text, limit=MAX_PROMPT_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _is_prompt(text):
    text = (text or "").lstrip()
    return bool(text) and not text.startswith(SKIP_PREFIXES)


def _iter_rows(path):
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _command_text(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args
    if not isinstance(args, dict):
        return ""
    value = args.get("command") or args.get("cmd") or args.get("input") or ""
    return " ".join(value) if isinstance(value, list) else str(value)


class Extract:
    def __init__(self, cwd=""):
        self.cwd = cwd
        self.prompts = []
        self.files = []
        self.commands = []
        self.last_reply = ""

    def add_file(self, path):
        if path and path not in self.files:
            self.files.append(path)

    def add_command(self, command):
        command = _clip(command, 160)
        if command and verify_gate.is_verify_command(command) and command not in self.commands:
            self.commands.append(command)

    def add_tool(self, name, args):
        if name in EDIT_TOOLS:
            if isinstance(args, dict):
                for key in ("file_path", "path", "notebook_path"):
                    if args.get(key):
                        self.add_file(str(args[key]))
            for match in PATCH_FILE_RE.findall(_command_text(args) if name == "apply_patch" else ""):
                self.add_file(match.strip())
            return
        command = _command_text(args)
        for match in PATCH_FILE_RE.findall(command):
            self.add_file(match.strip())
        try:
            target = edit_detect.find_write_target(command, self.cwd)
        except Exception:  # 감지 실패는 노트 품질 문제일 뿐이다
            target = None
        if target:
            self.add_file(str(target))
        self.add_command(command)


def from_claude(path, cwd=""):
    ex = Extract(cwd)
    for row in _iter_rows(path):
        kind = row.get("type")
        message = row.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if kind == "user" and not row.get("isMeta"):
            texts = [content] if isinstance(content, str) else [
                c.get("text", "") for c in (content or []) if isinstance(c, dict) and c.get("type") == "text"]
            for text in texts:
                if _is_prompt(text):
                    ex.prompts.append(_clip(text))
        elif kind == "assistant" and isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    ex.add_tool(item.get("name", ""), item.get("input") or {})
                elif item.get("type") == "text" and item.get("text", "").strip():
                    ex.last_reply = _clip(item["text"], 400)
    return ex


def from_codex(path, cwd=""):
    ex = Extract(cwd)
    for row in _iter_rows(path):
        if row.get("type") != "response_item":
            continue
        p = row.get("payload") or {}
        kind = p.get("type")
        if kind == "message" and p.get("role") == "user":
            for c in p.get("content") or []:
                text = c.get("text", "") if isinstance(c, dict) else ""
                if _is_prompt(text):
                    ex.prompts.append(_clip(text))
        elif kind == "message" and p.get("role") == "assistant":
            for c in p.get("content") or []:
                if isinstance(c, dict) and c.get("text", "").strip():
                    ex.last_reply = _clip(c["text"], 400)
        elif kind in ("function_call", "custom_tool_call"):
            ex.add_tool(p.get("name", ""), p.get("arguments") or p.get("input") or "")
    return ex


def git_status(cwd):
    try:
        out = subprocess.run(["git", "-C", cwd, "status", "--short"], capture_output=True,
                             text=True, timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    return out[:12]


def build_note(transcript_path, cwd, client="claude"):
    ex = from_codex(transcript_path, cwd) if client == "codex" else from_claude(transcript_path, cwd)
    if not ex.prompts and not ex.files:
        return ""
    first, recent = (ex.prompts[0], ex.prompts[-MAX_PROMPTS:]) if ex.prompts else ("", [])
    lines = ["<!-- 자동 생성: 압축 직전 트랜스크립트에서 기계적으로 추출. 결정·이유는 담기지 않는다. -->",
             "## 목표", "- 첫 요청: %s" % (first or "(없음)")]
    if len(recent) > 1:
        lines.append("- 최근 요청:")
        lines += ["  - %s" % p for p in recent[:-1]]
    lines += ["## 결정", "- (자동 추출 불가. 압축 요약·메모리 참조)"]
    lines += ["## 진행", "- 편집한 파일 %d개:" % len(ex.files)] + ["  - %s" % f for f in ex.files[-MAX_FILES:]]
    if ex.last_reply:
        lines.append("- 마지막 응답 요지: %s" % ex.last_reply)
    lines += ["## 검증 상태", "- 실행한 검증 명령(결과는 확인 필요):"] + \
             (["  - `%s`" % c for c in ex.commands[-MAX_COMMANDS:]] or ["  - (기록 없음)"])
    lines += ["## 다음 할 일", "- 마지막 요청을 기준으로 이어간다: %s" % (recent[-1] if recent else "(없음)")]
    status = git_status(cwd)
    lines += ["## 주의", "- git 미커밋 변경 %d개%s" % (len(status), ":" if status else "")] + \
             ["  - %s" % s for s in status]
    return "\n".join(lines) + "\n"
