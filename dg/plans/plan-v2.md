# Plan V2: voice barge-in, record key, streaming TTS and input device fixes

Supersedes `plan-v1.md`. Built from `sweep-1/2/3` and `review-v1.md`, all in this folder; read them for evidence. Line numbers are for base `3b42157858` (v0.23.1). Re-check every line before editing.

Repo: fork clone `~/!MY PROJECTS/hermes-agent-dg`, branch `dg/local-patches`, remote `origin` = `wakeupdg/hermes-agent`, `upstream` = NousResearch. Install: `~/.hermes/hermes-agent` (detached at `v0.23.1`, managed by `hermes update`).

## Goals (acceptance)
1. The record key (Ctrl+Space) always cuts audible TTS and starts listening, at any point in a turn. Continuous mode stays on.
2. Speaking over a reply cuts it (mic barge-in stays armed across key presses).
3. Long multi-sentence replies play fully. No `TTS CUT … prefetch failed` warning.
4. Ctrl+C / Ctrl+Q stop audio without exiting the app or poisoning the next turn.
5. A typed message during playback cuts the audio (documented behaviour that is broken today).
6. The mic can be pinned to "USB Microphone" (`voice.input_device`).
7. All existing suites stay green. Every fix has a new test shown red on the base commit and green after.

## Root causes (all confirmed in review except R6)
- R1/R2: `_agent_running` spans the streaming-TTS join. The key's busy branch disarms continuous mode and returns before the cut (`cli_tui_mixin.py:836-858`, `cli_chat_turn_mixin.py:501-507`). A manual utterance stop also disarms (`:830`).
- R3/R4: bounded 64-chunk queue + `put(timeout=30)` truncates long sentences. A stop strands prefetch threads (`tts_tool_speaker.py:166, 232-260`).
- R5: Ctrl+C/Q never cut playback (`cli_tui_mixin.py:926-968`).
- R6 (unproven): mic barge-in floor of 1500 RMS (`voice_mode.py:1308, 1383`).
- R7: no input-device pin (`voice_mode.py:812, 1197, 1439`).
- Background review is NOT a cause (S2). Leave it on.

## Decisions (final)
- D1 Ending your own utterance with the key keeps continuous armed when the recorder supports silence auto-stop (`supports_silence_autostop`; Termux keeps today's disarm). Change the hint value at `locales/en.yaml:2300` and the doc line at `website/docs/user-guide/features/voice-mode.md:154`. No new locale keys.
- D2 A mid-turn key press never interrupts on the press. It cuts TTS, keeps continuous on, and starts recording. With a modal prompt open, it cuts only. While STT is running, it is ignored. The turn is interrupted only once a non-empty transcript exists and the agent thread is still alive (re-checked right before the call, run off the UI thread). The escape-hatch disarm is removed. The remaining exits are Ctrl+C while recording, the stop phrase, `/voice off`, and three silent takes.
- D3 No trigger change now. Add one info log line on listener exit (floor, trigger, peak RMS during playback) so it can be tuned from `agent.log` later.

## Build order (one commit per step, tests in the same commit, push to origin after each)

### W0 — Tooling first (`dg/` only)
1. Commit `dg/plans/` (it is under `dg/`, so it is already excluded from the shipped diff). The fork must be clean for the script.
2. Rewrite `dg/reapply.sh` to be idempotent:
   - Save the patch it applied to `$INSTALL/.git/dg-applied.patch`. On every run, reverse-apply that saved patch first (before any rebase), then apply the new diff and overwrite the saved file.
   - `--revert` removes our patch from the install.
   - `--check` changes nothing.
   - Exclude `dg/` and `tests/` from the shipped diff.
   - Detect a stash-restored old patch (the reverse-apply of the saved patch succeeds or is already-applied) and handle it.
3. `dg/README.md`: before `hermes update`, run `dg/reapply.sh --revert` (or answer n at "Keep these changes"). After the update, run `dg/reapply.sh`. Restart both the TUI and the gateway (`hermes gateway restart` or the documented equivalent; verify the command) because `tools/voice_mode.py` runs in both processes.
4. Test the script against a scratch fork/install pair in `$TMPDIR`: first apply, second apply with a changed patch, simulated stash-restore, `--revert`. Do not touch the real install.

### W1 — Pipeline: no truncation, clean stop (`tools/tts_tool_speaker.py`)
First read upstream PR #76655 (`gh pr view 76655 -R NousResearch/hermes-agent --comments`) and record why it was closed. Also run `git log -p -S "timeout=30.0" -- tools/tts_tool_speaker.py`, using a full fetch of that path if the partial clone times out. Adopt the PR's test if it fits.
1. Make the per-sentence queue unbounded when the consumer is the temp-file path (`not self._use_device`, `:233`). Keep the bound on the PortAudio path. Memory is already capped at 16 MiB per sentence (`tts_streaming.py:29, 218-226`).
2. Stop-aware semaphore acquire in `speak()` (`:232`): loop `acquire(timeout=0.2)` and check the stop event.
3. Re-check the stop event between WAV write and `play_audio_file` (`:56-62`).
4. Logging: log `type(exc).__name__` with sentence index, text length and bytes queued (`:247`). On the PortAudio path, keep the timed put but make the sentinel put non-blocking after a stop.

### W2 — Turn drain loop (`hermes_cli/cli_chat_turn_mixin.py`) — goals 3 and 5
Replace `tts_thread.join(timeout=120)` (`:504`) with a poll: `while tts_thread.is_alive(): tts_thread.join(0.2)`. Leave the loop when:
- the thread is dead;
- `_should_exit`;
- the stop event has been set for more than 5 s (grace);
- a stall watchdog fires: pipeline pending and no audio output for 45 s → warn, set stop, leave;
- `_interrupt_queue` is non-empty → set the stop event, call `stop_playback()`, keep polling until the thread is dead or the grace expires (goal 5).

Rules:
- Compute `tts_normal_exit` from the thread result, never from `_voice_tts_done`.
- Do not change the 60 s batch wait (`cli_tui_runtime_mixin.py:223`) or `finish()`'s 300 s join.
- Keep the bound after stop.

### W3 — Record key + cut helper (`hermes_cli/cli_voice_mixin.py`, minimal hunks in `cli_tui_mixin.py`) — goals 1, 2
Put the new logic in `cli_voice_mixin.py`. The net change to `cli_tui_mixin.py` should be about zero lines (size ratchet, rebase-friendly).
1. Store the agent thread on the turn (`self._turn_agent_thread`); "generating" = that thread `is_alive()`. Class-level default `None` on the mixin.
2. `_voice_cut_tts(reason)` in `cli_voice_mixin.py`:
   - snapshot `getattr(self, "_voice_tts_stop", None)` into a local and set it;
   - `mark_speech_interrupted()`, `stop_playback()`, set `_voice_tts_done`;
   - log at info with the state (`agent_running`, generating, audible, continuous).
3. The batch path stores its own event in `_voice_tts_stop` (so every cutter works unchanged) and checks it before each chunk file (`cli_voice_mixin.py:338-340`). Reset it with an identity guard (`if self._voice_tts_stop is turn.stop_event`). Skip the batch speak call if `_voice_recording` is already set.
4. `_tui_handle_voice_record` (`cli_tui_mixin.py:817`) delegates to a new `_voice_record_key_pressed(event)` in `cli_voice_mixin.py`:
   - not voice mode → return;
   - recording → stop + transcribe; continuous stays on per D1 when `supports_silence_autostop`, else disarm as today;
   - `_voice_processing` → ignore;
   - if anything is pending or audible (`not _voice_tts_done.is_set()`, `is_audio_output_active()`, or an active `_voice_tts_stop` not set) → `_voice_cut_tts("record_key")`;
   - modal prompt open → return (cut only);
   - full-duplex listener already capturing (`_voice_barge_capture`) → return;
   - otherwise arm continuous (under `_voice_lock`) and start recording on a daemon thread. This now also applies mid-turn.
5. Deferred interrupt (D2): when a manual recording that started mid-turn produces a non-empty transcript, and the agent thread is still alive, and no modal is open, call `agent.interrupt()` on a daemon thread (the `GatewayAgent` RPC can block 5 s). The transcript still goes through `_pending_input` (S1: `_VoiceInputMessage` must not enter `_interrupt_queue`).
6. Listener guards (`cli_voice_mixin.py:385-433`):
   - `_should_stop` returns True once `_voice_recording` is set.
   - `_on_trigger` discards a trip when `_voice_recording` is set.
   - Add the D3 exit info line.
7. Goal-continuation / notification turns must not start while `_voice_recording` is set (`cli_tui_runtime_mixin.py:209-216, 234-235`). Use the smallest guard that works.
8. New state uses class-level defaults and `getattr`, so the `HermesCLI.__new__` test stubs keep working.

### W4 — Ctrl+C / Ctrl+Q (`cli_tui_mixin.py:926-968`, helper in `cli_voice_mixin.py`) — goal 4
Gate the new branch on `getattr(self, "_voice_mode", False)`.

Order in `_tui_handle_ctrl_c`:
1. Cancel recording (unchanged).
2. Cancel foreground UI (unchanged).
3. **New:** if TTS is pending or audible:
   - agent not generating → `_voice_cut_tts("ctrl_c")` and return (no hard interrupt, no 2 s force-exit arming, no exit on an empty buffer);
   - generating → cut, then continue to the normal path.
4. Draft clear / overlays / interrupt / exit (unchanged).

Same for Ctrl+Q. Respect the override at `hermes_cli/cli_backtrack.py:305-309`. The trace result is already known (review M10): the gateway agent resets its interrupt state per turn, so no next-turn fix is needed.

### W5 — `voice.input_device` (`hermes_cli/config_defaults.py`, `tools/voice_mode.py`) — goal 6
1. Add `voice.input_device: null` beside the other voice defaults. Add validation in `hermes_cli/config_value_checks.py` if that is the repo pattern.
2. Copy the parser from `tools/wake_word.py:128-131` locally; do not import `wake_word`.
3. Pass `device=` at `voice_mode.py:812, 1197, 1439`, and read the sample rate from the pinned device when the stream is built (`:128-136, 849`).
4. `null` must keep today's calls byte-identical.
5. A missing or ambiguous device raises a clear error for the recorder. The listener prints one visible dim line per session (not just a debug log).
6. Termux ignores it.

### W6 — Tests (procedure is mandatory)
1. Setup in the fork:
   - export a disposable `HERMES_HOME=$TMPDIR/hermes-test-home` and `HERMES_RUNTIME_DIR` (see `AGENTS.md:63-65`, `scripts/run_tests.sh` header);
   - build the env with `python -m pm.build_env --source . --out .venv --group dev --group test` (`.venv` is git-ignored);
   - run with `scripts/run_tests.sh <paths>`.
   - Never install anything into the Hermes runtime venv, and never use `~/.hermes`.
2. Rules (from `tests/AGENTS.md`, `CONTRIBUTING.md`):
   - new tests go in new files;
   - no non-zero `time.sleep` (use Events / `tests.waiting.wait_until`);
   - wall-clock bounds of at least 2 s;
   - macOS behaviour uses `@pytest.mark.platforms("macos")`;
   - no source-reading tests;
   - pipeline tests patch `tools.voice_mode.play_audio_file` themselves.
3. Red-on-base: for each workstream, show the new tests fail on `3b42157858` (stash the fix) and pass after. Record the commands and results in `dg/plans/test-log.md`.
4. New cases:
   - S1 §f 1-10 (adapted to D2: no escape hatch);
   - S3 §7 1-7 and 9;
   - review §4 1-15, including deferred-interrupt alive/dead, modal open, gap-between-sentences press, typed-message cut, drain-loop exits, `_voice_tts_done` set by another writer, Ctrl+C/Q batch-idle no exit, input-device propagation and errors, the Linux PortAudio stop path, and `dg/reapply.sh` scenarios (as a shell test or documented manual run).
5. Must stay green, in full:
   - `tests/tools/test_voice_cli_integration.py`, `test_tts_streaming.py`, `test_voice_mode.py`, `test_tts_macos_output.py`;
   - `tests/hermes_cli/test_chat_turn_streaming_tts_arming.py`, `test_cli_ctrl_c_draft_priority.py`, `test_cli_status_bar.py`, `test_cli_backtrack_browse.py`;
   - `tests/gateway/test_cli_plugin_command_own_socket.py`, `test_cli_plugin_command_in_gateway.py`;
   - `tests/tui_gateway/` (all of it);
   - `tests/agent/test_i18n.py`.

### W7 — Hand-off (builder stops here)
1. Update `dg/README.md` "Patches" with one line per commit, and push the branch.
2. Run `dg/reapply.sh --check` only. **Do not apply to the install, restart Hermes, or restart the gateway.** The parent agent applies after DG approves, because applying while Hermes runs mixes code versions in the live gateway.
3. Write `dg/plans/build-report.md`:
   - commits;
   - test results (red/green table);
   - deviations from this plan and why;
   - the PR #76655 finding;
   - DG's live checklist.

DG's live checklist (the parent runs it with DG after apply + restart of TUI and gateway):
- Ctrl+Space during a long reply cuts within 300 ms, and "continuous" stays on.
- Ctrl+Space in a pause between sentences also cuts.
- Speaking over a reply cuts it.
- A 4+ sentence reply plays fully.
- Ctrl+C during playback stops audio and the app stays open.
- A typed message during playback cuts audio.
- With `voice.input_device` set to `USB Microphone`, recordings log the USB device.

## Out of scope (stated)
- Wake word is deaf during the playback tail (`cli_voice_mixin.py:670`).
- "Working…" stays up during the drain (`cli_modern_layout.py:237`). The drain is shorter now but still shown.
- Lowering the barge-in trigger (D3). The player-set registry and stop epoch (review M6). The any-length tail flush (M5). Extra locking at single writers (minor 1). Kokoro server streaming.
- `tui_gateway/methods_voice.py` behaviour: it does not share R1/R2. It does get the W1 and W5 changes (shared `tools/` code), so `tests/tui_gateway/` must pass and the gateway needs a restart.
- Upstream PRs: W1 and W4 are candidates after review. W3/W5 would need the size-ratchet layout and upstream discussion. Not part of this build.
