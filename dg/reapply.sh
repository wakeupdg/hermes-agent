#!/usr/bin/env bash
# Reapply DG's local Hermes patches to the installed Hermes after `hermes update`.
#
# Source of truth: branch dg/local-patches in this fork clone.
# Base tracking:   ref refs/dg/base = the upstream commit our patches sit on.
# Install:         ~/.hermes/hermes-agent (managed by `hermes update`; we only
#                  apply our diff as uncommitted changes on top of it).
# Saved patch:     <install>/.git/dg-applied.patch = exactly what the last run
#                  applied. Every run reverse-applies it first, so a changed
#                  patch, a rebase, or a patch that `hermes update` stashed and
#                  restored never blocks the next apply.
#
# Usage: dg/reapply.sh            revert the saved patch, rebase onto the installed
#                                 version when it moved, apply the new diff
#        dg/reapply.sh --check    dry run: report what would happen, change nothing
#        dg/reapply.sh --revert   remove our patch from the install (run before `hermes update`)
set -euo pipefail

FORK="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL="${HERMES_INSTALL:-$HOME/.hermes/hermes-agent}"
BRANCH="dg/local-patches"
MODE=apply
case "${1:-}" in
  "") ;;
  --check) MODE=check ;;
  --revert) MODE=revert ;;
  *) echo "usage: $0 [--check|--revert]" >&2; exit 64 ;;
esac
# dg/ holds tooling and plans; tests/ is not needed at runtime. Never ship either.
DIFF_PATHS=(-- . ':(exclude)dg' ':(exclude)tests')
SAVED="$(git -C "$INSTALL" rev-parse --absolute-git-dir)/dg-applied.patch"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# State of a patch in the install working tree: applied | absent | mixed.
patch_state() {
  if git -C "$INSTALL" apply --reverse --check "$1" 2>/dev/null; then echo applied
  elif git -C "$INSTALL" apply --check "$1" 2>/dev/null; then echo absent
  else echo mixed; fi
}

# Dry run of "reverse <old>, then apply <new>" on copies of the touched files.
simulate() {  # simulate <old-or-empty> <new>
  local sim="$TMP/sim" f
  mkdir -p "$sim"
  for f in $( { if [[ -n "$1" ]]; then git apply --numstat "$1"; fi; git apply --numstat "$2"; } | cut -f3 | sort -u); do
    if [[ -e "$INSTALL/$f" ]]; then mkdir -p "$sim/$(dirname "$f")"; cp -p "$INSTALL/$f" "$sim/$f"; fi
  done
  ( cd "$sim" && { [[ -z "$1" ]] || git apply --reverse "$1"; } && git apply --check "$2" )
}

mixed_help() {
  echo "The install holds a partial or conflicting copy of the saved patch ($SAVED)." >&2
  echo "Inspect with: git -C \"$INSTALL\" status / diff. Restore the touched files with" >&2
  echo "  git -C \"$INSTALL\" checkout -- <files>   (and delete files the patch added)," >&2
  echo "then remove $SAVED and run dg/reapply.sh again. Nothing changed." >&2
}

saved_state=none
if [[ -f "$SAVED" ]]; then saved_state="$(patch_state "$SAVED")"; fi

# --revert: take our patch out of the install and stop.
if [[ "$MODE" == revert ]]; then
  case "$saved_state" in
    none) echo "No saved patch at $SAVED. Nothing to revert." ;;
    absent) echo "The saved patch is not in the install (already reverted)."; rm -f "$SAVED" ;;
    applied) git -C "$INSTALL" apply --reverse "$SAVED"; rm -f "$SAVED"
             echo "Reverted. The install is plain upstream again; restart the Hermes TUI and the gateway to load it." ;;
    mixed) mixed_help; exit 3 ;;
  esac
  exit 0
fi

cd "$FORK"
[[ -z "$(git status --porcelain)" ]] || { echo "Fork has uncommitted changes; commit or stash first." >&2; exit 1; }

new_base="$(git -C "$INSTALL" rev-parse HEAD)"
old_base="$(git rev-parse refs/dg/base)"
echo "Installed Hermes: $(git -C "$INSTALL" describe --tags --always "$new_base")"
echo "Patches based on: $(git describe --tags --always "$old_base" 2>/dev/null || echo "$old_base")"
echo "Saved patch in install: $saved_state"
if [[ "$saved_state" == mixed ]]; then mixed_help; exit 3; fi

patch="$TMP/dg.patch"
build_patch() {
  base_for_diff="$(git rev-parse refs/dg/base)"
  git diff --binary "$base_for_diff" "$BRANCH" "${DIFF_PATHS[@]}" > "$patch"
}

# Fast path: same base, and exactly this patch is already in the install.
if [[ "$new_base" == "$old_base" && "$saved_state" == applied ]]; then
  build_patch
  if cmp -s "$SAVED" "$patch"; then echo "This exact patch is already applied. Nothing to do."; exit 0; fi
fi

# 1. Take the previously applied patch out BEFORE any rebase (the rebase destroys the
#    old diff). This also covers a patch `hermes update` stashed and restored.
if [[ "$saved_state" == applied && "$MODE" == apply ]]; then
  git -C "$INSTALL" apply --reverse "$SAVED"
  echo "Reverted the previously applied patch."
fi

# 2. Make sure the fork has the installed commit, then rebase when the install moved.
if ! git cat-file -e "$new_base^{commit}" 2>/dev/null; then
  echo "Fetching installed commit from upstream..."
  git fetch upstream "$new_base" --no-tags 2>/dev/null || git fetch upstream --tags
fi
if [[ "$new_base" != "$old_base" ]]; then
  if [[ "$MODE" == check ]]; then
    echo "[check] Would rebase $BRANCH from $old_base onto $new_base (the check below uses the pre-rebase diff)."
  else
    git checkout -q "$BRANCH"
    if ! git rebase --onto "$new_base" "$old_base" "$BRANCH"; then
      if [[ "$saved_state" == applied ]]; then rm -f "$SAVED"; fi
      echo "CONFLICT while rebasing patches onto the new Hermes version. The install is unpatched now." >&2
      echo "Resolve in $FORK (git status), then: git rebase --continue && git update-ref refs/dg/base $new_base && dg/reapply.sh" >&2
      exit 2
    fi
    git update-ref refs/dg/base "$new_base"
    git push -q --force-with-lease origin "$BRANCH" || echo "(push to fork failed; run git push later)"
  fi
fi

# 3. Build the new diff and apply it as uncommitted changes.
build_patch
if [[ ! -s "$patch" ]]; then
  if [[ "$MODE" == apply ]]; then rm -f "$SAVED"; fi
  echo "No patches on $BRANCH yet. Nothing to apply."; exit 0
fi
git diff --stat "$base_for_diff" "$BRANCH" "${DIFF_PATHS[@]}"

if [[ "$MODE" == check ]]; then
  old=""; if [[ "$saved_state" == applied ]]; then old="$SAVED"; fi
  if simulate "$old" "$patch"; then
    echo "[check] Patch applies cleanly${old:+ after reverting the saved patch}. Nothing changed."; exit 0
  fi
  echo "[check] Patch would NOT apply cleanly (uncommitted edits in the install?). Nothing changed." >&2; exit 3
fi

cd "$INSTALL"
if ! git apply --check "$patch"; then
  echo "Patch does not apply cleanly to the install (uncommitted edits there?)." >&2
  if [[ "$saved_state" == applied ]] && git apply "$SAVED"; then
    echo "Restored the previously applied patch." >&2
  fi
  exit 3
fi
git apply "$patch"
cp "$patch" "$SAVED"
echo "Applied. Restart the Hermes TUI (/exit, then hermes --continue) AND the gateway (hermes gateway restart): both load tools/."
