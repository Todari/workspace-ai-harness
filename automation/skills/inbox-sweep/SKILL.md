---
name: inbox-sweep
description: 디스코드 인박스(/task /idea /note /til /checkin 캡처)를 옵시디언 볼트로 정리한다. task·idea는 추가하고, note는 기존 볼트를 해석해 편집한다(우선순위 낮춤·상태 변경 등). 봇의 직접 기록 실패 시 남은 캡처를 복구한다. /inbox-sweep 또는 Mac의 09:10 크론으로 실행한다. 처리한 항목은 ✅ 리액션으로 ack.
---

# 디스코드 인박스 → 볼트 스위퍼

todari-ops 봇은 `/task`·`/idea`·`/note`를 Git 볼트에 직접 기록한다. 실패하면 Discord에
`📥` 캡처가 남으며 이 스킬이 Mac 볼트에서 복구한다. `/til`·`/checkin` 캡처도 처리한다.
세션 내 볼트 쓰기는 메인 세션만 수행한다. 봇의 원격 변경이 아직 내려오지 않았다면 먼저
Git 상태를 확인하고, 로컬 수정과 충돌하는 pull·덮어쓰기는 하지 않는다.

볼트 경로: `/Users/lth/Library/Mobile Documents/iCloud~md~obsidian/Documents/docs`

## 절차

1. **조회**: `python3 ~/.claude/hooks/workspace-harness/inbox_fetch.py list`
   → `[{id, kind, project, text, timestamp}, ...]`. 0건이면 "인박스 비어있음" 보고 후 종료.
2. **중복 확인** (항목별):
   - `ops_report.py receipt check ID`에 written/skipped 원장이 있으면 본문을 다시 적용하지 않고 ack만 재시도한다.
   - 원장이 없으면 `vault_search.py "inbox:discord:ID"`로 기록 마커를 찾는다. 이미 있으면
     그 노트로 `ops_report.py receipt written ID --note "볼트 상대경로.md"`를 실행하고 ack한다.
     검색 결과가 부분 실패라면 미기록이라고 단정하지 않는다.
   - `kind=quarantine` 또는 지원하지 않는 kind는 편집·ack하지 않고 원문 형식 오류로 보고한다.
3. **분류·기록** (항목별):
   - `kind=task` + `project` 있음 → `repos.py list`의 `vault_note`·별칭으로 허브를 찾고
     `## 다음 할 일`에 `- [ ] <text>`를 추가한다. 섹션이 없으면 끝에 새로 만든다.
     `jeongpyo`/`basetie`→`이정표/이정표.md`, `forcletter`→`포크레터/포크레터.md`.
     레지스트리에 없을 때만 기존 `프로젝트/<project>.md`를 확인한다.
   - `kind=task` + project 없음 → 내용으로 프로젝트를 추정할 수 있으면 해당 노트로,
     아니면 `홈.md`가 아닌 **데일리 인박스**로: 볼트에서 데일리/인박스 노트를 찾아
     (`vault_search.py --list` 활용) 거기 체크박스로 추가. 못 찾으면 `프로젝트/todari-ops.md`
     다음 할 일에 `- [ ] (미분류) <text>`로 넣고 보고에 명시.
   - `kind=idea` → 봇과 같은 `창/아이디어 인박스.md`에 날짜와 원문을 추가한다.
     유사 내용이 있으면 중복 적용하지 않는다. 별도 아이디어 정리는 사용자가 요청할 때 한다.
   - `kind=til` (project 자리에 slug) → 세션 요약이다. 내용에 문제→원인→해결이 있으면
     `공부/트러블슈팅/YYYY-MM-DD 제목.md`, 아니면 `공부/TIL/YYYY-MM-DD 제목.md`
     (기존 /til 스킬과 같은 템플릿·frontmatter, `프로젝트:` 에 slug). 같은 주제 기존 노트가
     있으면 섹션 추가. 단순 작업 나열뿐이고 배움이 없으면 기록하지 말고 ack만 한다.
   - `kind=checkin` (project 자리에 날짜) → `데일리/<날짜>.md` 에 `## 저녁 체크인` 섹션으로
     오늘/막힘/내일 세 줄을 기록 (노트 없으면 데일리 템플릿 형식으로 생성, 이미 섹션이 있으면
     덮지 말고 추가). "막힘" 내용이 트러블슈팅감이면 별도 노트로도 뽑는다.
   - `kind=note` → **자유 지시다. 추가가 아니라 기존 볼트 내용을 해석해 편집한다.**
     아래 [[#note 처리 규칙]]을 따른다. task와 달리 판단이 필요하므로 규칙을 정독할 것.
   - 날짜 표기(`📅 8/5` 등)가 텍스트에 있으면 `📅 YYYY-MM-DD`로 정규화해 유지
     (vault_sync가 D-day로 읽는다).
4. **기록 확인·원장**: 단일 노트는 변경과 함께 `<!-- inbox:discord:ID -->`를 저장하고 다시 읽어 확인한다.
   여러 노트를 바꾸는 항목은 먼저 대상 경로·할 변경을 로컬 복구 메모로 남기고 각 노트 변경을 확인한다.
   **모든 대상 변경을 다시 읽어 검증한 뒤에만**, 마지막으로 대표 노트에 이 완료 마커를 저장한다.
   부분 완료 노트에는 이 마커를 쓰지 않는다. 중단 후 마커가 없으면 복구 메모와 각 노트의 실제 내용을
   대조해 남은 변경만 수행한다. 그 뒤 `python3 ~/.claude/hooks/workspace-harness/ops_report.py receipt written ID --note "상대경로.md"`.
   원장은 내용 대신 경로·해시만 보관한다. 원장 기록 후 로컬 복구 메모를 완료 처리한다.
   이미 같은 내용인 항목은 `receipt skip ID --reason duplicate`, 배움 없는 til은 `--reason no-learning`.
   기록/원장 중간에 실패하면 ack하지 않는다. 재실행 시 원장 또는 마커를 확인해 이어간다.
5. **ack**: 원장까지 기록한 항목만
   `python3 ~/.claude/hooks/workspace-harness/inbox_fetch.py ack ID1,ID2,...`
   실패 항목은 ack 하지 않는다 (다음 실행이 재시도).
6. **동기화**: `python3 ~/.claude/hooks/workspace-harness/vault_sync.py`를 실행해
   봇 쪽 상태도 즉시 갱신한다.
7. **보고**: 처리 n건 — 항목별 `어디에 기록했는지 경로` 목록. 커밋은 하지 않는다
   (obsidian-git이 처리).

## note 처리 규칙

`/note`는 "할 일 추가"가 아니라 **기존 볼트를 바꾸라는 지시**다. 예: "행동대장 도메인 당분간
갱신 안 함, 우선순위 낮춰", "포크레터 가격 노트에 인포크 언더컷이라고 한 줄 추가", "취취 카카오
지원 상태 불합격으로". 처리 원칙은 **가역적·투명·보수적**이다.

**대상 노트 찾기** — project가 있으면 허브 매핑을 따른다: `jeongpyo`/`basetie`→`이정표/이정표.md`,
`forcletter`→`포크레터/포크레터.md`, 그 외는 `프로젝트/<slug>.md`. project가 없으면 내용에서
대상을 추정한다(`vault_search.py`로 관련 노트를 먼저 찾을 것). 못 찾으면 아래 "모호" 규칙으로.

**의도별 편집** — 지시를 해석해 가장 작은 가역적 편집을 한다:
- **우선순위 낮춤 / 보류 / 당분간 안 함** → 대상 노트에 `## 보류` 섹션을 만들고(없으면), 해당
  `## 다음 할 일`의 관련 `- [ ]` 항목을 그 섹션으로 옮긴다. 각 이동 항목 끝에 ` — 보류(YYYY-MM-DD): <사유>`.
  프로젝트 전체가 대상이면 허브 노트 상단에 `> ⏸️ 보류(YYYY-MM-DD): <사유>` 콜아웃을 추가하고,
  노트에 `상태:` frontmatter가 있으면 `보류`로 바꾼다(원값을 콜아웃에 병기).
- **우선순위 올림 / 다시 시작 / 재개** → 위의 역: `## 보류`에서 `## 다음 할 일`로 되돌리고 콜아웃 제거,
  `상태:` 복원.
- **상태·속성 변경** (예: "지원 상태 불합격으로") → 해당 노트 frontmatter의 필드를 바꾼다.
- **내용 추가·수정** (예: "노트에 한 줄 추가") → 지시대로 해당 섹션에 문장을 넣는다.

**모호하거나 되돌리기 어려우면 추측하지 않는다.** 어느 항목·노트인지 불확실하거나, 삭제·대규모
재구성이 필요하거나, 해석이 갈리면 — 편집하지 말고 대상 노트(못 찾으면 데일리 인박스)에
`- [ ] (해석 필요) <원문>` 으로 남기고 보고에 "미처리 사유"를 명시한다. **틀린 편집보다 보류가 낫다.**

**보고** — note 항목은 반드시 "무엇을 어떻게 바꿨는지"를 한 줄로 보고한다(단순 경로가 아니라
before→after 요지). 사용자가 스윕 결과만 보고 되돌릴지 판단할 수 있어야 한다.
성공한 항목만 ack 한다.

## 주의

- iCloud 경로에서 `grep -r` 금지 — 검색은 `vault_search.py`.
- 한글 파일명은 NFD — 경로 비교·생성 시 기존 파일을 Glob으로 먼저 확인.
- 같은 내용이 이미 노트에 있으면 duplicate 원장을 남기고 ack한다.
- `ops_report.py`의 짧은 표기는 모두 `python3 ~/.claude/hooks/workspace-harness/ops_report.py`를 뜻한다.
- 크론은 Mac이 잠들면 건너뛴다. 등록과 실제 성공은 `ops_report.py report`에서 구분한다.
