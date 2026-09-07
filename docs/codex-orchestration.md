# Fable → Codex 오케스트레이션 운영 가이드

## 목적과 경계

Fable는 범위·위험·완료 기준만 짧게 결정하고, 전용 Codex 세션이 코드 탐색·편집·검증을
담당한다. 양쪽이 같은 레포를 다시 읽는 비용을 줄이고 Claude와 Codex 사용량을 분리하는 것이
목적이다.

- 대화·제품 판단: Fable `medium`이 직접 답한다.
- 레포 조사·진단: Codex `inspect`/`medium`이 read-only로 수행한다.
- 일반 구현: Fable가 3,000자 이내 계약만 만들고 Codex `high`가 수행한다.
- 불명확한 고위험 설계: `fable-architect`/`high`를 한 번만 거쳐 Codex에 넘긴다.
- Fable `xhigh`와 Codex `xhigh`는 측정된 품질 이득이 있는 예외에만 사용한다.
- Codex worker는 서브에이전트·커밋·푸시·PR·배포를 수행하지 않는다.
- 동일 worktree에는 writer를 하나만 둔다. repo별 잠금과 전역 4-slot 대기열을 worker 프로세스가
  관리하므로 Claude가 빈자리를 확인하기 위해 폴링하지 않는다.

모델과 effort의 정본은 [`orchestration/models.json`](../orchestration/models.json)이다.
활성 설정, 자동 라우팅 임계값·예외 문구, 변경 이력을 한 파일에서 함께 관리한다.

## 설치와 호출

Claude 자산을 workspace에 등록한다.

```bash
cd ~/.claude/hooks/workspace-harness
python3 register_orchestration.py
python3 doctor.py
```

`register_orchestration.py`는 `~/.claude/settings.json`의 top-level `effortLevel`과 Fable 5.1
`modelSettings`를 `medium`으로 맞춘다. 다른 모델 설정은 보존한다. `--effort` 또는 ultracode로
실행하면 해당 세션에서는 그 명시값이 우선하므로 자동 라우터 세션에 사용하지 않는다.

등록 뒤 새 Claude Code 세션부터는 특수 명령 없이 평소처럼 자연어로 요청한다.

```text
로그인 실패 원인을 고치고 관련 테스트까지 실행해줘
```

`UserPromptSubmit` 훅의 규칙 기반 분류기는 LLM을 호출하지 않는다.

| 요청 | 실행 경로 |
|---|---|
| 일반 대화·제품 판단 | Claude 직접 |
| 코드·레포 원인 분석, 리뷰, 테스트 실행 | Codex read-only inspect |
| 코드·문서·설정 변경 | Codex implement |
| 불명확한 아키텍처 결정 | Fable architect 1회 → Codex |
| 여러 레포 변경 | 한 병렬 배치에서 Codex 최대 4개 |

`직접 해줘`, `Claude가 직접 해줘`, `Codex 쓰지 마`, `위임하지 마`는 직접 수행 예외다.
`/delegate-codex`는 자동 분류와 무관하게 위임을 강제할 때만 사용한다.

```text
/delegate-codex 로그인 실패 원인을 고치고 관련 테스트까지 실행해줘
```

라우팅·게이트 훅은 Codex 설정에 등록되지 않는다. 따라서 worker가 같은 정책을 받아 재귀적으로
새 worker를 띄우지 않는다. 위임된 Claude 턴에는 Git 상태 확인 2회, `run --detach` 1회(batch는
최대 4회), `wait` 최대 8회만 허용한다. 잘못된 도구는 허용 형식 템플릿과 함께 거부하고, 위반이
3회를 넘거나 결과 수신 후 도구를 더 쓰면 턴을 종료한다. `run_in_background` 파라미터도 `&`와
같이 차단한다. 짧은 질문(80자 이하, 질문형)과 파일 하나를 지목한 짧은 요청은 Claude가 직접
처리한다. Codex inspect 한 번이 수만 토큰·수 분이라 한 줄 질문에는 손해이기 때문이다.

### 강제 수준 스위치

`orchestration/models.json`의 `routing.enforcement`가 `hard`(기본)면 위임 턴에서 잘못된 도구를
거부하고 Stop을 한 번 막는다. `advisory`로 바꾸면 라우트 안내만 주입하고 아무것도 거부하지 않는다.
게이트가 작업을 반복해서 막는다고 느끼면 먼저 `advisory`로 낮추고 `stats.py`의 위임 턴 지표로
원인을 본 뒤 되돌린다. 재등록 없이 파일 저장 즉시 적용된다.

run 시작이 run_id 없이 실패하면(계약 검증 오류, 권한 분류기 거부) 결과가 아니라 시작 실패로 보고
슬롯을 돌려준다. `routing.max_launch_failures`(기본 2)까지 같은 턴에서 다시 시작할 수 있다.

### detach → wait 프로토콜

Claude Bash 도구는 한 호출을 최대 600초까지만 기다린다. 실측 Codex run은 중앙값 577초,
p90 1,712초라 blocking 1회 호출은 절반 가까이 결과를 잃었고, Claude가 `run_in_background`로
우회하면서 폴링이 재발했다. 그래서 worker 실행과 결과 회수를 분리한다.

1. `run --detach`는 `<run-id>.pending.json`을 쓰고 자식 프로세스를 새 세션으로 띄운 뒤 run_id를
   즉시 돌려준다. Claude 세션이 끊겨도 실행과 기록은 이어진다.
2. `wait <run-id> --timeout 540`은 기록 파일이 생길 때까지 worker 프로세스 안에서 대기한다.
   상한에 걸리면 `status: "running"`과 같은 `wait_command`를 돌려주고, Claude는 그 명령을
   다시 호출한다. 폴링은 프로세스가 하고 Claude 턴은 늘지 않는다.
3. 잠금 대기 초과 같은 실행 전 실패도 기록으로 남겨 `wait`가 `failed`를 받게 한다.

기본 worker timeout은 1,200초다. 3,600초 run 하나가 같은 repo 잠금을 한 시간 붙들어 뒤 run을
즉시 실패시킨 사례가 있어 낮췄고, repo 잠금은 이제 즉시 실패 대신 대기열 상한까지 기다린다.
이미 열려 있던 Claude 세션에는 새 SessionStart 컨텍스트가 없으므로 등록 후 새 세션을 연다.

참고: [Claude Code hooks 공식 문서](https://code.claude.com/docs/en/hooks)

직접 실행할 때는 작업 계약을 JSON으로 전달한다.

```bash
python3 ~/.claude/hooks/workspace-harness/codex_worker.py run \
  --repo /Users/me/workspace/projects/example \
  --manifest task.json
```

실제 모델을 호출하지 않고 계약·경로·명령만 확인하려면 `--dry-run`을 붙인다. 작업 계약에는
비밀 값, `.env` 내용, 인증 토큰을 넣지 않는다.

작업 계약 필드:

| 필드 | 의미 |
|---|---|
| `task_mode` | `implement` 또는 read-only `inspect` |
| `task_class` | `simple`, `medium`, `complex`, `high_risk`, `batch_or_repo_wide` |
| `objective` | 구현 목표 한 문장 |
| `worker_effort` | inspect는 `medium`, 구현은 `high`, 어려운 구현만 `xhigh` |
| `scope` | 수정 가능한 repo 상대 경로 |
| `acceptance_criteria` | 관찰 가능한 완료 조건 |
| `constraints` | 기존 변경 보존 등 추가 제약 |
| `verification_commands` | worker가 실행할 최소 검증 명령 |

hard route가 `--orchestrator-session`을 붙인 계약은 canonical JSON 기준 3,000자를 넘으면 실행
전에 거부한다. 기존 수동/과거 rerun 호환 경로는 이 상한을 강제하지 않는다. OpenAI의 비대화식 실행
가이드에 따라 `codex exec --json`, `--output-schema`를 사용하고, implement는
`--sandbox workspace-write`, inspect는 `read-only`로 고정한다. CLI의 저장된 로그인을 재사용하며, 자동 실행은
`approval_policy=never`로 고정해 승인을 기다리며 멈추지 않고 필요한 권한이 없으면
`blocked`로 반환하게 한다. 기본 worker는 토큰과 도구 잡음을 줄이기 위해
`--ignore-user-config`로 Codex의 개인 config·플러그인·MCP를 제외한다. 해당 도구가 꼭 필요한
작업만 `--with-user-config`를 붙인다. 저장된 인증은 유지되며, worker prompt는 repo의
`AGENTS.md` 또는 fallback `CLAUDE.md`를 먼저 읽도록 명시한다.

참고: [OpenAI Codex 비대화식 실행 공식 문서](https://developers.openai.com/codex/noninteractive),
[GPT-5.6 모델 가이드](https://developers.openai.com/api/docs/guides/latest-model)

## 재개와 재실행

같은 작업의 실패 원인을 보완할 때는 기존 Codex 문맥을 이어 간다.

```bash
python3 codex_worker.py resume 20260903-120000-abcdef \
  --instruction "실패한 테스트 원인을 수정하고 같은 명령으로 다시 검증해"
```

처음부터 다시 수행하거나 다른 모델·effort를 비교할 때는 새 세션을 만든다.

```bash
python3 codex_worker.py rerun 20260903-120000-abcdef
python3 codex_worker.py rerun 20260903-120000-abcdef \
  --model <candidate-model> --effort high
```

`resume`은 동일 thread ID를 사용하고, `rerun`은 원래 작업 계약을 복사해 새 thread를 만든다.
둘 다 `parent_run_id`를 기록한다. 다만 `rerun`은 Git 상태를 과거로 되돌리지 않는다.
`parent_context.head_matches_original`이 `false`면 코드 상태가 달라진 비교다. 과거 상태를 정확히
재현해야 하면 기록의 `git_before.head`로 별도 worktree를 만든 뒤 저장된 manifest를 새 `run`에
전달한다. 현재 worktree를 reset하지 않는다.

## 실행 이력과 토큰 확인

기록은 기본적으로 `~/.claude/cache/codex-worker/<run-id>.json`에 저장된다. 디렉터리는 `0700`,
기록 파일은 `0600`이며 최근 200건을 보존한다. 원시 JSONL이나 reasoning 본문은 저장하지 않는다.

주요 기록:

- 모델·effort·Codex CLI 버전·prompt 버전
- thread ID·parent run ID·작업 계약과 SHA-256
- 실행 전후 branch·HEAD·dirty 상태
- input·cached input·output·reasoning output 토큰
- queue/실행 시간·task mode·orchestrator session·구조화된 결과·테스트·남은 위험

```bash
python3 codex_worker.py list --limit 10
python3 codex_worker.py show <run-id>
python3 stats.py 30
```

`stats.py`는 Codex cached input을 분리하고 Claude JSONL의 streaming 중복 message id를 제거해
모델/effort별 요청·cache-create/read·output·thinking을 함께 보여 준다. 모델이나 프롬프트 변경
전후의 총 input만 비교하면 캐시 효과를 잘못 해석할 수 있으므로 두 값을 같이 본다.

### 초기 smoke 기준선 — 2026-09-03

빈 임시 Git 저장소에서 파일 변경 없이 `git status --short`만 검증했다. 품질 벤치마크가 아니라
CLI·스키마·이력 파이프라인과 고정 오버헤드를 확인하는 기준선이다.

| run ID | 설정 | input | cached | uncached | output | 시간 |
|---|---|---:|---:|---:|---:|---:|
| `20260903-164338-f92d81` | 사용자 config 포함 | 36,412 | 17,920 | 18,492 | 285 | 13.8초 |
| `20260903-164512-a66555` | `--ignore-user-config` | 33,980 | 16,640 | 17,340 | 284 | 14.6초 |

개인 config 제외로 input은 2,432토큰(약 6.7%) 줄었다. 실제 개발 작업의 절감률은 repo 규칙,
도구 수, 작업 길이에 따라 달라지므로 대표 작업 run을 별도로 비교한다.

### hard handoff 도입 기준선 — 2026-09-07

도입 전 최근 5시간 로그를 message id로 중복 제거했다. Fable 5.1 xhigh가 10개 세션에서 약
478회 호출되어 output 약 766,760(그중 thinking 315,755), Bash 약 550회, 폴링·로그 확인
약 152회를 기록했다. 같은 기간 Codex가 실제로 실행됐음에도 Claude 사용량이 컸으므로,
모델 부재가 아니라 soft instruction과 반복 감독을 원인으로 판단했다.

도입 후 대표 작업에서는 다음을 함께 본다: Fable 요청 2회 이하, 폴링 0회, 계약 3,000자 이하,
Codex 완료 기준 충족, 사람의 재수정 횟수. 품질이 유지된 상태에서 Claude output/thinking이
감소해야 승격 상태를 유지한다. `stats.py`의 "위임 턴" 줄이 run ID 회수율·위반·background
시도·wait 횟수를 상태 파일에서 직접 센다.

### v2 첫날 실측 — 2026-09-07

도입 당일 위임 턴 30건 중 run ID를 회수한 상태 파일은 4건이었다. Codex run 55건은 중앙값
577초, p90 1,712초, 3,600초 timeout 1건이었고 Claude는 Bash 600초 상한을 피하려고
`run_in_background`를 60회 이상 썼다. 게이트는 명령 문자열의 `&`만 검사해 이를 잡지 못했다.
detach/wait 프로토콜, `run_in_background` 차단, repo 잠금 대기, 위반 허용 3회는 이 실측의
결과다. 같은 날 Fable 요청 1,338건 중 xhigh가 1,050건이었는데, `effortLevel: medium` 동기화는
새 세션부터 적용되므로 효과는 다음 날 로그로 판단한다.

## 모델·CLI 업그레이드 체크리스트

1. 공식 모델 문서와 Codex CLI 릴리스 동작을 확인한다. 추측한 모델 이름을 넣지 않는다.
2. 기존 대표 run ID를 최소 세 종류 고른다: 단일 버그, 다중 파일 기능, 실패 복구.
3. 활성 설정을 바꾸기 전에 `rerun --model <candidate> --effort <level>`로 비교한다.
4. 완료 기준 충족, 테스트 성공, 사람의 재수정 횟수, input/output 토큰, cached 비율, 실행 시간을
   함께 비교한다. 낮은 토큰만으로 승격하지 않는다.
5. 채택하면 `orchestration/models.json`의 활성 설정·라우팅/계약/동시성 임계값·`last_reviewed`·
   `tested_cli_version`을 바꾸고 `history` 끝에 이전 기준과 변경 이유·공식 근거를 남긴다.
6. 역할·작업 계약이 바뀐 경우에만 Claude 자산을 수정한다. 모델 slug만 바뀌면 config 변경으로 끝난다.
7. `python3 register_hooks.py`, `python3 register_orchestration.py`, 관련 단위 테스트,
   `python3 doctor.py`를 실행한다.
8. 문제가 생기면 config의 직전 값으로 되돌리거나 `rerun --model <old-model>`로 비교한다.

권장 승격 원칙은 같은 대표 작업에서 한 단계 낮은 effort도 반드시 비교하는 것이다. OpenAI도
`high`·`xhigh`는 측정된 품질 향상이 있을 때 사용하도록 권장한다. `doctor.py`는
`review_interval_days`(기본 90일)가 지나거나 설치된 Codex CLI가 검증 버전과 달라지면 재검토를
경고한다.
