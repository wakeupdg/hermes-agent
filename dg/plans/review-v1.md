# Review V1 — adversarial review of `plan-v1.md`

Reviewer scope: read-only. No code, config, process or audio setting was changed. The only file written is this one.

**Paths.** Every bare path is in repo `hermes-agent-dg` (fork clone `/Users/dganesh/!MY PROJECTS/hermes-agent-dg`, branch `dg/local-patches`, base `3b42157858` = v0.23.1). The install `~/.hermes/hermes-agent` is the same commit, detached at tag `v0.23.1`, a partial clone. `kokoro:` means `~/.hermes/plugins/kokoro-tts/`.

**Labels** (all defined in `dg/plans/plan-v1.md`): R1-R7 are the plan's root causes, W1-W7 its workstreams, D1-D3 its open decisions. S1/S2/S3 are the three sweep reports in `dg/plans/`.

**What I checked and how.**

| Claim type | Method |
|---|---|
| Every `file:line` in the plan | Read the code at that line |
| `dg/reapply.sh` behaviour | Ran `dg/reapply.sh --check` on the real fork (dry run), and ran the real script against a throwaway fork/install pair in the scratch dir |
| `hermes update` on a patched install | Read `hermes_cli/update_cmd_stash.py`, `update_cmd.py` |
| How the install loads code | Read the launcher, the venv `.pth` files and a `.pyc` header |
| Upstream state | `git ls-remote upstream`, raw files from `main`, one web search |

**Not done.** No test was run: there is no pytest in the runtime and I did not build a test environment. `git log -S` for intent timed out on the partial clone. Two upstream files (`tools/voice_mode.py`, `hermes_cli/cli_chat_turn_mixin.py`) could not be compared with `main` (HTTP 429).

## 1. Root causes: verdict per row

| # | Verdict | Note |
|---|---|---|
| R1 | Confirmed | `hermes_cli/cli_tui_mixin.py:836-840` returns before `:847-858`; `_agent_running` spans `chat()` (`hermes_cli/cli_tui_runtime_mixin.py:159-166, 187`); join at `hermes_cli/cli_chat_turn_mixin.py:504` |
| R2 | Confirmed | `cli_tui_mixin.py:830`; listener gate `hermes_cli/cli_voice_mixin.py:386-387`; arming gate `cli_chat_turn_mixin.py:233` |
| R3 | Confirmed by code | `tools/tts_tool_speaker.py:166, 245`; 64 × 8192 B at 48,000 B/s = 10.92 s (`kokoro:kokoro_provider.py:26`) |
| R4 | Confirmed by code | untimed sentinel `:249`, semaphore `:232`, skip without drain `:258-260`, 10 s joins `:326-327` |
| R5 | Confirmed | `cli_tui_mixin.py:926-968` never calls `stop_playback` (plan cites `926-966`; the handler ends at 968) |
| R6 | Unproven, as the plan says | Floor at `tools/voice_mode.py:1308, 1383-1385`. R2 alone already explains a dead listener |
| R7 | Confirmed | No `device=` at `tools/voice_mode.py:812, 1197, 1439`; rate from the default device `:128-136, 849` |

All other line references in the plan are correct. One fact the plan's `speaking` test depends on and no sweep states: on macOS the thinking sound exits at once (`tools/voice_mode.py:471-472`), so it never marks audio active here. Record beeps do (`:76-87` → `play_audio_file` `:1012-1020`), so a beep counts as "audible".

## 2. Findings

### BLOCKER

**B1. The W7 ship path fails as written (two parts reproduced).**
- `dg/reapply.sh:21` refuses a dirty fork. `dg/plans/` is untracked, so today `dg/reapply.sh --check` prints "Fork has uncommitted changes" and exits 1. Reproduced on the real fork.
- The script cannot update an install that already carries an earlier version of the patch. `dg/reapply.sh:58-63`: the reverse check fails (the new hunks are absent), then the forward check fails (the old hunks are present), exit 3. Reproduced in a scratch fork/install pair: commit 1 applied, commit 2 → "patch does not apply … Nothing changed", exit 3. "One commit per workstream" plus any fix after live testing hits this on the second run.
- After `hermes update` the same failure is likely. The updater asks "Keep these changes after updating? [Y/n]", default yes (`hermes_cli/update_cmd_stash.py:179-194`), stashes with `--include-untracked` (`:267`) and re-applies the stash after the pull (`hermes_cli/update_cmd.py:1158`). The install then holds the old-base patch; the rebased diff neither reverse-applies nor applies. Code read only, not run.
- New test files (W6) land in the install as untracked files. A later `git apply --check` fails on "already exists".

Correction:
1. Add `dg/plans/` to `.git/info/exclude`, or commit the plans.
2. Make the script idempotent: save the applied patch (for example `$INSTALL/.git/dg-applied.patch`); on every run reverse-apply that file first, then apply the new one. Compute and revert before the rebase step, because the rebase destroys the old diff.
3. Add `':(exclude)tests'` to `DIFF_PATHS` (`dg/reapply.sh:18`). The install does not need tests.
4. `dg/README.md`: before `hermes update`, run a new `dg/reapply.sh --revert`, or answer **n** at the "Keep these changes" prompt.

**B2. The plan as specified breaks suites that W6 says must pass.**
- W1.5 turns `_active_playback` into a set. `tests/tools/test_voice_mode.py:889-897` assigns a process to it and asserts `is None` after `stop_playback()`; `:1480` and `:1514` patch it to `None`.
- W2/W3 add `_turn_generating` and `_voice_tts_cancel`. Existing tests build the CLI with `HermesCLI.__new__` and a fixed attribute list (`tests/tools/test_voice_cli_integration.py:15-44`, `tests/hermes_cli/test_chat_turn_streaming_tts_arming.py:34-44`). The Ctrl+C stub has no voice attributes at all (`tests/hermes_cli/test_cli_ctrl_c_draft_priority.py:17-23`). A direct `self._voice_tts_cancel` or `self._turn_generating` read raises `AttributeError` in those tests.
- W6 omits three more callers of `_tui_handle_ctrl_c`: `tests/hermes_cli/test_cli_backtrack_browse.py:163`, `tests/gateway/test_cli_plugin_command_own_socket.py:58`, `tests/gateway/test_cli_plugin_command_in_gateway.py:499`. There is also an override at `hermes_cli/cli_backtrack.py:305-309`.

Correction: keep `_active_playback` as a compatible single slot and add a separate registry, or defer W1.5 (see M6). Give new state a class-level default on the mixin (`_turn_generating = False`) and read events with `getattr(self, name, None)`, as `cli_voice_mixin.py:360-363, 383` already does. Gate W3 on `getattr(self, "_voice_mode", False)` first. Add the three files to W6.

### MAJOR

**M1. The `speaking` test reopens R1 during generation, and interrupt-on-press is unsafe.**
`speaking = is_audio_output_active() or (tts pending and not _turn_generating)`. While the model is still generating, this reduces to "a player process is running this instant". That flag is true only inside `play_audio_file` (`tools/voice_mode.py:1012-1020`) and drops between sentences; the detector code documents the flapping (`:1366-1367`). A key press in a gap (for example after a short first sentence, while sentence 2 is still being generated and synthesised) is classified "generating, nothing audible" → escape hatch: no cut, continuous off, listener dead. That is the R1 symptom again, intermittently. Inferred from code, not reproduced.

The "speaking and generating → `agent.interrupt()`" branch has three further problems:
- A modal prompt may be open (approval while a sentence is spoken). The typed-interrupt path clears overlays after interrupting (`cli_chat_turn_mixin.py:451-454`); the plan's direct call does not, which is the frozen-prompt class `cli_tui_mixin.py:901-907` warns about.
- It kills a long tool run on one press with no undo. S1 §(c) rejected exactly that for silent generation.
- If generation ends between the check and the RPC, the gateway still purges queued prompts on an idle session (`tui_gateway/session_lifecycle.py:793-797`).

Correction (also settles D2): the key never interrupts on press. Mid-turn press = cut TTS + keep continuous + start recording, unless a modal is open (then cut only) or STT is running (ignore). Interrupt the turn only when a non-empty transcript exists and the agent thread is still alive, re-checked immediately before the call. If the escape hatch is kept instead, add hysteresis: treat "audio active within the last ~3 s" or "sentences queued and unplayed" as speaking.

**M2. W2.7 replaces bounded waits with unbounded ones, on the wrong signal.**
- `chat()` runs on the input-processing thread (`cli_tui_runtime_mixin.py:164`). While it waits, `_agent_running` is true and no input is processed.
- Blockers that ignore the stop event exist: `_SyncSentencePipeline.close()` (`tools/tts_tool_speaker.py:120-124`), a PortAudio `write` (`:263, 297`), a Kokoro request up to 120 s (`kokoro:kokoro_provider.py:25`), `afplay` up to 300 s per sentence (`tools/voice_mode.py:1108`).
- `_voice_tts_done` is shared and set by other writers (`cli_voice_mixin.py:348, 590`, and the plan's own cut helper). A wait on it can return while the pipeline thread is alive. Then `tts_normal_exit` is false (`cli_chat_turn_mixin.py:507`) and the release path cuts audio (`:153-155`).
- The 60 s wait (`cli_tui_runtime_mixin.py:223`) only matters for batch TTS; with streaming TTS the event is already set when `chat()` returns.
- `finish()` is shared with the gateway speak path, which blocks on it synchronously (`hermes_cli/voice.py:563-588`).

Correction: poll the thread, not the event: `while tts_thread.is_alive(): tts_thread.join(0.2)`, leaving on `_should_exit`, on stop set plus a 5 s grace, and on a stall watchdog (no audio output for N seconds while pending → warn, set stop, leave). Keep a bound after stop in `finish()`. Drop the 60 s change from this patch.

**M3. Gap: a typed message during the playback tail does not cut audio.**
The monitor loop that reads `_interrupt_queue` ends when the agent thread ends (`cli_chat_turn_mixin.py:428-484`); the queue is drained only after `chat()` returns (`cli_tui_runtime_mixin.py:206`). The docs promise that sending a message stops playback (`website/docs/user-guide/features/voice-mode.md:268`). S2's 00:07:58 timeline fits this (not independently verified). Correction: in the M2 wait loop, a non-empty `_interrupt_queue` sets the stop event and calls `stop_playback()`.

**M4. W1 is larger than the bug and is inconsistent with itself.**
- With a stop-aware blocking put, `queue.Full` cannot reach the `except`. W1.4's "catch `queue.Full`, log seconds waited" and S3 test 8 become dead code.
- "Drain skipped queues" is redundant once puts are stop-aware. A blocking drain (`iter(q.get, None)`) would wait on a producer stuck in a network read.
- Closing `audio_iter` does little: `_capped` iterates with a `for` loop and does not close its inner iterator (`tools/tts_streaming.py:218-226`).
- Smallest fix for R3 and R4 on macOS: make the per-sentence queue unbounded when `not self._use_device` (`tools/tts_tool_speaker.py:233`), plus a stop-aware semaphore acquire (`:232`) and the stop re-check before play (`:61-62`). Puts then never block, so the sentinel is never stranded. Memory is already capped at 16 MiB per sentence (`tools/tts_streaming.py:29, 218-226`).
- Prior art: upstream PR #76655, "prevent stalled streaming prefetch from wedging voice barge-in", makes the same stop-aware changes and has a regression test (`test_hybrid_interrupt_unblocks_stalled_prefetch_pipeline`). It appears closed without merge (web page read only; the code at this base still has the 30 s put). Find out why before building W1 or calling it an upstream candidate. `AGENTS.md:48-50` also requires the intent check (`git log -p -S "timeout=30.0"`), which I could not complete.

**M5. W1.6 (flush any tail after ~1 s idle) is a behaviour regression risk and is not needed for the five goals.**
Today only buffers over 100 characters flush on idle (`tools/tts_tool_speaker.py:384`). Any-length flushing splits a sentence, or a word, whenever the token stream pauses for a second. Correction: move it to a separate later patch.

**M6. W1.5 does not close the race it names, and has low value in DG's flow.**
The real gap is between `Popen` and registration (`tools/voice_mode.py:1105-1107`): a stop that lands there finds no process and the new `afplay` plays the whole sentence. "Registered under the lock" does not fix that unless `Popen` also runs under the lock. Beep-over-TTS overlap is rare once the key cuts TTS before the start beep. Correction: defer; or add a stop epoch (`stop_playback()` increments a counter; the player compares it after registering and terminates itself on change).

**M7. "Only W1 touches shared code" is false, and the gateway process is not covered by a TUI restart.**
W1.5, W4 and W5 edit `tools/voice_mode.py`, which the gateway voice surface uses (`tui_gateway/methods_voice.py:254-266`, `hermes_cli/voice.py:113, 306, 362`). The gateway is a separate long-lived process (S2). Correction: W7 names a gateway restart, or states that the gateway keeps old code. `tests/tui_gateway/` runs in full.

**M8. D1 and D2 change user-facing text that the plan does not list.**
- D1 contradicts the hint printed at every recording start, `locales/en.yaml:2300` ("{shortcut} to stop & exit continuous"), and `website/docs/user-guide/features/voice-mode.md:154`.
- D2's notice needs a new locale key. `tests/agent/test_i18n.py:41-48` requires every catalog in `locales/` to have exactly the English key set, so one key means an edit in every locale file.

Correction: with the M1 design no notice is needed. If D1 is accepted, change the English hint value and the doc line (a value edit needs no key change).

**M9. W6 has no runnable procedure, and the repo's test rules constrain the proposed tests.**
- Procedure from the repo: `python -m pm.build_env --source . --out .venv --group dev --group test`, then `scripts/run_tests.sh <paths>` (`tests/AGENTS.md`, `CONTRIBUTING.md:190-227`). `.venv` is git-ignored (`.gitignore:14-15`). Choose a disposable `HERMES_HOME`/`HERMES_RUNTIME_DIR` first (`AGENTS.md:63-65`); otherwise provisioning uses DG's live `~/.hermes` tool store (`scripts/run_tests.sh` header).
- Rules that bind the new tests: no new non-zero `time.sleep` in tests (HX013; use Events or `tests.waiting.wait_until`); wall-clock bounds of at least 2 s, so S3's "returns in under 1 s" needs rewording; macOS-only behaviour uses `@pytest.mark.platforms("macos")`, never a faked platform; no source-reading tests; new tests go in new files.
- An autouse fixture stubs playback in `hermes_cli.voice` only (`tests/conftest.py:1377-1414`). Pipeline tests must patch `tools.voice_mode.play_audio_file` themselves, as `tests/tools/test_tts_streaming.py:659, 1098-1099` do.
- Each fix needs a test shown red on the base commit (`AGENTS.md:136-137`). The plan has no such step.

**M10. W3 leaves order and outcome undefined; the "trace later" item can be closed now.**
- Undefined: where the cut sits relative to draft clearing and overlay clearing. With batch TTS and an idle agent, Ctrl+C today reaches `_tui_clear_or_exit` and exits the app on an empty buffer (`cli_tui_mixin.py:950-951`); the cut must come first.
- Trace result: the gateway-backed agent resets its interrupt state at each turn start (`hermes_cli/gateway_agent.py:758-759`) and the RPC carries the turn generation (`:593-598`), so the next turn is not aborted. Side effect: the gateway drops queued prompts (`tui_gateway/session_lifecycle.py:793-797`). For the in-process agent the stale-flag hazard is documented at `cli_chat_turn_mixin.py:676-685`. "Cut and return, no interrupt" is therefore right, and no extra fix is needed.

**M11. W5 is under-specified.**
- "Fails loudly" is not true for the listener: its failure is only logged (`cli_voice_mixin.py:429-431`, `tools/voice_mode.py:1454-1455`). Print one visible line per session.
- `AudioRecorder.start` re-reads the rate on each start (`tools/voice_mode.py:849`) while the stream stays open (`:782-791`). Read the rate from the pinned device and only when the stream is built.
- The pin also applies to the gateway, Ink and desktop surfaces through `create_audio_recorder` (`hermes_cli/voice.py:306, 362`). Termux ignores it. PortAudio lists devices once per process, so a hot-plugged microphone needs a restart; an ambiguous substring raises.
- `tools/wake_word.py` imports `pm` and its engines at import time (`:13, 27`). Copy the two-line parser (`:128-131`) instead of importing it.

**M12. The upstream claim does not survive the size ratchet.**
`hermes_cli/cli_tui_mixin.py` has 2,311 lines and `hermes_cli/config_defaults.py` 3,108; the cap is 2,000 and files over it may only shrink (`AGENTS.md:103-106`, `scripts/code_health/config.py:22-27`). W2, W3, W4 and W5 as written grow both. Correction: put `_voice_cut_tts` and the state test in `hermes_cli/cli_voice_mixin.py` (843 lines) and keep the net change in `cli_tui_mixin.py` at zero or below. This matters for upstream PRs only; local patches are not checked.

### MINOR

1. W2.6 (lock at three single-assignment writers) changes no behaviour: the issue is ordering, which a lock does not fix. Cut it; it is three extra hunks to rebase.
2. W2.1: `_chat_monitor_agent_thread` can return with the agent thread alive (`cli_chat_turn_mixin.py:471-483`). Store the thread and test `is_alive()` instead of keeping a flag; one added line in `chat()`.
3. W2.5: give the batch path its own event and store it in the existing `_voice_tts_stop`; every existing cutter then works unchanged. Reset with an identity guard (`if self._voice_tts_stop is turn.stop_event`), because batch speech starts before the `finally` (`cli_chat_turn_mixin.py:128, 133, 607-609`).
4. Batch tail race: a key press between agent end and `_voice_speak_response_async` starts a recording that the reply then talks over. Skip the speak call when `_voice_recording` is set.
5. A goal-continuation or notification turn can start while the user records after a tail cut (`cli_tui_runtime_mixin.py:209-216, 234-235`). Guard on `_voice_recording`.
6. A listener trip can still land within one 30 ms block after the key press. Discard it in `_on_trigger` when `_voice_recording` is set.
7. W4.2: `HERMES_VOICE_DEBUG=1` prints to stderr inside the full-screen UI (`tools/voice_mode.py:1313-1318`). Add one info line on listener exit (floor, trigger, peak RMS during playback) so D3 can be decided from `agent.log`.
8. W4.1 at its default is a knob nobody sets (`AGENTS.md:112`): acceptable locally, not upstream. The desktop app has its own trigger code and will not read it.
9. Handler snapshot: read `_voice_tts_stop` into a local before use; another thread resets it.
10. Not addressed, should be stated as out of scope: wake word is deaf during the tail (`cli_voice_mixin.py:670`); "Working…" stays up for the whole drain (`hermes_cli/cli_modern_layout.py:237`); a playback-phase voice barge does not interrupt a turn that is still generating, so the captured utterance waits behind it (`cli_voice_mixin.py:401-407`).
11. Upstream has moved to `716e965`. `tools/tts_tool_speaker.py`, `hermes_cli/cli_tui_mixin.py` and `hermes_cli/cli_voice_mixin.py` are unchanged there; `hermes_cli/cli_tui_runtime_mixin.py` changed in unrelated hunks.
12. Loading: a TUI restart is enough for the classic CLI. The launcher puts the checkout first on `sys.path` (`~/.hermes/hermes-agent/.hermes/bin/hermes`), the venvs use an editable install, and `.pyc` files are timestamp-checked. No dependency changes, so no PM sync.

## 3. Decisions D1-D3

| | What it is | What to weigh | Recommendation | Best argument against |
|---|---|---|---|---|
| D1 | Whether ending your own utterance with the key keeps continuous mode on | How you end utterances (key or 2.5 s silence, `voice.silence_duration`); the printed hint and docs say the key exits (M8); remaining exits would be Ctrl+C while recording, the stop phrase, `/voice off`, three silent takes | Accept "keep armed", gated off where there is no silence auto-stop, and change the hint text | It reverses documented behaviour; a silence-only habit needs no change at all |
| D2 | What the key does mid-turn when nothing is audible | The classification race (M1); a killed tool run cannot be undone; a new notice string costs an edit in every locale file | Reject the plan's default. Cut + record on every mid-turn press; interrupt only on a non-empty transcript | A press during silent work mutes that reply's speech even if you then say nothing |
| D3 | Whether to lower the playback trigger now | AirPods give no speaker bleed, so a low value is safe for you; on speakers it self-triggers (upstream issue #75780); R6 is unproven | Keep the default: no change now, decide from the exit log line (MINOR 7) | With headphones the 1500 floor has no purpose, and a second tuning round costs another ship cycle |

If D1 or D2 is decided wrong, the fix is a one-commit revert plus a reapply, once B1 is fixed.

## 4. Missing test cases

1. Key press while generating, in a gap with no player running and sentences already spoken: audio pipeline stopped, continuous still on.
2. Key press with a modal prompt open during speech: cut only; no interrupt, no recording, overlay intact.
3. Deferred interrupt: transcript arrives while the agent thread is alive → interrupt once; thread already dead → no interrupt.
4. Typed message during the playback tail sets the stop event (M3).
5. Drain loop leaves on a dead thread, on `_should_exit`, on stop plus grace, and on the stall watchdog; a reply with more than 120 s of audio is not cut.
6. `_voice_tts_done` set by another writer while the pipeline thread is alive does not cut the reply.
7. Ctrl+C and Ctrl+Q with batch TTS playing and an idle agent: audio cut, app does not exit.
8. Ctrl+C with a typed draft and audio playing: defined result, asserted.
9. Stop arriving between `Popen` and registration: the player is terminated.
10. Existing-contract guards: the three `_active_playback` tests and the `__new__`-stub tests pass unchanged.
11. `voice.input_device`: missing device prints a visible message for recorder and listener; rate is read from the pinned device; `null` keeps today's calls byte-identical.
12. Batch path: cut during synthesis (before any file plays) plays nothing.
13. Non-macOS path: stop during a PortAudio sentence lets `finish()` return (`@pytest.mark.platforms("linux")`).
14. `dg/reapply.sh`: second run with a changed patch; run after a simulated stash-restore; `--revert`.
15. Each fix's test recorded red on `3b42157858`.

## 5. Must change before build

1. Fix `dg/reapply.sh` first: ignore `dg/plans/`, revert-then-apply using a saved patch, exclude `tests/`, add `--revert`, document the `hermes update` prompt (B1).
2. Keep existing tests green by design: compatible `_active_playback`, class-level defaults and `getattr` for new state, three extra Ctrl+C test files in W6 (B2).
3. Replace the `speaking`/escape-hatch branch: mid-turn key press always cuts and records, never interrupts on press, never interrupts with a modal open; interrupt on a non-empty transcript (M1, D2).
4. Rewrite W2.7 as a bounded poll on the pipeline thread with exit conditions and a stall watchdog; do not wait on `_voice_tts_done`; drop the 60 s change (M2). Add the typed-message cut to the same loop (M3).
5. Shrink W1 to: unbounded queue on the temp-file path, stop-aware semaphore acquire, stop re-check before play, clearer exception logging. Read upstream PR #76655 and its closure reason first (M4).
6. Move W1.5 and W1.6 to a later patch; cut W2.6 (M5, M6, MINOR 1).
7. Write the test procedure into W6 with an isolated `HERMES_HOME`, the repo's test rules, and a red-on-base step (M9).
8. Specify W3 ordering, and record the interrupt trace result in place of "trace later" (M10).
9. Complete W5: visible failure for the listener, rate from the pinned device, local parser copy (M11).
10. Correct the out-of-scope and upstreaming sections: W4/W5 touch shared code, the gateway needs its own restart, and W2/W3/W5 need a sibling-file layout to pass the size ratchet (M7, M12).
11. If D1 is accepted, change `locales/en.yaml:2300` and the doc line; add no new locale key (M8).

Onboarding: the one piece of source worth reading before deciding D2 is the turn lifecycle, `hermes_cli/cli_tui_runtime_mixin.py:159-166` (busy flag) and `hermes_cli/cli_chat_turn_mixin.py:124-133` (the phases of one `chat()` call).
