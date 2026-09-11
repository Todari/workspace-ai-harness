#!/bin/zsh
# Called through ops_report.py run; exit status must reach the job receipt.
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HARNESS_DIR=${0:A:h:h}
VAULT="${OBSIDIAN_VAULT_PATH:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/docs}"
WORKSPACE="${WORKSPACE_HARNESS_ROOT:-$HOME/workspace}"
case "${1:-}" in
  inbox-sweep) turns=30 ;;
  weekly-review) turns=40 ;;
  *) echo 'supported jobs: inbox-sweep, weekly-review' >&2; exit 2 ;;
esac
[[ -r "$VAULT/홈.md" ]] || { echo 'vault unavailable' >&2; exit 1; }
CLAUDE_BIN="$HOME/.local/bin/claude"
[[ -x "$CLAUDE_BIN" ]] || { echo 'Claude executable unavailable at ~/.local/bin/claude' >&2; exit 127; }
if [[ "$1" == 'inbox-sweep' ]]; then
  count=$(python3 "$HARNESS_DIR/inbox_fetch.py" count) || exit $?
  [[ "$count" == '0' ]] && { echo 'inbox empty'; exit 0; }
fi
cd "$WORKSPACE"
"$CLAUDE_BIN" -p "/$1" --model sonnet --effort medium --max-turns "$turns" \
  --add-dir "$VAULT" \
  --allowedTools 'Read,Glob,Grep,Write,Edit,Bash(git:*),Bash(date:*),Bash(ls:*),Bash(find:*),Bash(head:*),Bash(stat:*),Bash(wc:*),Bash(python3:*)'

# A CLI exit 0 alone does not prove that the requested artifact exists.
if [[ "$1" == 'weekly-review' ]]; then
  [[ -s "$VAULT/회고/$(date +%G-W%V).md" ]] || { echo 'weekly review artifact missing' >&2; exit 1; }
else
  remaining=$(python3 "$HARNESS_DIR/inbox_fetch.py" count) || exit $?
  [[ "$remaining" == '0' ]] || { echo 'inbox has unresolved items' >&2; exit 1; }
fi
