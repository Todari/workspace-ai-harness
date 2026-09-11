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
- 규칙 기반 라우터가 LLM 호출 없이 요청을 분류한다. 현재 개인 설정은 advisory로, 차단 없이 위임을 권고한다.
  hard 모드는 선택할 수 있지만 오분류·재시도 비용을 먼저 측정해야 한다.
  전용 `codex exec` 세션이 조사·구현·검증하고 실행별 모델·effort·thread·Git·토큰을 기록한다.

## 요구 사항

- Python 3.8 이상
- Claude Code 2.1.251 이상(modelSettings·현재 훅 응답 형식) 또는 Codex 0.151 이상
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
| `WORKSPACE_HARNESS_CODEX_RUNS_DIR` | `<cache>/codex-worker` | Codex worker 실행 이력(최근 200건) |
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
- `claude_auto_route.py`: Claude에만 자동 구현 라우팅 정책 주입(Codex 재위임 방지)
- `claude_handoff_gate.py`: 요청 분류와 위임 전후 도구·턴 상한을 강제하는 상태 머신
- `repo_map.py`: 저장소 구조·스크립트·Git 상태 요약 (lean 모드·그룹 인덱스 포함)
- `repos.py`: 레포 레지스트리 조회·활동 추적·규칙 표 생성
- `sync_context.py`: 공통 규칙을 지침 문서 세 곳에 동기화
- `verify_gate.py` + `edit_detect.py`: 편집 이후 관련 검증 실행 여부 확인
- `register_hooks.py` / `register_codex.py`: Claude Code / Codex 훅 등록
- `register_orchestration.py`: Claude의 Fable architect와 강제 위임용 `/delegate-codex` 명령 동기화
- `codex_worker.py`: 구조화된 Codex 구현 실행·resume·rerun·토큰 이력
- `orchestration/models.json`: 활성 모델·effort와 업그레이드 이력의 정본
- `doctor.py`: 등록·동기화·리비전·레지스트리 드리프트 점검
- `stats.py`: Claude·Codex 토큰, 라우팅 이벤트와 검증 이행률
- `vault_search.py` / `obsidian_bridge.py` / `vault_subagent.py`: Obsidian 연동

## 보안 경계

- 훅은 보안 샌드박스가 아니라 개발 흐름을 돕는 best-effort 자동화입니다.
- 개인 컨텍스트(`context/local/`)와 `ops.env`는 Git에서 제외됩니다.
- 볼트 쓰기는 메인 세션 하나로 제한하는 것을 권장합니다.
- 설치 스크립트를 실행하기 전에 변경되는 Claude Code·Codex 설정을 직접 검토하세요.

보안 문제는 공개 이슈 대신 [보안 정책](SECURITY.md)에 따라 제보해 주세요.

## Fable → Codex 개발 위임

새 Claude Code 세션에서는 평소처럼 자연어로 요청하면 됩니다. 대화·제품 판단은 Claude가 직접,
레포 조사·진단은 Codex `inspect`, 구현은 Codex worker가 담당합니다. 불명확한 고위험 설계만
`fable-architect`를 한 번 사용합니다. 기본은 Fable `medium` → Codex `high`이며, 실제로 어려운
구현에만 Codex `xhigh`를 사용합니다. `register_orchestration.py`는 선언만 바꾸는 것이 아니라
Claude의 실제 Fable 기본 effort도 `medium`으로 동기화합니다.

`직접 해줘`, `Claude가 직접 해줘`, `Codex 쓰지 마`, `위임하지 마`라고 하면 Claude가 직접
처리합니다. 반대로 `/delegate-codex <작업>`은 자동 분류와 무관하게 Codex 위임을 강제합니다.
자동 정책은 Claude 훅에만 등록되므로 Codex worker가 다시 자신을 호출하지 않습니다.
아래 강제 차단 동작은 `routing.enforcement=hard`를 선택한 경우에만 적용됩니다.
현재 `advisory`에서는 직접 수행이 허용되며, 강제 차단하지 않습니다.
하드 모드로 위임된 턴에서는 Claude의 레포 탐색·편집·background 실행(`&`와 `run_in_background` 파라미터 모두)·
로그 redirect·주기적 폴링이 차단됩니다. worker는 `run --detach`로 세션과 분리해 띄우고 run_id를
즉시 받은 뒤, `wait <run_id>`를 foreground에서 호출해 기다립니다. Codex run은 중앙값 10분,
p90 30분 가까이 걸려 Claude Bash 도구의 600초 상한을 넘기 때문에 blocking 1회 호출로는 결과가
유실됐고, 이 구조가 그 문제를 없앱니다. `wait`가 `running`을 돌려주면 같은 명령을 다시 호출하며
(최대 8회), 최종 JSON이 오면 추가 도구 없이 요약합니다. 여러 레포 요청은 최대 4개 worker를 한
번의 병렬 도구 배치로 시작하고 `wait`에 run ID를 모두 넘깁니다. 전체 Codex worker 동시 실행 수는
4개, 같은 repo의 writer는 1개로 제한되며 잠금은 즉시 실패하지 않고 대기열 상한까지 기다립니다.

```bash
python3 codex_worker.py run --repo <git-root> --manifest - --detach <<'JSON' ... JSON
python3 codex_worker.py wait <run-id> --timeout 540   # running이면 다시 호출
python3 codex_worker.py list --limit 10       # 최근 run·모델·토큰
python3 codex_worker.py resume <run-id> --instruction "후속 수정"
python3 codex_worker.py rerun <run-id> --model <candidate> --effort high
```

동일 worktree에는 worker 하나만 실행합니다. 상세 운영·재현·모델 승격 절차는
[`docs/codex-orchestration.md`](docs/codex-orchestration.md)를 참고하세요.

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
