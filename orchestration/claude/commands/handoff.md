---
description: 현재 세션의 작업 상태를 인계 노트로 저장해 같은 경로의 다음 세션에 자동 주입한다.
argument-hint: [보충 메모]
---

<!-- workspace-harness: managed orchestration -->

이 세션을 끝내고 다음 세션에 넘길 인계 노트를 저장한다. 보충 메모: $ARGUMENTS

1. 대화에서 확인된 사실만으로 아래 절을 채운다. 추측·계획 문서는 넣지 않는다.
   `## 목표` `## 결정`(이유 포함) `## 진행`(완료·미완료) `## 검증 상태`(실행한 검증과 결과, 못 돌린 것)
   `## 다음 할 일`(명령·파일 경로까지) `## 주의`(함정·미커밋 변경·비밀 취급)
2. heredoc으로 저장한다. `--cwd`는 현재 작업 경로, `--session`은 이 세션 id다.
   `python3 ~/.claude/hooks/workspace-harness/session_handoff.py write --cwd <cwd> --session <id> <<'EOF'`
3. 메모리·볼트에 남길 결정이 있으면 남긴다.
4. 사용자에게 같은 경로에서 새 세션을 열면 노트가 자동 주입된다고 한 줄로 알린다.
