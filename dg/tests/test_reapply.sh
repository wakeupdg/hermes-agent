#!/usr/bin/env bash
# Scenario test for dg/reapply.sh against a throwaway upstream/fork/install trio in $TMPDIR.
# Never touches the real install: HERMES_INSTALL points at the scratch install.
# Usage: dg/tests/test_reapply.sh      (exit 0 = all scenarios passed)
set -euo pipefail

SCRIPT_SRC="${REAPPLY_SCRIPT:-$(cd "$(dirname "$0")/.." && pwd)/reapply.sh}"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dg-reapply-test.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_NOSYSTEM=1 HOME="$ROOT/home"; mkdir -p "$HOME"
git config --global init.defaultBranch main
git config --global advice.detachedHead false

U="$ROOT/upstream" F="$ROOT/fork" O="$ROOT/origin.git" I="$ROOT/install"
pass=0
ok() { echo "  ok - $1"; pass=$((pass + 1)); }
fail() { echo "  FAIL - $1" >&2; exit 1; }
run() { HERMES_INSTALL="$I" "$F/dg/reapply.sh" "$@"; }
state() { git -C "$I" status --porcelain; git -C "$I" diff | shasum; }

# upstream commit A
git init -q "$U"; mkdir -p "$U/tools" "$U/tests"
printf 'line1\nvalue = 1\nline3\n' > "$U/tools/a.py"
printf 'b1\nb2\nb3\n' > "$U/tools/b.py"
printf 'test\n' > "$U/tests/t.py"
git -C "$U" add -A; git -C "$U" commit -qm A; A="$(git -C "$U" rev-parse HEAD)"
git -C "$U" tag v1

git init -q --bare "$O"
git clone -q "$U" "$F"; git -C "$F" remote rename origin upstream; git -C "$F" remote add origin "$O"
git -C "$F" checkout -qb dg/local-patches; git -C "$F" update-ref refs/dg/base "$A"
mkdir -p "$F/dg"; cp "$SCRIPT_SRC" "$F/dg/reapply.sh"; chmod +x "$F/dg/reapply.sh"
git -C "$F" add -A; git -C "$F" commit -qm "dg tooling"; git -C "$F" push -q origin dg/local-patches
git clone -q "$U" "$I"; git -C "$I" checkout -q --detach "$A"

echo "1. no patches yet"
out="$(run)"; [[ "$out" == *"No patches"* ]] && ok "nothing to apply" || fail "$out"

echo "2. first apply (tests/ and dg/ excluded, new file shipped)"
sed -i.bak 's/value = 1/value = 2/' "$F/tools/a.py"; rm "$F/tools/a.py.bak"
printf 'new\n' > "$F/tools/new.py"; printf 'changed\n' > "$F/tests/t.py"
git -C "$F" add -A; git -C "$F" commit -qm "patch 1"
run >/dev/null
grep -q 'value = 2' "$I/tools/a.py" && ok "a.py patched" || fail "a.py"
[[ -f "$I/tools/new.py" ]] && ok "new file added" || fail "new.py"
grep -q '^test$' "$I/tests/t.py" && ok "tests/ not shipped" || fail "tests shipped"
[[ ! -e "$I/dg" ]] && ok "dg/ not shipped" || fail "dg shipped"
[[ -f "$I/.git/dg-applied.patch" ]] && ok "patch saved" || fail "no saved patch"

echo "3. second run with the same patch is a no-op"
out="$(run)"; [[ "$out" == *"already applied"* ]] && ok "idempotent" || fail "$out"

echo "4. --check changes nothing"
before="$(state)"; run --check >/dev/null; [[ "$(state)" == "$before" ]] && ok "install unchanged" || fail "check mutated"

echo "5. changed patch on the same base: revert old, apply new"
sed -i.bak 's/value = 2/value = 3/' "$F/tools/a.py"; rm "$F/tools/a.py.bak"
git -C "$F" add -A; git -C "$F" commit -qm "patch 2"
out="$(run --check)"; [[ "$out" == *"applies cleanly after reverting"* ]] && ok "check predicts revert+apply" || fail "$out"
run >/dev/null
grep -q 'value = 3' "$I/tools/a.py" && ok "new patch in place" || fail "a.py: $(cat "$I/tools/a.py")"
[[ -f "$I/tools/new.py" ]] && ok "new file still present" || fail "new.py lost"

echo "6. hermes update kept the patch (stash -u, move to B, stash pop)"
printf 'b1\nB2\nb3\n' > "$U/tools/b.py"; git -C "$U" commit -qam B; B="$(git -C "$U" rev-parse HEAD)"
git -C "$I" stash -q -u; git -C "$I" fetch -q origin; git -C "$I" checkout -q --detach "$B"; git -C "$I" stash pop -q
out="$(run --check)"; [[ "$out" == *"Would rebase"* ]] && ok "check reports the rebase" || fail "$out"
[[ "$(git -C "$F" rev-parse refs/dg/base)" == "$A" ]] && ok "check did not rebase" || fail "check rebased"
run >/dev/null
[[ "$(git -C "$F" rev-parse refs/dg/base)" == "$B" ]] && ok "fork rebased onto B" || fail "base not moved"
grep -q 'value = 3' "$I/tools/a.py" && grep -q 'B2' "$I/tools/b.py" && ok "patch on top of B" || fail "content"
[[ "$(git -C "$O" rev-parse dg/local-patches)" == "$(git -C "$F" rev-parse dg/local-patches)" ]] && ok "branch pushed" || fail "not pushed"

echo "7. --revert restores plain upstream"
run --revert >/dev/null
[[ -z "$(git -C "$I" status --porcelain)" ]] && ok "install clean" || fail "$(git -C "$I" status --porcelain)"
[[ ! -f "$I/.git/dg-applied.patch" ]] && ok "saved patch removed" || fail "saved patch left"
out="$(run --revert)"; [[ "$out" == *"Nothing to revert"* ]] && ok "second revert is a no-op" || fail "$out"

echo "8. hermes update answered n (patch dropped) -> plain reapply"
run >/dev/null; git -C "$I" stash -q -u; git -C "$I" stash drop -q
run >/dev/null
grep -q 'value = 3' "$I/tools/a.py" && ok "reapplied after a dropped stash" || fail "not reapplied"

echo "9. hand-edited patched line -> refuse, change nothing"
sed -i.bak 's/value = 3/value = 99/' "$I/tools/a.py"; rm "$I/tools/a.py.bak"
before="$(state)"
set +e; run >/dev/null 2>&1; rc=$?; set -e
[[ $rc -eq 3 && "$(state)" == "$before" ]] && ok "mixed state refused (exit 3), untouched" || fail "rc=$rc"

echo "all $pass checks passed"
