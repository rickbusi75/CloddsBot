#!/usr/bin/env bash
set -euo pipefail
if [[ -n $(git status --porcelain) ]]; then echo 'Use a clean development checkout first.' >&2; exit 1; fi
suffix=${1:-$(date -u +%Y-%m-%d)}
[[ $suffix =~ ^[A-Za-z0-9._-]+$ ]] || exit 2
git fetch upstream
git fetch origin
# The pristine main mirror remains separate from the personalized branch.
git switch main
git merge --ff-only upstream/main
git switch -c "update/upstream-$suffix" origin/rick
git merge main
echo 'Integration prepared locally. Resolve conflicts if any, run checks, then push this branch and open a PR into rick.'