# Test log — plan-v2 build

Environment: fork `hermes-agent-dg`, branch `dg/local-patches`, base `3b42157858` (v0.23.1).
Test interpreter built in the fork with
`HERMES_HOME=$TMPDIR/hermes-test-home HERMES_RUNTIME_DIR=$TMPDIR/hermes-test-runtime /usr/local/bin/python3.14 -m pm.build_env --source . --out .venv --group dev --group test`
(Python 3.14.9 in the built env). Tests run with `scripts/run_tests.sh <paths>` and
`HERMES_PYTHON=.venv/bin/python`, same `HERMES_HOME`/`HERMES_RUNTIME_DIR`. Nothing under `~/.hermes` was used or written.

Red-on-base method: the new test file is run against the base code (the workstream's
source change stashed / the file checked out from the parent commit), then against the fix.

## Baseline: must-stay-green list on the base commit

`scripts/run_tests.sh <the 12 paths from plan W6.5>` on `3b42157858`:
411 files, 3352 passed, 7 failed, 7 errors, 50 skipped. All 14 are pre-existing and unrelated:

- 7 collection errors, `ModuleNotFoundError: No module named 'aiohttp'` (not in the dev/test groups):
  `tests/tui_gateway/test_groups_renewable_forward.py`, `test_hosted_room_idle_maintenance.py`,
  `test_hosted_room_peer_approvals.py`, `test_hosted_room_retry_lease.py`, `test_hosted_room_revoke_deadline.py`,
  `test_hosted_room_two_gateway_peer.py`, `test_hosted_room_two_gateway_scoped.py`.
- `tests/tui_gateway/test_served_profile_child_env.py::test_helper_children_resolve_secrets_through_the_served_profile`
  (`key_cmd ... exited 127`, host tool missing).
- `tests/tui_gateway/test_tui_gateway_server.py`: `test_agent_build_failure_surfaces_error_and_drops_turn`,
  `test_cancelled_turn_before_agent_ready_emits_error_event`, `test_interrupt_before_agent_ready_prevents_late_turn_start`,
  `test_session_not_running_before_agent_ready_emits_error_event`, `test_slow_agent_build_delivers_prompt_instead_of_timing_out`,
  `test_slow_agent_build_emits_keyed_progress_notice`.

"Green" below means: no failure outside this baseline set.

## W0 — `dg/reapply.sh`

| Run | Command | Result |
|---|---|---|
| red (old script) | `REAPPLY_SCRIPT=<HEAD:dg/reapply.sh> bash dg/tests/test_reapply.sh` | FAIL at scenario 2: "tests shipped" (exit 1) |
| green (new script) | `bash dg/tests/test_reapply.sh` | all 21 checks passed (9 scenarios: no patch, first apply, idempotent rerun, `--check` no-op, changed patch, stash-restored patch after an upstream move + rebase + push, `--revert`, dropped stash, hand-edited install refused) |
