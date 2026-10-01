#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  export GIT_ASKPASS="$script_dir/git-askpass.sh"
  export GIT_TERMINAL_PROMPT=0
fi

state_file="${1:-state/state.json}"
test -f "$state_file"

git config user.name "arxivmail bot"
git config user.email "arxivmail-bot@users.noreply.github.com"

state_blob="$(git hash-object -w "$state_file")"
state_tree="$(printf '100644 blob %s\tstate.json\n' "$state_blob" | git mktree)"
state_parent="$(git show-ref --verify --hash refs/remotes/origin/state 2>/dev/null || true)"
state_message="arxivmail state $(date -u +%Y-%m-%dT%H:%M:%SZ)"
state_commit="$(printf '%s\n' "$state_message" | git commit-tree "$state_tree")"

if [[ -n "$state_parent" ]]; then
  git push --force-with-lease="refs/heads/state:$state_parent" origin "$state_commit:refs/heads/state"
else
  git push origin "$state_commit:refs/heads/state"
fi

git update-ref refs/remotes/origin/state "$state_commit"
echo "Persisted a historyless delivery-state snapshot without changing the source branch."
