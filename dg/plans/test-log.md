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

**Rebuilt env.** The first env had no `numpy`/`sounddevice`, so most of `test_tts_streaming.py` and part of
`test_voice_mode.py` were silently skipped. The env was rebuilt with
`... --group dev --group test --extra audio-io --extra sms` (`audio-io` = numpy + sounddevice; `sms` = aiohttp).
Baseline on the base code with that env: 411 files, 3426 passed, **8 failed**, 38 skipped:

- `tests/tools/test_voice_mode.py::TestPlayBeep::test_beep_calls_sounddevice_play` (macOS host: beeps go through afplay,
  the test expects `sd.play`);
- the same `test_served_profile_child_env.py` failure and the same 6 `test_tui_gateway_server.py` failures as above.
  The 7 `aiohttp` collection errors are gone.

This 8-test set is **the baseline**. "Green" below means: no failure outside it.

## W0 — `dg/reapply.sh`

| Run | Command | Result |
|---|---|---|
| red (old script) | `REAPPLY_SCRIPT=<HEAD:dg/reapply.sh> bash dg/tests/test_reapply.sh` | FAIL at scenario 2: "tests shipped" (exit 1) |
| green (new script) | `bash dg/tests/test_reapply.sh` | all 21 checks passed (9 scenarios: no patch, first apply, idempotent rerun, `--check` no-op, changed patch, stash-restored patch after an upstream move + rebase + push, `--revert`, dropped stash, hand-edited install refused) |

## W1 — speaker pipeline (`tools/tts_tool_speaker.py`)

New file `tests/tools/test_tts_speaker_backpressure.py` (5 tests). Red = base `tools/tts_tool_speaker.py`
(`git show HEAD:tools/tts_tool_speaker.py`), `HERMES_TEST_FILE_RETRIES=0`.

| Test | Base | Fix |
|---|---|---|
| `test_long_sentence_queued_behind_slow_playback_is_not_truncated` (S3 §7.1) | FAIL: prefetch stalled at the 64-chunk bound | pass |
| `test_stop_releases_a_speak_blocked_on_prefetch_capacity` (S3 §7.2/7.3; deterministic 4th-acquire sync point, the fix the #76655 sweeper asked for) | FAIL: `speak()` still blocked after stop | pass |
| `test_stop_between_wav_write_and_play_never_starts_the_player` (S3 §7.4) | FAIL: player called once | pass |
| `test_prefetch_failure_log_names_the_exception_and_sentence` (S3 §7.8) | FAIL: no exception type in the line | pass |
| `test_stop_during_a_portaudio_sentence_lets_finish_return` (review §4.13) | FAIL: `finish()` still running after 5 s (10 s prefetch join) | pass |

Summary: base 0 passed / 5 failed (14.2 s); fix 5 passed (1.4 s).
Related suites after W1 (`test_tts_streaming`, `test_tts_macos_output`, `test_voice_mode`, `test_voice_cli_integration`,
`test_chat_turn_streaming_tts_arming`): 127 passed, 1 failed = baseline `test_beep_calls_sounddevice_play`.

## W2 — turn drain loop (`hermes_cli/cli_chat_turn_mixin.py`)

New file `tests/hermes_cli/test_chat_turn_tts_drain.py` (6 tests, fake speaker thread on a fake clock, entry point
`_chat_settle_turn`). Red = base `cli_chat_turn_mixin.py`.

| Test | Base | Fix |
|---|---|---|
| `test_reply_longer_than_two_minutes_is_not_cut` (S3 §7.9, review §4.5) | FAIL (join gave up at 120 s) | pass |
| `test_message_typed_during_playback_cuts_the_audio` (review §4.4, M3) | FAIL (stop never set) | pass |
| `test_stopped_pipeline_that_does_not_wind_down_is_left_after_the_grace` | FAIL (waited 120 s) | pass |
| `test_stall_watchdog_stops_a_silent_pipeline` | FAIL (no stop, 120 s) | pass |
| `test_app_exit_leaves_the_drain` | FAIL (ignored `_should_exit`) | pass |
| `test_tts_done_set_by_another_writer_does_not_cut_the_reply` (review §4.6) | pass (guard: the base joined the thread too) | pass |

Summary: base 1 passed / 5 failed; fix 6 passed. `test_chat_turn_streaming_tts_arming.py` + `test_voice_cli_integration.py`
+ the new file: 41 passed.

Note: the baseline failure `test_beep_calls_sounddevice_play` was not investigated (it fails on the base code on this host).

## W1b — killed player is a stop (`tools/voice_mode.py`, from sweep-4 §3.0)

New file `tests/tools/test_voice_player_signal_stop.py` (4 tests). Red = base `tools/voice_mode.py`.

| Test | Base | Fix |
|---|---|---|
| `test_player_killed_by_a_signal_launches_no_other_player[-15]` | FAIL (fallback player launched) | pass |
| `test_player_killed_by_a_signal_launches_no_other_player[-9]` | FAIL | pass |
| `test_stop_playback_mid_sentence_plays_nothing_else` (real child process, `platforms("posix")`) | FAIL (second player launched) | pass |
| `test_player_that_genuinely_fails_still_falls_through` (rc=1 guard) | pass | pass |

Summary: base 1 passed / 3 failed; fix 4 passed. `test_voice_mode.py` + `test_tts_macos_output.py` + new file:
only the baseline `test_beep_calls_sounddevice_play` fails.
