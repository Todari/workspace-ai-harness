---
description: 자동 분류와 관계없이 이번 작업을 Codex worker에 강제 위임한다.
argument-hint: <개발 작업>
---

<!-- workspace-harness: managed orchestration -->

자동 라우팅 결과와 관계없이 다음 작업을 Fable 설계 → Codex 구현 흐름으로 처리한다: $ARGUMENTS

1. 현재 경로의 git 루트·브랜치·dirty 상태만 확인한다. 기존 변경을 보존한다.
2. 메인 Fable 세션은 파일을 읽거나 편집하지 않고 사용자 요청만으로 3,000자 이내 작업 계약을 만든다.
3. 보통 구현은 `worker_effort: "high"`, 난도가 실제로 높은 구현만 `"xhigh"`로 둔다.
4. 작업 계약을 stdin으로 전달해 아래 실행기를 `--detach`로 한 번 시작한다. run_id가 즉시 돌아온다.
   실행 중 같은 worktree를 편집하거나 다른 writer를 동시에 띄우지 않는다.

```bash
python3 ~/.claude/hooks/workspace-harness/codex_worker.py run \
  --repo "$(git rev-parse --show-toplevel)" \
  --orchestrator-session <HARNESS_ROUTE에 표시된 session> --manifest - --detach <<'JSON'
{
  "task_mode": "implement",
  "task_class": "medium",
  "objective": "구체적인 구현 목표",
  "worker_effort": "high",
  "scope": ["수정이 허용된 상대 경로 또는 디렉터리"],
  "acceptance_criteria": ["관찰 가능한 완료 기준"],
  "constraints": ["기존 변경 보존", "관련 없는 리팩터링 금지"],
  "verification_commands": ["실제로 실행할 가장 작은 검증 명령"]
}
JSON
```

5. 결과는 `python3 ~/.claude/hooks/workspace-harness/codex_worker.py wait <run_id> --timeout 540`을
   foreground로 호출해 기다린다. `status: "running"`이면 같은 wait를 다시 호출한다(최대 8회).
   `run_in_background`·`&`·로그 redirect·다른 도구로 폴링하지 않는다. 반환된 압축 JSON만 사용하고
   원시 로그나 레포를 다시 읽지 않는다.
6. 결과 상태와 관계없이 동일 턴에서 추가 도구나 `resume`을 실행하지 않고 구조화 결과를 보고한다.
7. 이후 사용자가 실패 원인을 보완하라고 요청하면 저장된 run ID로 수동 `resume`한다. 모델 비교나
   처음부터 재실행해야 하면 `rerun <run_id> --model <모델> --effort <effort>`를 사용한다.
8. 사용자 요청 없이는 커밋·푸시·PR·배포하지 않는다.

최종 응답에는 run ID, 사용 모델/effort, 변경 파일, 검증 결과, 남은 위험만 간결하게 보고한다.
