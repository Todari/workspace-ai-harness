# Workspace AI Harness

Claude Code와 Codex가 같은 작업 공간에서 공통 프로젝트 컨텍스트, Git 상태와 검증 규칙을
공유하도록 연결하는 로컬 훅 모음입니다. Python 표준 라이브러리만 사용하며, 훅 오류가 개발
세션을 막지 않도록 fail-open으로 동작합니다.

## 제공 기능

- 세션 시작 시 현재 Git 루트·브랜치·dirty 상태와 스택·스크립트를 요약한 repo map 주입.
  레포에 `AGENTS.md`/`CLAUDE.md`가 있으면 구조는 그 문서에 맡기고 짧은 lean 모드로 주입한다.
  git 레포가 아닌 그룹 디렉토리에서는 하위 레포의 브랜치·변경 요약을 대신 준다.
- 공통 규칙은 workspace `CLAUDE.md`와 Codex 전역 `~/.codex/AGENTS.md`에 동기화한다.
  Codex 전역 지침이 있는 환경에서 workspace `AGENTS.md`는 짧은 진입 안내로 만들어 중복 주입을 줄인다.
- 레포 레지스트리(`context/local/repos.json`) 하나로 규칙 표·볼트 노트 매핑·활동 추적을 생성.
- 소스 편집 후 타입체크·테스트·빌드 없이 종료하려 할 때 편집 세대당 한 번 알림.
- 선택적인 Obsidian 프로젝트 노트 검색·다음 할 일 브리지, 서브에이전트용 읽기 전용 안내.
- 훅 목록(`hooks_manifest.py`) 하나에서 Claude Code와 Codex 설정에 멱등 등록, `doctor.py`로 드리프트 점검.
- 로그·캐시 크기 제한과 모든 훅의 fail-open 처리. `stats.py`는 기본으로 가벼운 작업 기록만 집계하고
  Claude 전체 사용량은 `--include-claude`로 선택한다. JSON 집계도 지원한다.
- `ops_report.py`로 자동화의 실제 종료 코드·마지막 성공·미실행을 관측하고, LLM 없이 로컬 리포트를 만든다.
  Slack·Discord는 명시적인 `--send`에서만 전송하며 일일 중복 전송과 불확실한 응답 재전송을 막는다.
- Discord 캡처 페이지네이션·처리 원장·부분 ack 실패 보고, 레지스트리 기반 볼트 동기화와 전송 전 Git 최신성 검사.

## 요구 사항

### 실행 환경

- Python 3.8 이상
- Claude Code 2.1.251 이상(현재 훅 응답 형식) 또는 Codex 0.151 이상
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
python3 register_orchestration.py
python3 doctor.py
```

Codex는 등록된 훅 명령의 변경을 감지하면 다시 신뢰 승인을 요구합니다.
리비전은 진입 스크립트와 전이적인 로컬 Python import를 함께 해시합니다. 하네스 코드를 수정한 뒤에는
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
- `verify_gate.py` + `edit_detect.py`: 편집 이후 관련 검증 실행 여부 확인. 차단 사유에 편집한 레포의 검증 명령 후보(package.json 스크립트·pytest/unittest·Makefile, `repos.json`의 `verify`가 우선)를 붙임
- `context_watch.py`(Claude 전용): 직전 요청 문맥이 25만/50만 토큰을 넘으면 `/compact`·새 세션 권고, 6천 자 이상 도구 출력(Read 제외)에 자르기 안내
- `session_handoff.py`: 세션 인계. Stop에서 문맥 50만 토큰 이상이면 한 번 차단해 인계 노트(`write`)를 쓰게 하고, 같은 경로의 다음 Claude·Codex 세션 SessionStart에 주입(72시간, 세션당 1회). `/handoff`로 수동 저장, `done`으로 폐기. Codex는 rollout의 token_count로 판단(직전 턴 18만 토큰↑ 또는 누적 입력 4천만↑)
- `handoff_extract.py`: PreCompact(Claude·Codex)에서 트랜스크립트로 자동 인계 노트(요청·편집 파일·검증 명령·git 상태)를 기계적으로 생성. 같은 세션의 수동 노트가 있으면 덮지 않음
- `register_hooks.py` / `register_codex.py`: Claude Code / Codex 훅 등록
- `register_orchestration.py`: Claude 명령 자산(`/handoff`)을 workspace `.claude/`에 동기화
- `doctor.py`: 등록·동기화·리비전·레지스트리 드리프트 점검
- `stats.py`: 훅 이벤트, 검증 이행률, 선택적 Claude·Codex 세션 토큰
- `vault_search.py` / `obsidian_bridge.py` / `vault_subagent.py`: Obsidian 연동

## 보안 경계

- 훅은 보안 샌드박스가 아니라 개발 흐름을 돕는 best-effort 자동화입니다.
- 개인 컨텍스트(`context/local/`)와 `ops.env`는 Git에서 제외됩니다.
- 볼트 쓰기는 메인 세션 하나로 제한하는 것을 권장합니다.
- 설치 스크립트를 실행하기 전에 변경되는 Claude Code·Codex 설정을 직접 검토하세요.

보안 문제는 공개 이슈 대신 [보안 정책](SECURITY.md)에 따라 제보해 주세요.

## 운영과 실측 개선

운영 리포트, 실패 복구, Slack·Discord 연결, 2026-09-08 감사 결과는
[`docs/operations.md`](docs/operations.md)를 참고하세요.

```bash
python3 stats.py 7 --json
python3 stats.py 7 --include-claude  # 상세 사용량을 조사할 때만
python3 ops_report.py report --days 1  # 미리보기, 외부 전송 없음
```

## 검증

```bash
for t in test_*.py; do python3 -m unittest -q "${t%.py}"; done   # 파일별 실행 (볼트 테스트가 느릴 수 있음)
python3 -m compileall -q .
python3 doctor.py
```

## 라이선스

[MIT](LICENSE)
