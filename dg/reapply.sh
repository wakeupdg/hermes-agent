#!/usr/bin/env bash
# Reapply DG's local Hermes patches to the installed Hermes after `hermes update`.
#
# Source of truth: branch dg/local-patches in this fork clone.
# Base tracking:   ref refs/dg/base = the upstream commit our patches sit on.
# Install:         ~/.hermes/hermes-agent (managed by `hermes update`; we only
#                  apply our diff as uncommitted changes on top of it).
#
# Usage: dg/reapply.sh            rebase onto installed version + apply
#        dg/reapply.sh --check    dry run: report what would happen, change nothing
set -euo pipefail

FORK="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL="${HERMES_INSTALL:-$HOME/.hermes/hermes-agent}"
BRANCH="dg/local-patches"
CHECK=0; [[ "${1:-}" == "--check" ]] && CHECK=1
# dg/ holds tooling only; never ship it into the install.
DIFF_PATHS=(-- . ':(exclude)dg')

cd "$FORK"
[[ -z "$(git status --porcelain)" ]] || { echo "Fork has uncommitted changes; commit or stash first." >&2; exit 1; }

new_base="$(git -C "$INSTALL" rev-parse HEAD)"
old_base="$(git rev-parse refs/dg/base)"
echo "Installed Hermes: $(git -C "$INSTALL" describe --tags --always "$new_base")"
echo "Patches based on: $(git describe --tags --always "$old_base" 2>/dev/null || echo "$old_base")"

# 1. Make sure the fork has the installed commit.
if ! git cat-file -e "$new_base^{commit}" 2>/dev/null; then
  echo "Fetching installed commit from upstream..."
  git fetch upstream "$new_base" --no-tags 2>/dev/null || git fetch upstream --tags
fi

# 2. Rebase our patches onto the installed version when it moved.
if [[ "$new_base" != "$old_base" ]]; then
  if (( CHECK )); then
    echo "[check] Would rebase $BRANCH from $old_base onto $new_base."
  else
    git checkout -q "$BRANCH"
    if ! git rebase --onto "$new_base" "$old_base" "$BRANCH"; then
      echo "CONFLICT while rebasing patches onto the new Hermes version." >&2
      echo "Resolve in $FORK (git status), then: git rebase --continue && git update-ref refs/dg/base $new_base && dg/reapply.sh" >&2
      exit 2
    fi
    git update-ref refs/dg/base "$new_base"
    git push -q --force-with-lease origin "$BRANCH" || echo "(push to fork failed; run git push later)"
  fi
fi

# 3. Apply our diff to the install as uncommitted changes.
base_for_diff="$(git rev-parse refs/dg/base)"
patch="$(mktemp)"; trap 'rm -f "$patch"' EXIT
git diff "$base_for_diff" "$BRANCH" "${DIFF_PATHS[@]}" > "$patch"
if [[ ! -s "$patch" ]]; then echo "No patches on $BRANCH yet. Nothing to apply."; exit 0; fi
git diff --stat "$base_for_diff" "$BRANCH" "${DIFF_PATHS[@]}"

cd "$INSTALL"
if git apply --reverse --check "$patch" 2>/dev/null; then
  echo "Patches already applied to the install. Nothing to do."; exit 0
fi
if ! git apply --check "$patch"; then
  echo "Patch does not apply cleanly to the install (uncommitted edits there?). Nothing changed." >&2; exit 3
fi
if (( CHECK )); then echo "[check] Patch applies cleanly."; exit 0; fi
git apply "$patch"
echo "Applied. Restart Hermes (/exit, then hermes --continue) to load the changes."
