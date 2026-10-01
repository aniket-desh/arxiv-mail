#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  export GIT_ASKPASS="$script_dir/git-askpass.sh"
  export GIT_TERMINAL_PROMPT=0
fi

state_file="${1:-state/state.json}"
mkdir -p "$(dirname "$state_file")"

if git ls-remote --exit-code --heads origin state >/dev/null 2>&1; then
  git fetch origin +refs/heads/state:refs/remotes/origin/state
  state_temp="${state_file}.tmp"
  git show refs/remotes/origin/state:state.json > "$state_temp"
  mv "$state_temp" "$state_file"
  chmod 600 "$state_file"
  echo "Restored delivery state from the private state branch."
else
  remote_status=$?
  if [[ "$remote_status" -eq 2 ]]; then
    echo "No state branch exists yet; starting with empty delivery history."
  else
    echo "Could not verify the private state branch; refusing to continue with empty history." >&2
    exit "$remote_status"
  fi
fi
