# Workspace AI Harness

Claude Code와 Codex가 같은 작업 공간에서 공통 프로젝트 컨텍스트, Git 상태와 검증 규칙을
공유하도록 연결하는 로컬 훅 모음입니다. Python 표준 라이브러리만 사용하며, 훅 오류가 개발
세션을 막지 않도록 fail-open으로 동작합니다.

## 제공 기능

- 세션 시작 시 현재 Git 루트·브랜치·dirty 상태와 스택·스크립트를 요약한 repo map 주입
- `AGENTS.md`와 `CLAUDE.md`에 같은 workspace 규칙을 관리 블록으로 동기화
- 소스 편집 후 타입체크·테스트·빌드 없이 종료하려 할 때 한 번 알림
- 선택적인 Obsidian 프로젝트 노트 검색·다음 할 일 브리지
- Claude Code와 Codex 설정에 소유한 훅만 멱등적으로 등록
- 로그·캐시 크기 제한과 모든 훅의 fail-open 처리

## 요구 사항

- Python 3.10 이상
- Claude Code 또는 Codex
- Git
- 선택 사항: Obsidian 볼트와 iCloud Drive 전체 디스크 접근 권한

## 설치

```bash
git clone https://github.com/Todari/workspace-ai-harness.git \
  ~/.claude/hooks/workspace-harness
cd ~/.claude/hooks/workspace-harness
```

기본 작업 공간은 `~/workspace`입니다. 다른 위치를 사용하면 셸 프로필에 환경변수를 설정합니다.

```bash
export WORKSPACE_HARNESS_ROOT="$HOME/dev"
export OBSIDIAN_VAULT_PATH="$HOME/Documents/notes"
```

개인 workspace 규칙은 추적되지 않는 `context/local/workspace.md`에 둡니다.

```bash
mkdir -p context/local
cp context/workspace.example.md context/local/workspace.md
$EDITOR context/local/workspace.md

python3 sync_context.py
python3 register_hooks.py
python3 register_codex.py
```

Codex는 등록된 훅 명령의 변경을 감지하면 다시 신뢰 승인을 요구할 수 있습니다. 등록 후 Hooks
화면에서 경로와 변경 내용을 확인하세요.

## 설정

| 환경변수 | 기본값 | 역할 |
| --- | --- | --- |
| `WORKSPACE_HARNESS_ROOT` | `~/workspace` | 훅을 적용할 작업 공간 |
| `WORKSPACE_HARNESS_CONTEXT_DIR` | `context/local` | 레포별 심층 컨텍스트 디렉터리 |
| `WORKSPACE_HARNESS_CONTEXT_FILE` | `context/local/workspace.md` 또는 예시 | 공통 지침 원본 |
| `WORKSPACE_HARNESS_CACHE_DIR` | `~/.claude/cache` | repo map·이벤트·마커 캐시 |
| `OBSIDIAN_VAULT_PATH` | macOS Obsidian iCloud 기본 경로 | 선택적 볼트 루트 |

레포별 심층 컨텍스트는 `<레포 디렉터리명>.md`로 저장합니다.

```text
context/local/
  workspace.md
  example-web.md
```

개인 경로, 비공개 프로젝트 구조와 운영 규칙은 `context/local/`에만 두고 커밋하지 않습니다.

## 구조

- `harness_lib.py`: 경로 설정, 로그·이벤트와 fail-open 공용 기능
- `repo_map.py`: 저장소 구조·스크립트·Git 상태 요약
- `sync_context.py`: 공통 규칙을 workspace 문서에 동기화
- `verify_gate.py`: 편집 이후 관련 검증 실행 여부 확인
- `register_hooks.py`: Claude Code 훅 등록
- `register_codex.py`: Codex 훅 등록
- `vault_search.py`: Unicode 정규화를 포함한 Obsidian 검색
- `obsidian_bridge.py`: 프로젝트 노트의 다음 할 일 주입

## 보안 경계

- 훅은 보안 샌드박스가 아니라 개발 흐름을 돕는 best-effort 자동화입니다.
- 개인 컨텍스트와 `ops.env`는 Git에서 제외됩니다.
- 볼트 쓰기는 메인 세션 하나로 제한하는 것을 권장합니다.
- 설치 스크립트를 실행하기 전에 변경되는 Claude Code·Codex 설정을 직접 검토하세요.

보안 문제는 공개 이슈 대신 [보안 정책](SECURITY.md)에 따라 제보해 주세요.

## 검증

```bash
python3 -m unittest discover -v
python3 -m compileall -q .
```

## 라이선스

[MIT](LICENSE)
