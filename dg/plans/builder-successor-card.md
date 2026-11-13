# Builder successor card — plan-v2 build (paused at DG's request)

Written by the first builder when DG asked it to pause. Everything listed as done is committed, tested and pushed.
The working tree is clean.

## 1. Branch state

- Repo: `/Users/dganesh/!MY PROJECTS/hermes-agent-dg`, branch `dg/local-patches`, pushed to `origin` (wakeupdg/hermes-agent).
- HEAD: `6bd65b86b34a575b01ff760af3d4fff8d1d301f9` (same on `origin/dg/local-patches`).
- `refs/dg/base` = `3b42157858` (v0.23.1). The install `~/.hermes/hermes-agent` was never touched (still clean at v0.23.1, no saved patch).

`git log --oneline 3b42157858..HEAD`:
```
6bd65b86b3 feat(cli/voice): Escape stops TTS while it is pending or audible
3568e293ae fix(cli/voice): Ctrl+C / Ctrl+Q stop speech without exiting the app
8c990d54cb fix(cli/voice): the record key cuts TTS and records at any point in a turn
6879c83e64 fix(voice): a player killed by a signal is a stop, not a failed player
ea6cc1edb0 fix(cli): drain streaming TTS by polling the speaker thread, not a 120 s join
f619f23fc5 fix(tts): no truncation of long queued sentences; stop never strands the speaker pipeline
113f98e18f dg(W0): idempotent reapply.sh (saved patch, --revert, tests/ excluded) + plans
5cb7923f15 dg: patch-tracking tooling (reapply script + README)   <- pre-existing, not this build
```

## 2. Workstreams

| WS | Status | Commit | Notes |
|---|---|---|---|
| W0 reapply.sh + plans | done | `113f98e18f` | saved patch at `<install>/.git/dg-applied.patch`, `--revert`, `--check` simulates revert+apply on file copies, `dg/` + `tests/` excluded; `dg/tests/test_reapply.sh` (9 scenarios, 21 checks) |
| W1 speaker pipeline | done | `f619f23fc5` | `tools/tts_tool_speaker.py` |
| W1b killed player = stop (sweep-4 blocker) | done | `6879c83e64` | `tools/voice_mode.py` `_run_system_player`: rc < 0 returns True, no ffplay replay |
| W2 drain loop | done | `ea6cc1edb0` | `_chat_drain_tts` in `cli_chat_turn_mixin.py`; also committed `dg/plans/sweep-4-stop-key.md` |
| W3 record key + cut helper + listener guards + D1/D3 | done | `8c990d54cb` | logic in `cli_voice_mixin.py`; `cli_tui_mixin.py` net -53 lines; en.yaml hint + docs line |
| W4 Ctrl+C / Ctrl+Q | done | `3568e293ae` | +4 lines in `cli_tui_mixin.py`, `_voice_ctrl_key_cut` in voice mixin |
| W3b Escape stop key | done | `6bd65b86b3` | one binding after line ~1884 of `cli_tui_mixin.py`; `_voice_stop_key_active` / `_voice_stop_key_pressed` |
| W5 `voice.input_device` | **not started** | — | design notes in §9 |
| W6 tests | done for W0–W4 + W3b (every commit carries its tests); W5 tests not written; **full must-stay-green run not yet done on HEAD** | — | |
| W7 hand-off | **not started** | — | README "Patches" lines, `reapply.sh --check`, `build-report.md` |

## 3. Uncommitted files

`git status --short` is empty. Nothing half-done is in the tree.
Untracked-but-ignored: `.venv/` (test env, git-ignored).

## 4. Test environment

- Interpreter used to bootstrap: `/usr/local/bin/python3.14` (3.14.2; the built env is 3.14.9). Nothing installed into it.
- Disposable dirs: `HERMES_HOME=$TMPDIR/hermes-test-home`, `HERMES_RUNTIME_DIR=$TMPDIR/hermes-test-runtime`
  (`$TMPDIR` = `/var/folders/61/49v5p_7j099glflsbb8rj4yh0000gn/T/`).
- Build (from the fork root; `.venv` must not exist first):
  ```
  export HERMES_HOME=$TMPDIR/hermes-test-home HERMES_RUNTIME_DIR=$TMPDIR/hermes-test-runtime
  /usr/local/bin/python3.14 -m pm.build_env --source . --out .venv --group dev --group test --extra audio-io --extra sms
  ```
  `--extra audio-io` is needed (numpy + sounddevice; without it most of `test_tts_streaming.py` silently skips);
  `--extra sms` brings aiohttp (7 `tests/tui_gateway/test_hosted_room_*` files otherwise fail to import).
- Run tests:
  ```
  export HERMES_HOME=$TMPDIR/hermes-test-home HERMES_RUNTIME_DIR=$TMPDIR/hermes-test-runtime \
         HERMES_PYTHON="/Users/dganesh/!MY PROJECTS/hermes-agent-dg/.venv/bin/python"
  scripts/run_tests.sh <paths>          # add HERMES_TEST_FILE_RETRIES=0 for red runs
  ```
  Helper scripts (scratch, not committed): `$TMPDIR/dgenv.sh` (exports the above), `$TMPDIR/green.sh <log>` (runs the
  12-path must-stay-green list from plan W6.5). Lint: `.venv/bin/ruff check <files>`.
- Do NOT run `scripts/run_tests.sh tests/hermes_cli/ -k ...` over the whole directory: it took > 7 min and a test there
  left a `fixture/` HERMES_HOME directory in the repo root (deleted).

## 5. Test results

All red/green tables are in `dg/plans/test-log.md` (one section per workstream). Summary:

| WS | New test file(s) | red on base | green |
|---|---|---|---|
| W0 | `dg/tests/test_reapply.sh` | old script fails ("tests shipped") | 21/21 |
| W1 | `tests/tools/test_tts_speaker_backpressure.py` | 0/5 | 5/5 |
| W1b | `tests/tools/test_voice_player_signal_stop.py` | 1/4 (rc=1 guard) | 4/4 |
| W2 | `tests/hermes_cli/test_chat_turn_tts_drain.py` | 1/6 (guard) | 6/6 |
| W3 | `tests/hermes_cli/test_cli_voice_record_key.py`, `tests/tools/test_voice_listener_exit_log.py` | 2/21 (guards) | 21/21 |
| W4 | `tests/hermes_cli/test_cli_voice_ctrl_keys.py` | 4/11 (guards) | 11/11 |
| W3b | `tests/hermes_cli/test_cli_voice_stop_key.py` | 1/10 at base | 10/10 |

Baseline of the must-stay-green list on the base code (rebuilt env): 3426 passed, **8 failed** —
`test_voice_mode.py::TestPlayBeep::test_beep_calls_sounddevice_play`, `test_served_profile_child_env.py::test_helper_children_resolve_secrets_through_the_served_profile`,
and 6 in `test_tui_gateway_server.py` (agent-build / before-agent-ready tests). All pre-existing; causes not investigated.
Subsets of the green list were re-run after each workstream (no failures outside the baseline), but the **full**
12-path list (incl. all of `tests/tui_gateway/`) has not been re-run on HEAD yet.

## 6. Deviations from plan-v2 (for build-report.md)

1. **W1 PortAudio put:** plan said "keep the timed put". Kept the 30 s deadline, but the put polls the stop event
   (0.2 s) — otherwise `finish()` waited 10 s per stranded thread after a stop. The sentinel put never blocks after a
   stop (plan item met).
2. **W1 PortAudio test** is not `platforms("linux")`: it forces the route by patching `_StreamerPlayback._device_usable`
   (the "sounddevice output usable" seam) with a mocked sounddevice, so it runs (and was shown red/green) on macOS.
   No OS is faked.
3. **PR #76655's test not adopted**: it fakes `platform.system` and asserts `done` within 1 s on a stalled provider,
   which needs consumer-side stop-aware gets (out of W1's minimal scope); W2's drain loop bounds that case on the CLI
   side (5 s grace). Our capacity test adds the deterministic 4th-acquire sync point the PR's sweeper asked for.
4. **W1b** added (sweep-4 blocker) as its own commit because W1 was already pushed.
5. **D3 exit line** lives in `tools/voice_mode.full_duplex_listen` (the detector owns floor/trigger/peak), not in
   `cli_voice_mixin.py`. It therefore also logs for the gateway listener.
6. **W3.7 guard** is broader than lines 209-216/234-235: the idle tick (`_tui_idle_tick`) also skips the notification
   drain, loop tick and parked-goal resume while a take records/transcribes (otherwise the idle tick started the
   notification turn 0.1 s later). The after-turn auto-restart is also skipped while transcribing (the take's own turn
   restarts it).
7. **W3 listener discard** also deletes the WAV a discarded trip captured.
8. **W3b filter** is `_voice_stop_key_active` = cuttable AND not within 1.5 s of a re-feed, instead of plain
   `_voice_tts_cuttable`: the killed player keeps `is_audio_output_active()` true for a few ms, so a re-fed Escape would
   re-enter the handler and loop (covered by a test). `_voice_tts_cuttable` itself was added in W3 (shared by the record
   key, Ctrl+C/Q and Escape).
9. **Red runs for W4/W3b** were done against both the previous commit and the base copies of the changed files (W4 and
   W3b build on W3's helpers).
10. **D1 locale:** only `locales/en.yaml` `hint_continuous` value changed; other locales still say "exit continuous"
    (no key change, i18n test green).
11. **Ink/desktop and gateway listener:** unchanged except shared `tools/` code (W1, W1b, D3 line).

## 7. PR #76655 finding

It is **OPEN, not closed** (review-v1 was wrong). Unmerged since 2026-08-02, author Drexuxux, labels type/bug, tool/tts,
P2, sweeper:risk-compatibility. The hermes-sweeper verdict is `keep_open, salvageability=high`; its one objection is
that the test does not prove the 4th semaphore acquire was reached before `stop.set()`. Two community verifications
call it mergeable with CI green. It targets the pre-split `tools/tts_tool.py`, so it no longer applies to v0.23.1
(`tools/tts_tool_speaker.py`). Intent check: the 64-chunk bound + `put(timeout=30)` arrived with `026707a516`
("per-sentence prefetch pipeline with PortAudio resilience", salvage of #71084), designed for a live-draining PortAudio
consumer; the temp-file consumer was not considered.

## 8. Blockers and open questions

- **`dg/reapply.sh --check` cannot be run from inside Hermes:** the Hermes terminal guard blocks any command whose
  referenced script contains the text `hermes gateway restart` (the script's final `echo`). Options: run it from a
  separate shell, or reword that echo to e.g. "restart the gateway (see dg/README.md)" in a small `dg/` commit and
  re-run `bash dg/tests/test_reapply.sh` + `dg/reapply.sh --check`. Do not work around the guard otherwise.
- Full must-stay-green run on HEAD pending (see §5).
- In-process agent stale-interrupt hazard: the deferred interrupt (W3) calls `agent.interrupt()` after re-checking
  the thread is alive; if the turn ends between the check and the call, an in-process `AIAgent` may keep a stale flag.
  Same hazard exists in the upstream listener path; the gateway agent resets per turn (review M10). Not fixed.
- `test_beep_calls_sounddevice_play` fails on base on this host; cause not investigated.

## 9. Exact next actions

1. `cd '/Users/dganesh/!MY PROJECTS/hermes-agent-dg'`; `source $TMPDIR/dgenv.sh` (or export the values in §4). Check
   `git status` is clean and HEAD is `6bd65b86b3`.
2. **W5 `voice.input_device`** (one commit, with tests):
   a. `hermes_cli/config_defaults.py` voice block (~line 1198-1232): add `"input_device": None`. Check
      `hermes_cli/config_value_checks.py` for a voice/wake_word `input_device` validator pattern; add one only if
      that is the pattern.
   b. `tools/voice_mode.py`: copy the parser from `tools/wake_word.py:128-131` as `_voice_input_device()` reading
      `_voice_config()` (already imported from `tools.voice_mode_transcript`); return None on Termux. Do not import
      `wake_word`.
   c. Add `VoiceInputDeviceError(RuntimeError)` and a resolver that calls `sd.query_devices(device, "input")` and turns
      ValueError (missing / "Multiple input devices") into a clear message naming `voice.input_device`.
   d. Pass `device=` only when pinned (keep `null` calls byte-identical: no `device` kwarg) at
      `AudioRecorder._ensure_stream` (`sd.InputStream`, ~line 812), `listen_for_speech` (~1197) and
      `full_duplex_listen` (~1450). For the recorder with a pin, read the rate from the pinned device when the stream
      is built; leave the `null` path's `_default_input_samplerate(sd)` call in `start()` unchanged. Log the pinned
      device name at info ("Voice recording started …" gets the device when pinned).
   e. Listener visibility: let `full_duplex_listen` raise `VoiceInputDeviceError` (resolve before its try), and in
      `CLIVoiceMixin._voice_full_duplex_listener`'s `except` print one dim line per session (class-level flag) for that
      error. The gateway listener just logs (debug) — note it.
   f. Tests in a new file (e.g. `tests/tools/test_voice_input_device.py`): null → no `device` kwarg at all three sites;
      name and index reach all three sites and `query_devices`; missing / ambiguous → clear error for the recorder; the
      CLI listener prints once per session. Show red on base, green after; append to `dg/plans/test-log.md`.
3. Run the full green list: `bash $TMPDIR/green.sh $TMPDIR/green-head.log` (~7 min; run it as a background process
   and poll). Compare with the 8-test baseline in test-log; record the result.
4. **W7:** update `dg/README.md` "Patches" with one line per commit in §1 (plus W5); push.
5. Resolve the `--check` blocker in §8 (reword the echo in a `dg/` commit, re-run `bash dg/tests/test_reapply.sh`),
   then run `dg/reapply.sh --check` only. Never apply, never restart Hermes or the gateway.
6. Write `dg/plans/build-report.md`: commits, red/green table (from test-log), deviations (§6), PR #76655 finding (§7),
   the `--check` output, and DG's live checklist:
   - Ctrl+Space during a long reply cuts within 300 ms, and "continuous" stays on.
   - Ctrl+Space in a pause between sentences also cuts.
   - Speaking over a reply cuts it.
   - A 4+ sentence reply plays fully (no "TTS CUT … prefetch failed").
   - Ctrl+C during playback stops audio and the app stays open.
   - A typed message during playback cuts audio.
   - Escape stops a long reply with no replay of the sentence (no ffplay fall-through).
   - Escape during "Working…" means nothing is spoken for that turn.
   - Alt+Enter during playback still inserts a newline.
   - With `voice.input_device` set to `USB Microphone`, recordings log the USB device.
   - After apply: restart both the TUI and the gateway (`tools/` runs in both).
7. Commit + push the report; final answer to the parent.
