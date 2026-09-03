# Workspace AI Harness

Claude Code와 Codex가 같은 작업 공간에서 공통 프로젝트 컨텍스트, Git 상태와 검증 규칙을
공유하도록 연결하는 로컬 훅 모음입니다. Python 표준 라이브러리만 사용하며, 훅 오류가 개발
세션을 막지 않도록 fail-open으로 동작합니다.

## 제공 기능

- 세션 시작 시 현재 Git 루트·브랜치·dirty 상태와 스택·스크립트를 요약한 repo map 주입.
  레포에 `AGENTS.md`/`CLAUDE.md`가 있으면 구조는 그 문서에 맡기고 짧은 lean 모드로 주입한다.
  git 레포가 아닌 그룹 디렉토리에서는 하위 레포의 브랜치·변경 요약을 대신 준다.
- 공통 규칙을 workspace `AGENTS.md`·`CLAUDE.md`와 Codex 전역 `~/.codex/AGENTS.md`에 관리 블록으로 동기화.
  Codex는 git 루트 위의 AGENTS.md를 읽지 않으므로 전역 파일이 레포 세션에 규칙을 전달한다.
- 레포 레지스트리(`context/local/repos.json`) 하나로 규칙 표·볼트 노트 매핑·활동 추적을 생성.
- 소스 편집 후 타입체크·테스트·빌드 없이 종료하려 할 때 편집 세대당 한 번 알림.
- 선택적인 Obsidian 프로젝트 노트 검색·다음 할 일 브리지, 서브에이전트용 읽기 전용 안내.
- 훅 목록(`hooks_manifest.py`) 하나에서 Claude Code와 Codex 설정에 멱등 등록, `doctor.py`로 드리프트 점검.
- 로그·캐시 크기 제한과 모든 훅의 fail-open 처리, `stats.py`로 주입량·검증 이행률 관측.

## 요구 사항

- Python 3.8 이상
- Claude Code 또는 Codex(0.151 이상: SubagentStart 훅 사용)
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

개인 workspace 규칙과 레포 레지스트리는 추적되지 않는 `context/local/`에 둡니다.

```bash
mkdir -p context/local
cp context/workspace.example.md context/local/workspace.md
$EDITOR context/local/workspace.md      # 규칙. 레포 표 자리에는 <!-- workspace-harness: repo-table -->
$EDITOR context/local/repos.json        # {"repos": [{"name", "path", "rule", "vault_note", "aliases"}]}

python3 sync_context.py
python3 register_hooks.py
python3 register_codex.py
python3 doctor.py
```

Codex는 등록된 훅 명령의 변경을 감지하면 다시 신뢰 승인을 요구합니다. 하네스 코드를 수정한 뒤에는
`register_codex.py`를 다시 실행하고 Hooks 화면에서 승인하세요. `doctor.py`가 리비전 불일치를 알려 줍니다.

## 설정

| 환경변수 | 기본값 | 역할 |
| --- | --- | --- |
| `WORKSPACE_HARNESS_ROOT` | `~/workspace` | 훅을 적용할 작업 공간 |
| `WORKSPACE_HARNESS_CONTEXT_DIR` | `context/local` | 레포별 심층 컨텍스트 디렉터리 |
| `WORKSPACE_HARNESS_CONTEXT_FILE` | `context/local/workspace.md` 또는 예시 | 공통 지침 원본 |
| `WORKSPACE_HARNESS_REPOS` | `context/local/repos.json` | 레포 레지스트리 |
| `WORKSPACE_HARNESS_CACHE_DIR` | `~/.claude/cache` | repo map·이벤트·검증 상태 캐시 |
| `OBSIDIAN_VAULT_PATH` | macOS Obsidian iCloud 기본 경로 | 선택적 볼트 루트 |
| `CODEX_HOME` | `~/.codex` | Codex 설정·전역 AGENTS.md 위치 |

레포별 심층 컨텍스트는 `<레포 디렉터리명>.md`로 저장합니다. 안전·검증 정보만 요약 주입되고,
문서 이후 커밋이 30개 이상 쌓이면 노후화 경고가 붙습니다.

```text
context/local/
  workspace.md
  repos.json
  example-web.md
```

## 구조

- `harness_lib.py`: 경로 설정, 로그·이벤트와 fail-open 공용 기능
- `hooks_manifest.py`: 등록할 훅의 단일 목록
- `repo_map.py`: 저장소 구조·스크립트·Git 상태 요약 (lean 모드·그룹 인덱스 포함)
- `repos.py`: 레포 레지스트리 조회·활동 추적·규칙 표 생성
- `sync_context.py`: 공통 규칙을 지침 문서 세 곳에 동기화
- `verify_gate.py` + `edit_detect.py`: 편집 이후 관련 검증 실행 여부 확인
- `register_hooks.py` / `register_codex.py`: Claude Code / Codex 훅 등록
- `doctor.py`: 등록·동기화·리비전·레지스트리 드리프트 점검
- `stats.py`: 이벤트 통계와 검증 이행률
- `vault_search.py` / `obsidian_bridge.py` / `vault_subagent.py`: Obsidian 연동

## 보안 경계

- 훅은 보안 샌드박스가 아니라 개발 흐름을 돕는 best-effort 자동화입니다.
- 개인 컨텍스트(`context/local/`)와 `ops.env`는 Git에서 제외됩니다.
- 볼트 쓰기는 메인 세션 하나로 제한하는 것을 권장합니다.
- 설치 스크립트를 실행하기 전에 변경되는 Claude Code·Codex 설정을 직접 검토하세요.

보안 문제는 공개 이슈 대신 [보안 정책](SECURITY.md)에 따라 제보해 주세요.

## 검증

```bash
for t in test_*.py; do python3 -m unittest -q "${t%.py}"; done   # 파일별 실행 (볼트 테스트가 느릴 수 있음)
python3 -m compileall -q .
python3 doctor.py
```

## 라이선스

[MIT](LICENSE)
