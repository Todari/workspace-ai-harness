---
description: Opt-in bounded ultracode mode — small slice first, Codex implementation workers, no expansion without approval.
---

<!-- workspace-harness: managed orchestration -->

Use bounded ultracode mode for this task: $ARGUMENTS

Hard constraints:

- Do not run across the whole repository yet.
- First run on at most three representative files or one directory.
- No nested agents and no worker-created workers.
- Use Fable only for initial architecture or final high-risk judgment.
- Route implementation to `codex_worker.py`; use Codex high by default and
  xhigh only for difficult leaf work with a measured quality benefit.
- Allow only one writer per worktree. Independent parallel workers require
  separate git worktrees.
- After the small slice, report Codex run IDs, model/effort, token usage,
  findings, and whether a full run is justified.
- Do not expand to the full workflow without explicit user approval.

Report the small-slice result in this shape before proposing anything further:

```json
{
  "small_slice_scope": [],
  "codex_runs": [],
  "token_usage_summary": "",
  "findings": [],
  "full_run_recommended": false,
  "full_run_plan": null
}
```
