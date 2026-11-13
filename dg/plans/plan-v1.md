# Plan V1: voice barge-in, record key, streaming TTS and input device fixes

Target: Hermes v0.23.1 (`3b42157858`), fork `wakeupdg/hermes-agent`, branch `dg/local-patches`, clone `~/!MY PROJECTS/hermes-agent-dg`. Ship to the install with `dg/reapply.sh`.

Evidence: `sweep-1-voice-state-machine.md` (S1), `sweep-2-background-review.md` (S2), `sweep-3-tts-playback.md` (S3), all in this folder. Line numbers are for `3b42157858`; re-check before editing.

## Root causes (confirmed)

| # | Symptom | Cause | Evidence |
|---|---|---|---|
| R1 | Record key does nothing while Hermes speaks, and "continuous" disappears | `_agent_running` stays true through the streaming-TTS join (up to 120 s). The key handler's busy branch disarms continuous and returns before the TTS-cut block. | S1 §0, S2, `cli_tui_mixin.py:836-858`, `cli_chat_turn_mixin.py:501-507` |
| R2 | Barge-in dies after one key press, and only "worked once" | The disarm stops the full-duplex listener. A manual stop of your own utterance also disarms (`:830`), so the next turn has no listener. | S1 §d |
| R3 | Long replies cut mid-sentence ("TTS CUT … prefetch failed") | Bounded 64-chunk queue (10.92 s of Kokoro audio) plus `put(timeout=30)`. A long sentence waiting more than 30 s behind earlier audio is truncated. | S3 §2, reproduced |
| R4 | Barge-in looks dead for 10-30 s after a cut | Stop strands blocked prefetch threads (untimed sentinel put, undrained queues, unstoppable semaphore), so `finish()` stalls the done event | S3 §2, reproduced |
| R5 | Ctrl+C / Ctrl+Q never stop audio | They only interrupt the agent; nothing cuts playback | S1 §E6 |
| R6 | Mic barge-in may not trip on quiet speech | Playback trigger has a hard floor of 1500 RMS; failures are debug-only, so the cause is unproven | S1 §d, S3 §3 |
| R7 | Mic can follow macOS to the AirPods | No `voice.input_device`; all capture opens the default input | S3 §5 |

Refuted: background review does not set `_agent_running` and is not the cause (S2). Leave it on.

## Workstreams (one commit each, tests in the same commit)

### W1 — Streaming TTS pipeline (`tools/tts_tool_speaker.py`, `tools/voice_mode.py`) — fixes R3, R4
1. Replace `chunk_queue.put(chunk, timeout=30.0)` (`:245`) with a stop-aware blocking put (reuse `_put_unless_stopped`, `tools/tts_streaming.py:382-392`). Add a liveness guard: if the consumer thread is dead, log a warning and stop.
2. Stop-aware sentinel put (`:249`), stop-aware semaphore acquire (`:232`), drain skipped sentence queues in `_for_each_sentence` (`:258-260`), close `audio_iter` in the prefetch `finally`.
3. Re-check stop between WAV write and `play_audio_file` (`:56-62`).
4. Logging: catch `queue.Full` separately; log the exception type, sentence index, text length, chunks and bytes queued, and seconds waited.
5. `_active_playback` becomes a set of processes registered under the lock; `stop_playback()` kills all of them, and each player removes only itself (`voice_mode.py:981-1003, 1105-1122`). This stops a finishing beep from orphaning the TTS player.
6. Flush the short-tail sentence after about 1 s idle regardless of length (`:384`).

### W2 — Record-key state machine (`hermes_cli/cli_tui_mixin.py`, `cli_chat_turn_mixin.py`, `cli_voice_mixin.py`) — fixes R1, R2
1. Add `_turn_generating`: true only while the agent thread runs (set around `agent_thread.start()` / monitor return, cleared in `finally`; init at both init sites). Leave `_agent_running` semantics alone.
2. Add helper `_voice_cut_tts(reason)`. It calls `mark_speech_interrupted()`, sets `_voice_tts_stop` if present, sets the new batch cancel event (W2.5), calls `stop_playback()` and sets `_voice_tts_done`. It logs at info with the state.
3. Reorder `_tui_handle_voice_record`. `speaking = is_audio_output_active() or (tts pending and not _turn_generating)`.
   - Recording → stop and transcribe (unchanged except D1).
   - Speaking → cut. If still generating, call `agent.interrupt()` on a daemon thread (the gateway RPC can block 5 s). If the barge listener already owns the mic, return. Otherwise arm continuous (under the lock) and start recording, unless a modal prompt is open or transcribing.
   - Generating with nothing audible, or transcribing → escape hatch: disarm continuous and print a dim one-line notice.
   - `_agent_running` with neither generating nor speaking (settle tail) → treat as idle.
   - Idle → unchanged.
4. Listener `_should_stop` returns true once `_voice_recording` is set, to prevent double capture (`cli_voice_mixin.py:385-393`).
5. Batch TTS cancel: add `_voice_tts_cancel` and check it before each chunk file (`cli_voice_mixin.py:338-340`). Set it from the cut helper, from `_disable_voice_mode`, and from the listener's playback branch. Reset `_voice_tts_stop = None` in `_chat_release_turn_audio`.
6. Take `_voice_lock` at the unlocked continuous writers (`cli_voice_mixin.py:271, 714`, `cli_chat_turn_mixin.py:575`).
7. Remove the fixed playback timeouts: the 120 s join (`cli_chat_turn_mixin.py:504`), the 60 s wait (`cli_tui_runtime_mixin.py:223`) and the 300 s worker join (`tts_tool_speaker.py:325`). Replace them with stop-aware waits on the done event. Keep the per-process 300 s afplay cap.

### W3 — Ctrl+C / Ctrl+Q cut audio (`cli_tui_mixin.py:926-966`) — fixes R5
- If speaking and not generating: cut and return, with no hard interrupt and no 2 s force-exit arming.
- If speaking and generating: cut, then do the normal hard interrupt.
- Trace whether a hard interrupt on an already-finished agent poisons the next turn (S1 open risk), and fix it if so.

### W4 — Barge-in tuning and observability (`tools/voice_mode.py`, `cli_voice_mixin.py`, config defaults) — R6
1. New config `voice.barge_in_playback_min_trigger` (default 1500, unchanged) that replaces the hard `PLAYBACK_MIN_TRIGGER` floor. Document that values below 1500 are for headphones only.
2. Raise listener armed / tripped / exited / failed logs from debug to info (failures at warning).

### W5 — `voice.input_device` (`hermes_cli/config_defaults.py`, `tools/voice_mode.py`) — R7
- Add `voice.input_device: null`. Parse it like `wake_word.input_device` (`tools/wake_word.py:128-131`) and pass `device=` at all three capture sites plus `query_devices`.
- Prefer names. A missing pinned device fails loudly, with no silent fallback to the default.
- After shipping, set `voice.input_device: "USB Microphone"`. This replaces the earlier launchd "AirPods mic watcher" idea.

### W6 — Tests (run with the Hermes test runner; install pytest into a dev venv, never into the Hermes runtime venv)
- Record-key cases: S1 §f 1-10.
- Pipeline cases: S3 §7 1-9.
- `voice.input_device` parsing and propagation.
- Full existing suites must pass: `tests/tools/test_voice_cli_integration.py`, `tests/tools/test_tts_streaming.py`, `tests/tools/test_voice_mode.py`, `tests/tools/test_tts_macos_output.py`, `tests/hermes_cli/test_chat_turn_streaming_tts_arming.py`, `tests/hermes_cli/test_cli_ctrl_c_draft_priority.py`, `tests/hermes_cli/test_cli_status_bar.py`, and `tests/tui_gateway/test_tui_gateway_server.py`.

### W7 — Ship and verify
1. Commit per workstream on `dg/local-patches`, update `dg/README.md` "Patches", and push to the fork.
2. `dg/reapply.sh --check`, then `dg/reapply.sh`.
3. Live verification is manual by DG after a restart. Checklist:
   - Record key during a long reply cuts audio within 300 ms, and "continuous" stays on.
   - Speaking over a reply cuts it.
   - A 4+ sentence reply plays fully with no "TTS CUT" warning.
   - Ctrl+C during playback stops the audio and does not kill the next turn.
   - The mic stays on the USB Microphone.

## Decisions needed from DG (defaults in bold)
- **D1** Ending your own utterance with the record key: **keep continuous armed** (gated off on Termux, where the key is the only way to end an utterance) / keep today's disarm.
- **D2** Record key during silent generation: **escape hatch with a visible notice** / start recording and interrupt once a transcript exists.
- **D3** Lower the barge-in playback trigger for headphones now: **no, ship the knob at 1500 and tune with `HERMES_VOICE_DEBUG=1` data** / yes.

## Out of scope
- `tui_gateway/methods_voice.py` parity (S1: it does not share R1/R2). Only W1 touches shared code, so the gateway surface gets the pipeline fixes for free.
- Kokoro server-side chunked streaming (low value on macOS).
- Background review config (S2: not the cause).

## Upstreaming
W1 and W3 are clean upstream PR candidates. W2 and W5 may be too after D1/D2 settle.
