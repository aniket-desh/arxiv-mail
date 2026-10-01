#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *) printf '%s\n' "${GITHUB_TOKEN:?GITHUB_TOKEN is required}" ;;
esac
