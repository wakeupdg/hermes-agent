# Sweep 3 — Streaming TTS playback on macOS with Kokoro: diagnosis

Read-only investigation, Hermes v0.23.1 (`~/.hermes/hermes-agent`; `tools/tts_tool_speaker.py` and `tools/voice_mode.py` are byte-identical in the `hermes-agent-dg` clone). No repo, config, service or audio setting was changed. Paths are relative to the Hermes repo unless prefixed `kokoro:` (`~/.hermes/plugins/kokoro-tts/`).

## 1. Verdict

- **The "TTS CUT" failures are a deterministic backpressure bug in the speaker pipeline.** They are not an afplay hang, Kokoro lock contention, Bluetooth/HFP, or CoreAudio contention.
- A queued sentence longer than **10.92 s of audio** that waits more than **30 s** behind earlier sentences is truncated to exactly its first 10.92 s. The rest is dropped.
- Reproduced offline with a fake streamer and a fake player (`~/.hermes/cache/scratch/sweep3/repro.py`): a 17.07 s sentence played as 10.92 s, with the same empty-message warning.
- **Barge-in has never cut playback in the logged history.** `agent.log` contains zero `Audio playback interrupted` lines (`tools/voice_mode.py:999`) and zero `TTS CUT: record key handler cutting TTS` lines (`hermes_cli/cli_tui_mixin.py:849`).
- The record key (ctrl+space) cannot cut TTS while a turn is open, by construction (section 3). Why the microphone barge-in did not trip is not proven: its failures are logged at debug level only.

## 2. (a) Architecture and root cause

### Pipeline on macOS

| Stage | What it does | Where |
|---|---|---|
| Arming | The CLI starts `stream_tts_to_speaker(text_queue, stop_event, _voice_tts_done)` per turn. The TUI gateway does the same. | `hermes_cli/cli_chat_turn_mixin.py:253-279`, `tui_gateway/methods_voice.py:172-191` |
| Text pump | Polls `text_queue` every 0.5 s, cuts sentences with `SentenceChunker`, calls `playback.speak()` per sentence. | `tools/tts_tool_speaker.py:379-390` |
| Streamer choice | `kokoro-mlx` declares `streams_pcm = True`, so it resolves to `_PluginPCMStreamer` and the `_StreamerPlayback` path. | `kokoro:kokoro_provider.py:84-85`, `tools/tts_streaming.py:174-195`, `tools/tts_tool_speaker.py:349-357` |
| Prefetch | `speak()` takes a `Semaphore(3)` slot, creates a `Queue(maxsize=64)` for the sentence, and starts a prefetch thread. | `tools/tts_tool_speaker.py:166, 177, 225-237` |
| Prefetch loop | `_consume_to_queue` does `chunk_queue.put(chunk, timeout=30.0)` per chunk. `stop_event` is checked only between chunks. The `finally` does an untimed `chunk_queue.put(None)` and then releases the semaphore. | `tools/tts_tool_speaker.py:239-250` |
| Consumer | One worker thread. On macOS `_device_usable()` returns False, so every sentence goes through `_play_sentence_via_tempfile`. | `tools/tts_tool_speaker.py:188-193, 305-309` |
| Playback | Drain the sentence's whole queue, write a temp WAV, call `play_audio_file`, which runs `afplay` and blocks in `proc.wait(timeout=300)`. | `tools/tts_tool_speaker.py:45-69, 72-74, 252-254`, `tools/voice_mode.py:1098-1123` |
| Stop | `stop_event` makes the consumer skip remaining sentences. `stop_playback()` terminates the one tracked `afplay`. | `tools/tts_tool_speaker.py:256-260`, `tools/voice_mode.py:991-1003` |
| Finish | `finish()` joins the worker (300 s), then each prefetch thread (10 s each), then sets `tts_done_event`. | `tools/tts_tool_speaker.py:322-328, 396-406` |

### Why the producer times out

- The consumer does not touch sentence N+1's queue until sentence N's `afplay` has exited.
- Kokoro returns a whole sentence in about 1–3 s, so the prefetch thread fills its queue almost at once.
- Kokoro chunks are 8192 bytes (`kokoro:kokoro_provider.py:26, 273`). 64 × 8192 B = 524,288 B, which is 10.92 s at 24 kHz int16 mono.
- A sentence longer than that blocks on chunk 65. If the audio ahead of it takes more than 30 s to play, `put` raises `queue.Full`.
- The `except` logs the warning. The sentence's remaining audio is discarded. Playback of that sentence later stops mid-word at 10.92 s.

### Evidence from the two named failures

Kokoro's server logs `POST` when it sends the response headers, i.e. when synthesis has finished (`kokoro:server/kokoro_server.py:237-238, 333`). So the prefetch thread starts blocking within milliseconds of that line.

| Failure | Last POST before it | Gap | Blocked sentence | Audio ahead of it |
|---|---|---|---|---|
| 00:07:36.260 | 00:07:06.253 | 30.007 s | 4th of 4, 482 chars (about 30 s) | 49 + 359 + 336 chars, about 46 s |
| 00:09:19.559 | 00:08:49.553 | 30.006 s | 5th, 186 chars (about 12 s) | 416 chars plus per-sentence afplay start-up |
| 00:14:00.863 / 02.276 / 03.055 | 00:13:30.854 / 32.272 / 33.051 | 30.0 s each | three queued sentences | — |

- The 00:07 reply was 1226 chars and exactly 4 sentences (from `state.db`), matching the 4 POSTs.
- Sentence 4 would have ended around 00:07:57 after its 10.92 s. The user's "i tried to barge in but did not work" arrived at 00:07:58.
- Sentence lengths and durations are my estimate at roughly 15 chars/s; the 30 s gaps are exact log arithmetic.
- The 00:09 reply's 6th sentence (100 chars) was never POSTed before the cut. Most likely cause: the idle flush only fires when the buffer is over 100 chars (`tools/tts_tool_speaker.py:384`), so a short tail before a tool call waits for the end of the turn.

### Hypotheses ruled out

- **afplay hang:** no `System player ... timed out` warning (`tools/voice_mode.py:1115`); sentences before the cut played normally.
- **Kokoro lock contention:** synthesis finished within about 3 s per sentence; the lock only serialises (`kokoro:server/kokoro_server.py:117-120`).
- **AirPods HFP switch:** all 47 `Voice recording started` lines show `rate=48000`. That fits the USB microphone; an AirPods HFP mic would report 16 or 24 kHz.
- **A stop event:** zero `TTS CUT: prefetch cancelled` lines (`tools/tts_tool_speaker.py:243`).

### Second defect: barge-in strands blocked prefetch threads (reproduced)

- When `stop_event` is set, the consumer skips queued sentences without draining them (`tools/tts_tool_speaker.py:258-260`).
- A prefetch thread blocked in `put` wakes after 30 s, logs the same warning, then blocks forever on the untimed `put(None)` (`:249`). Its semaphore slot is never released (`:250`).
- `finish()` waits 10 s per stuck thread (`:326-327`), so `tts_done_event` is delayed 10–30 s after a barge-in. In the repro, `finish()` returned 13.1 s after start (10 s of that is the join), and the thread was still alive 32 s later.
- The CLI waits on that event before restarting the mic (`hermes_cli/cli_tui_runtime_mixin.py:223`). So a successful barge-in can still look dead for up to 30 s.
- `speak()` calls `self._prefetch_sem.acquire()` with no stop check (`:232`). If all three slots are stranded, the pump blocks forever and `tts_done_event` is never set.

### Empty exception message and better logging

- `queue.Full` carries no arguments, so `str(exc)` is `""` and `%s` prints nothing (`tools/tts_tool_speaker.py:247`).
- Proposed: catch `queue.Full` separately and log at warning with `type(exc).__name__`, sentence index, text length, chunks and bytes queued, seconds waited, and whether `stop_event` is set. Use `%r` or `exc_info=True` for other exceptions.
- Also raise these from debug to info or warning so the next failure is diagnosable:
  - `Full-duplex listener failed` (`tools/voice_mode.py:~1454`)
  - listener armed / tripped / exited (`hermes_cli/cli_voice_mixin.py:402, 411`, `tui_gateway/methods_voice.py:205, 303-306`)
  - `Temp-file TTS fallback failed` should include the exception type (`tools/tts_tool_speaker.py:64`)

## 3. (b) stop_playback, is_audio_output_active and barge-in

### How they work

- `stop_playback()` terminates one module-global `_active_playback` process and calls `sd.stop()` (`tools/voice_mode.py:981-1003`).
- `is_audio_output_active()` is a ref-count bracketed around every `play_audio_file` call (`tools/voice_mode.py:419-435, 1012-1020`). It is true only while a player runs, so it drops between sentences. `_BargeDetector` tolerates that (`tools/voice_mode.py:1365-1374`).

### Per-path status

| Path | Stoppable | Reports active | Evidence |
|---|---|---|---|
| Streaming tempfile sentences | Yes, via `afplay` terminate; two races below | Yes | `tools/tts_tool_speaker.py:61-62` |
| Non-streaming whole-file (`_voice_speak_response`) | Current file only; the loop then plays the next chunk file because it has no stop check | Yes | `hermes_cli/cli_voice_mixin.py:339-342` |
| Sync per-sentence pipeline | Yes; checks stop before each play | Yes | `tools/tts_tool_speaker.py:148-150` |
| Beeps | Yes, but they share the single slot | Yes, so a beep counts as "playback" | `tools/voice_mode.py:76-87, 380-411` |

### Defects that break barge-in

1. **Record key is ignored while a turn is open.** `_tui_handle_voice_record` returns early when `_agent_running` is true, before the TTS-cut block (`hermes_cli/cli_tui_mixin.py:836-840` vs `:845-858`).
   - `_agent_running` stays true for the whole `chat()` call (`hermes_cli/cli_tui_runtime_mixin.py:159-166, 187`).
   - `chat()` includes `tts_thread.join(timeout=120)` (`hermes_cli/cli_chat_turn_mixin.py:502-507`), so the flag is true through up to 120 s of post-generation playback.
   - The early return also sets `_voice_continuous = False`. That makes the microphone listener's `_should_stop` return true (`hermes_cli/cli_voice_mixin.py:385-387`), so the keypress turns microphone barge-in off as well.
2. **Microphone barge-in only arms in continuous mode** (`hermes_cli/cli_chat_turn_mixin.py:233-235`).
3. **Playback trigger is fixed at 1500 RMS or higher.** `max(floor × mult, PLAYBACK_MIN_TRIGGER)` means `barge_in_threshold_multiplier` can only raise it (`tools/voice_mode.py:1308, 1383-1385`). That floor exists for speaker bleed, which headphones do not have. Whether DG's USB mic speech stays under 1500 is unverified; `voice.silence_threshold: 275` hints at low levels.
4. **Listener failures are silent.** `full_duplex_listen` opens a second `InputStream` at 16 kHz on the default device while `AudioRecorder` keeps its own 48 kHz stream open (`tools/voice_mode.py:782-791, 812, 1439`). The code comment at `:789-791` says CoreAudio can deactivate a stream when another opens. Any failure is logged at debug only.
5. **Single-slot player tracking.** Every player's `finally` calls `_set_active_playback(None)` (`tools/voice_mode.py:1107, 1122`). A beep finishing during TTS clears the slot, and the TTS `afplay` becomes unkillable.
6. **Stop/play race.** `_play_via_tempfile` does not check `stop_evt` between closing the WAV and calling `play_audio_file` (`tools/tts_tool_speaker.py:56-62`). There is also a gap between `Popen` and slot registration (`tools/voice_mode.py:1105-1107`).
7. **Timeouts that cut or overlap long replies.**
   - 120 s join, then the release path sets `stop_event` (`hermes_cli/cli_chat_turn_mixin.py:153-155, 504-507`). Replies with more than 120 s of audio left at end-of-text get cut. No occurrence in the logs yet.
   - 60 s wait before the mic restarts (`hermes_cli/cli_tui_runtime_mixin.py:223`). After that, the mic reopens while TTS still speaks, and the next ctrl+space stops the recording instead of the TTS (`hermes_cli/cli_tui_mixin.py:826-833`).
   - 300 s worker join (`tools/tts_tool_speaker.py:325`).

## 4. (c) Kokoro: sentence-chunked, not true streaming

- The server synthesises the whole input, then sends one body with `Content-Length` (`kokoro:server/kokoro_server.py:109-125, 324-338`). Inference is serialised by one lock (`:67, 117`).
- The provider's `stream()` slices that finished response into 8192-byte blocks (`kokoro:kokoro_provider.py:252-277`).
- On macOS the speaker drains the full sentence before playing anyway (`tools/tts_tool_speaker.py:253`), so server-side streaming would not lower latency without a different output path.
- **Latency:** time to first audio is first-sentence synthesis plus WAV write plus `afplay` start. From the logs, a 359-char sentence was ready within 3.0 s and a 482-char one within 2.6 s. I estimate synthesis runs about 10× faster than real time. Later sentences are prefetched, so gaps are mostly `afplay` start-up.
- **Minor:** `_PluginPCMStreamer.stream` is a generator (`tools/tts_streaming.py:186-189`). The provider's eager first request (`kokoro:kokoro_provider.py:260-263`) therefore runs in the prefetch thread, and the `try/except` in `speak()` never sees a dead-server error (`tools/tts_tool_speaker.py:227-231`).

## 5. (d) Input and output devices

### Current behaviour

- **Verified: there is no `voice.input_device`.** The `voice` defaults have no such key (`hermes_cli/config_defaults.py:1198-1232`). Only `wake_word.input_device` exists (`:1249`), parsed at `tools/wake_word.py:128-131` and used at `:639`.
- All three voice capture sites open the PortAudio default input with no `device=`:
  - `AudioRecorder._ensure_stream` (`tools/voice_mode.py:812`), rate from `sd.query_devices(None, "input")` (`:128-136`)
  - `full_duplex_listen` (`:1439`)
  - `listen_for_speech` (`:1197`)
- `create_audio_recorder()` takes no config (`:935-937`).
- Output is always `afplay` on macOS (`tools/voice_mode.py:64-73, 1088`), which follows the system default output and has no device flag.

### Feasibility of `voice.input_device`

Small change, and the wake-word code is a working template:

- Add `"input_device": None` to the `voice` block (`hermes_cli/config_defaults.py:~1213`).
- Add a `_voice_input_device()` helper in `tools/voice_mode.py`, reusing the parser at `tools/wake_word.py:128-131`.
- Pass `device=` at `tools/voice_mode.py:812, 1197, 1439` and to `query_devices` at `:131`.
- Reuse `_describe_input_device` (`tools/wake_word.py:233`) for diagnostics.
- If the pinned device is missing, fail loudly. A silent fallback to the default could select the AirPods mic.
- PortAudio matches names by substring and snapshots the device list at init, so a hot-plugged device needs a re-init.

### AirPods and HFP

- macOS drops AirPods from A2DP to HFP (mono, low-rate, with a 1–2 s route gap) only when a process opens the AirPods microphone.
- Hermes opens the default input, so this happens only if macOS makes the AirPods the default input, which it can do on reconnect.
- `AudioRecorder` keeps its stream open for the process lifetime (`tools/voice_mode.py:782-791`), so one bad pick would hold HFP for the whole session.
- Tonight's logs show no sign of it (all recordings at 48 kHz).
- Pinning "USB Microphone" by name removes the risk. Hermes cannot defend the output route itself; it can only detect and warn.

## 6. (e) Recommended fixes, by impact

1. **Stop the lossy put.** `tools/tts_tool_speaker.py:245`: replace `put(chunk, timeout=30.0)` with a stop-aware blocking put; `_put_unless_stopped` already exists (`tools/tts_streaming.py:382-392`). Alternatively make the per-sentence queue unbounded on the tempfile path (`:166, 233`). Memory is already capped at 16 MiB per sentence × 3 in flight (`tools/tts_streaming.py:29, 218-226`). This alone removes every cut in the logs.
2. **Make stop clean.** In `tools/tts_tool_speaker.py`:
   - stop-aware sentinel put (`:249`)
   - drain skipped queues in `_for_each_sentence` (`:258-260`)
   - stop-aware semaphore acquire (`:232`)
   - close `audio_iter` in the prefetch `finally` so the HTTP response is released
   - re-check `stop_evt` just before `play_audio_file` (`:61-62`)
3. **Let the record key cut TTS during a turn.** Move the cut block (`hermes_cli/cli_tui_mixin.py:845-858`) above the `_agent_running` return (`:836-840`). Do not clear `_voice_continuous` when the press was a TTS cut.
4. **Remove fixed playback timeouts.** Replace the 120 s join (`hermes_cli/cli_chat_turn_mixin.py:504`), the 60 s wait (`hermes_cli/cli_tui_runtime_mixin.py:223`) and the 300 s join (`tools/tts_tool_speaker.py:325`) with stop-aware waits on the done event.
5. **Logging** as listed in section 2.
6. **Add `voice.input_device`** as in section 5.
7. **Track all live players.** Make `_active_playback` a set, remove only the caller's own process, register under the lock (`tools/voice_mode.py:981-1003, 1105-1122`). Add a stop check between files in `_voice_speak_response` (`hermes_cli/cli_voice_mixin.py:340-342`).
8. **Make the playback trigger configurable**, e.g. `voice.barge_in_playback_min_trigger`, for headphone users (`tools/voice_mode.py:1308, 1383`).
9. **Flush short tails.** `tools/tts_tool_speaker.py:384`: flush after about 1 s idle regardless of length, so the sentence before a tool call is spoken.
10. **Optional:** chunked streaming in the Kokoro server. Low value on macOS today.

**Possible interim workaround, untested:** setting `tts.streaming.provider` to a built-in name with no credentials makes `resolve_streaming_provider` return `None` (`tools/tts_streaming.py:208-215`). That selects `_SyncSentencePipeline`, which has no chunk queue (`tools/tts_tool_speaker.py:100-154`). If credentials for that name appear later, the voice would switch.

## 7. (f) Tests

### Existing

- `tests/tools/test_tts_macos_output.py:78, 83` — macOS routing only (skips sounddevice).
- `tests/tools/test_tts_streaming.py`
  - prefetch order and overlap: `:720, 775, 827, 864`
  - PortAudio recovery: `:515-615`
  - sync pipeline: `:979-1025, 1158-1191`
  - byte cap: `:435`
- `tests/tools/test_voice_mode.py:582, 882` — macOS afplay routing; `stop_playback` terminates a process.
- `tests/tools/test_voice_cli_integration.py:252, 295`.
- `tests/hermes_cli/test_chat_turn_streaming_tts_arming.py`.
- `tests/tui_gateway/test_tui_gateway_server.py` references the full-duplex listener.
- Kokoro plugin: `tests/test_provider.py`, `tests/test_server.py`.

Nothing covers `_consume_to_queue` backpressure, the 30 s timeout, stop during a blocked prefetch, or the record key during an open turn. No test name in the three `tests/tools/test_voice_*` files I checked mentions barge or duplex.

### Needed

1. A sentence of more than 64 chunks queued behind slow playback plays completely. Use a fake streamer, a patched `play_audio_file` and an injectable timeout.
2. Stop while a prefetch thread is blocked: `finish()` returns in under 1 s, no thread stays alive, all semaphore slots are free, `tts_done_event` is set.
3. Stop with three blocked sentences and a fourth pending: the pump exits.
4. Stop set between WAV write and play: the player is not launched.
5. Record key with `_agent_running` true and `_voice_tts_done` clear: stop event set, `stop_playback` called, `_voice_continuous` unchanged.
6. Two concurrent players (beep plus TTS): `stop_playback` kills both, and the beep finishing does not orphan the TTS process.
7. `voice.input_device`: name and index reach all three `InputStream` sites and `query_devices`; a missing device raises a clear error.
8. The timeout log line includes the exception type and sentence stats.
9. A reply with more than 120 s of audio is not cut by the settle path.

## 8. (g) Risks

- **Unbounded per-sentence queue:** up to about 48 MiB transient. Negligible here; keep the bound on the PortAudio path where the consumer drains live.
- **Stop-aware puts without a timeout:** a dead consumer would block the producer until stop. Add a consumer-liveness check or a long watchdog that logs loudly.
- **Record-key change:** the key currently means "disarm continuous" during a turn. Redefining it as "cut TTS" while audio plays changes behaviour; keep the disarm when nothing is playing.
- **Removing join timeouts:** a truly hung `afplay` would then hang the turn. Keep the per-process 300 s cap (`tools/voice_mode.py:1108`) and stop-aware waits.
- **Input pinning:** substring matches can be ambiguous; the device list is stale after hot-plug; indices are unstable across reboots, so prefer names.
- **Lower playback trigger:** safe with headphones, self-triggers on speakers. It must stay opt-in.
- **Upstream drift:** `tools/tts_tool_speaker.py` is upstream code. Keep local patches minimal and track them with `dg/reapply.sh`. Fix 1 is a small patch and a good upstream PR candidate, because other streamers yield smaller chunks and hit the same bound sooner.
- **Open question:** the cause of the microphone barge-in failure. Run with `HERMES_VOICE_DEBUG=1` (`tools/voice_mode.py:1313-1318`) to capture detector output before tuning thresholds.
