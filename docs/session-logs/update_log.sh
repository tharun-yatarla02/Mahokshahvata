#!/bin/sh
# Writes docs/session-logs/<timestamp>.md: every commit so far + open backlog.
# Run by the SessionEnd hook in .claude/settings.json; safe to run by hand.
cd "$(dirname "$0")/../.." || exit 0
dir=docs/session-logs
head=$(git rev-parse --short HEAD)
dirty=$(git status --porcelain -- . ":!$dir")

# Nothing new since the last log -> don't write a duplicate.
last=$(ls "$dir"/*.md 2>/dev/null | tail -1)
[ -n "$last" ] && [ -z "$dirty" ] && grep -q "HEAD: $head" "$last" && exit 0

out="$dir/$(date +%Y-%m-%d_%H%M%S).md"
{
  echo "# Session log — $(date '+%Y-%m-%d %H:%M')"
  echo
  echo "HEAD: $head ($(git branch --show-current)) — $(git rev-list --count HEAD) commits"
  echo
  echo "## Commits so far (newest first)"
  echo
  git log --date=short --pretty='- `%h` %ad — %s'
  echo
  echo "## Uncommitted changes"
  echo
  if [ -n "$dirty" ]; then echo '```'; echo "$dirty"; echo '```'; else echo "None."; fi
  echo
  echo "## Backlog (from docs/PROJECT_STATUS.md)"
  echo
  # ponytail: copies sections by heading; if PROJECT_STATUS.md headings change, update these patterns.
  awk '/^Other things seen/{p=1} /^## Recommendation/{p=0} p' docs/PROJECT_STATUS.md | sed 's/^#/##/'
  grep -E '\*\*[^*]+:\*\* open' docs/PROJECT_STATUS.md | sed 's/^[0-9]*\. /- /'
} > "$out"
