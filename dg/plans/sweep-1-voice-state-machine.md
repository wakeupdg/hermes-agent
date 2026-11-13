# Sweep 1 — CLI voice state machine: record key, continuous mode, barge-in

Read-only diagnosis. Code: v0.23.1 @ `3b42157858` (`~/.hermes/hermes-agent`, identical to fork `dg/local-patches`).
All paths relative to repo root. No files in either repo were edited; nothing was restarted.

Not done: existing tests were **not run** — the Hermes venv
(`~/.hermes/installs/ef88449a58b4d62f/environments/15e218a8…/venv`) has no `pytest`, and I did not install it.
Everything below is from reading code plus `~/.hermes/logs/agent.log`.

---

## 0. Root cause in one paragraph

`_agent_running` does not mean "the model is generating". It is set before `chat()`
(`hermes_cli/cli_tui_runtime_mixin.py:159`) and cleared only after `chat()` returns (`:187`, via `:166`).
With streaming TTS, `chat()` does not return until the speaker thread has finished playing:
`_chat_settle_turn` pushes the end sentinel and **joins the TTS thread for up to 120 s**
(`hermes_cli/cli_chat_turn_mixin.py:501-507`). So for the entire audible reply `_agent_running` is still `True`.
The record-key handler checks `if self._agent_running or self._voice_processing:` first
(`hermes_cli/cli_tui_mixin.py:836`), sets `_voice_continuous = False` and returns (`:837-840`) — it never reaches
the TTS-cut block at `:847-858`. That single press then cascades:

1. TTS is not cut, recording does not start (symptom 1).
2. The status bar loses "continuous" because the flag really is now `False` (`hermes_cli/cli_status_bar_mixin.py:980`) (symptom 2).
3. The full-duplex listener exits on its next 30 ms block, because `_should_stop` returns `True` as soon as
   `_voice_continuous` is false (`hermes_cli/cli_voice_mixin.py:386-387`) — barge-in dies for the rest of this reply (symptom 3).
4. `_tui_after_turn` skips the auto-restart (`cli_tui_runtime_mixin.py:219`), and the next turn does not arm a
   listener (`cli_chat_turn_mixin.py:233`) — barge-in stays dead until the key is pressed again while fully idle.

Log evidence: `agent.log` (5,969 lines, this session) contains **zero** `TTS CUT: record key handler cutting TTS`
lines (the `logger.info` at `cli_tui_mixin.py:849`), i.e. the cut branch never executed. The only `TTS CUT` lines
are 13 × `streaming TTS prefetch failed mid-sentence` warnings from `tools.tts_tool`.

"Working…" after the visible reply (symptom 4) is the same window, not the background review:
the modern layout prints `cli.modern.working` whenever `_agent_running` is true and the spinner text is empty
(`hermes_cli/cli_modern_layout.py:237`, string at `locales/en.yaml:1800`). Text has finished streaming, the agent
thread is done, but `chat()` is parked in the TTS join. The background review runs on its own daemon thread
`bg-review` (`run_agent.py:827`), spawned from `agent/turn_finalizer.py:613`, and never touches `_agent_running`.
I found no code path where the review holds the CLI in "running" state; sweep 2 owns that question in detail.

---

## (a) Writers of each flag

### `_voice_continuous`
| Where | Sets | When |
|---|---|---|
| `cli_init_mixin.py:578`, `cli_tui_mixin.py:1874` | False | init |
| `cli_tui_mixin.py:860` | **True** (only arming site) | record key pressed while idle, no modal prompt |
| `cli_tui_mixin.py:830` | False | record key pressed while recording (manual stop) |
| `cli_tui_mixin.py:838` | False | record key pressed while `_agent_running or _voice_processing` (escape hatch) |
| `cli_tui_mixin.py:882` | False | Ctrl+C / Ctrl+Q cancels an active recording |
| `cli_voice_mixin.py:271` | False | 3 consecutive no-speech cycles while idle (not under lock) |
| `cli_voice_mixin.py:568` | False | `_disable_voice_mode` (`/voice off`, spoken or typed stop phrase) |
| `cli_voice_mixin.py:714` | False | wake word fired (single-utterance capture; not under lock) |
| `cli_chat_turn_mixin.py:575` | False | turn failed/partial with empty response (not under lock) |

Readers that matter: listener `_should_stop` `cli_voice_mixin.py:386`; listener arming `cli_chat_turn_mixin.py:233`
and `cli_voice_mixin.py:292`; restart gates `cli_voice_mixin.py:278`, `:469`, `cli_tui_runtime_mixin.py:219`;
status bar `cli_status_bar_mixin.py:980`.

### `_agent_running`
- False at init: `cli_init_mixin.py:511`, `cli_tui_mixin.py:1819`.
- True: `cli_tui_runtime_mixin.py:159`, immediately before `chat()`.
- False: `cli_tui_runtime_mixin.py:187` in `_tui_after_turn`, after `chat()` returns. That is after the agent
  thread ended, after the streaming-TTS join (`cli_chat_turn_mixin.py:504`), after render (`:128`) and after
  `_chat_release_turn_audio` (`:133`, another ≤5 s join at `:157`).
- Batch (non-streaming) TTS is different: `_voice_speak_response_async` (`cli_chat_turn_mixin.py:609`) is
  fire-and-forget, so `chat()` returns and `_agent_running` clears while audio is still playing.

### `_voice_processing`
- False at init: `cli_init_mixin.py:578`, `cli_tui_mixin.py:1873`.
- True: `cli_voice_mixin.py:200`, under `_voice_lock`, together with `_voice_recording = False`.
- False: `cli_voice_mixin.py:244` in the `finally`, after `recorder.stop()`, beeps and STT.
- Barge-capture transcription (`_voice_submit_barge_utterance`, `cli_voice_mixin.py:435-470`) does **not** set it.

### `_voice_tts_done` (Event; set = nothing pending)
- Created set: `cli_init_mixin.py:581-582`, `cli_tui_mixin.py:1875-1876`.
- Cleared: `tools/tts_tool_speaker.py:340` at streaming-pipeline start, which is at turn start
  (`cli_chat_turn_mixin.py:272-277`) — so it is "not done" through all of generation, before any audio exists.
  Also `cli_voice_mixin.py:288` and `:300` (batch path).
- Set: `tools/tts_tool_speaker.py:406` (pipeline `finally`), `cli_voice_mixin.py:348` (batch `finally`),
  `cli_tui_mixin.py:856` (record-key cut), `cli_voice_mixin.py:590` (`_disable_voice_mode`).
- "Audio is leaving the speakers right now" is a different signal: `is_audio_output_active()`
  (`tools/voice_mode.py:432`), ref-counted at `tools/tts_tool_speaker.py:316/320` and `tools/voice_mode.py:1016/1020`.

---

## (b) What Ctrl+Space does today, by state

Handler: `cli_tui_mixin.py:817-872`, bound at `:1936`. No-op unless `_voice_mode` (`:824`).

| State | Flags | Result | Lines |
|---|---|---|---|
| Idle | none | arms continuous, starts recording on a daemon thread | 860-871 |
| Idle, modal prompt open | clarify/sudo/approval/… | nothing | 842-844 |
| Recording | `_voice_recording` | continuous=False, stop + transcribe | 826-833 |
| Transcribing | `_voice_processing` | continuous=False, return | 836-840 |
| Foreground generation | `_agent_running` | continuous=False, return. No interrupt, no message printed | 836-840 |
| Streaming TTS, model still generating | `_agent_running`, tts_done clear | continuous=False, return. **Audio not cut** | 836-840 |
| Streaming TTS, model finished (the "Working…" window) | `_agent_running` still True | continuous=False, return. **Audio not cut**; listener exits | 836-840, `cli_voice_mixin.py:386` |
| Batch TTS playing, agent idle | tts_done clear, `_agent_running` False | **works**: cut + arm + record | 847-871 |
| TTS playing + background review, `chat()` returned | as above | works; the review is irrelevant to the handler | — |
| TTS playing + background review, `chat()` still in TTS join | `_agent_running` True | escape hatch; the review is still irrelevant | 836-840 |

So with `auto_tts: true` and a working streaming provider (`cli_chat_turn_mixin.py:243-251`), the cut branch is
only reachable in the ~0.3 s between `chat()` returning and the auto-restart, or never. It is effectively dead code
for the streaming path and live only for the batch path.

A second defect inside the working batch branch: `stop_playback()` kills the current `afplay` process
(`tools/voice_mode.py:991-1004`), but the batch speaker loops over chunk files with no cancel check
(`cli_voice_mixin.py:338-340`), so a long chunked reply resumes with the next file. `_voice_tts_stop` is `None`
or stale there — it is only assigned for streaming (`cli_chat_turn_mixin.py:279`) and never reset.

---

## (c) Should the key interrupt real generation? Is transcript routing correct?

**Interrupting.** The full-duplex generation path calls `self.agent.interrupt()` on detected speech
(`cli_voice_mixin.py:413-418`) and then submits the capture. The key press carries no utterance yet, so I would
not interrupt on the press alone: Ctrl+C already is the stop key, and an accidental press would kill a long tool
run. Recommended: during real generation with nothing audible the key keeps its escape-hatch meaning, but prints a
one-line notice so the status change is explained. If press-to-talk mid-generation is wanted later, it should
start recording and interrupt only once a non-empty transcript exists (same order as the listener). That is a
product decision for DG, not a bug fix.

**Routing.** `_voice_stop_and_transcribe` always puts on `_pending_input` (`cli_voice_mixin.py:232`); so does the
barge path (`:458`). Plugin injection instead picks `_interrupt_queue` when `_agent_running`
(`hermes_cli/plugins_injection.py:21`), which makes the monitor interrupt the turn (`cli_chat_turn_mixin.py:430-451`).
For voice, `_pending_input` is the right queue as the code stands, for two reasons:

- Today a manual recording cannot start mid-turn (escape hatch), so the only mid-turn transcripts come from the
  listener, which has already interrupted or cut by itself.
- `_VoiceInputMessage` is a slots wrapper, not a `str` (`cli.py:813-821`). The interrupt path does
  `self.agent.interrupt(interrupt_msg)` (`cli_chat_turn_mixin.py:451`) and later `"\n".join(text_parts)` (`:636`),
  which only unpacks tuples (`:629-635`). A `_VoiceInputMessage` there would raise `TypeError` and be dropped — the
  same failure class as the #110737 comment at `:623-626` — and would lose the voice marker that gates the concise
  prefix (`cli_process_notifications.py:88`, `cli_chat_turn_mixin.py:289`).

Consequence to accept: a transcript that lands on `_pending_input` while `chat()` is still running is not lost,
but waits until `chat()` returns. It can overlap a turn today when a recording started idle and a non-user turn
then begins (goal continuation / process notification, `cli_tui_runtime_mixin.py:209-216`, `:235`; the heartbeat
watchdog does check `_voice_recording`, `cli_loops_mixin.py:686-690`). `gateway_queue.py` is the durable `/queue`
FIFO for typed text (`hermes_cli/gateway_queue.py:1-13`, used at `cli_tui_mixin.py:1502-1503`); voice transcripts
bypass it, so a queued voice transcript is in-memory only.

---

## (d) Why continuous mode is lost in practice (most likely first)

1. **Record key during the audible reply** — escape hatch at `cli_tui_mixin.py:836-840`, because `_agent_running`
   spans streaming playback. This matches symptoms 1-3 exactly.
2. **Record key used to end your own utterance** — manual stop disarms by design (`cli_tui_mixin.py:830`).
   The turn that follows then has no listener (`cli_chat_turn_mixin.py:233`) and no auto-restart. This is the
   likely reason barge-in "worked once": it works only on turns whose utterance ended by silence auto-stop.
3. **Ctrl+C while recording** (`cli_tui_mixin.py:882`).
4. **Three empty transcriptions while idle** (`cli_voice_mixin.py:268-274`), e.g. room noise tripping the recorder.
5. **Failed turn with empty response** (`cli_chat_turn_mixin.py:574-576`).
6. **Wake word** (`cli_voice_mixin.py:714`).
7. **Stop phrase / `/voice off`** (`cli_voice_mixin.py:568`).

Nothing re-arms automatically: `cli_tui_mixin.py:860` is the only `True` writer.

Additional barge-in failure modes that do not involve the flag:
- Playback-phase trigger is clamped to at least `PLAYBACK_MIN_TRIGGER = 1500` RMS (`tools/voice_mode.py:1308`),
  with a 0.5 s grace after playback onset — quiet speech over the reply will not trip.
- Echo guard drops a playback-phase transcript that resembles the spoken text (`cli_voice_mixin.py:451-457`).
- `_voice_fd_active` allows one listener (`cli_voice_mixin.py:364-366`); it is cleared only after the previous
  capture has been transcribed (`:433`). The transcript is queued at `:458` before that clear, so the next turn's
  arming (`cli_chat_turn_mixin.py:235`) can in principle lose a narrow race and silently not arm. Unobserved; listed as a risk.
- All listener logging is `logger.debug` (`cli_voice_mixin.py:402`, `:410`, `:431`), so the log cannot confirm or
  rule these out at the current level.

---

## (e) Recommended change

### E1. A real "generating" signal
Add `self._turn_generating = False` at both init sites (`cli_init_mixin.py:511`, `cli_tui_mixin.py:1819`).
In `chat()` set it `True` just before `agent_thread.start()` (`cli_chat_turn_mixin.py:125`) and `False` right after
`_chat_monitor_agent_thread` returns (`:126`), plus in the `finally` at `:132`. Leave `_agent_running` alone —
too many readers depend on its "turn not fully settled" meaning.

### E2. Reorder `_tui_handle_voice_record` (`cli_tui_mixin.py:817`)
Compute once, lock-free:
```
audible     = is_audio_output_active()                    # tools/voice_mode.py:432
generating  = self._turn_generating
tts_pending = not self._voice_tts_done.is_set()
speaking    = audible or (tts_pending and not generating) # pipeline live after the model finished
```
Order of checks:
1. `not _voice_mode` → return (unchanged).
2. `_voice_recording` → stop + transcribe (unchanged; see E5 for the disarm).
3. **`speaking` → force cut, always.** `mark_speech_interrupted()`, set `_voice_tts_stop` if not None, set the new
   batch cancel event (E4), `stop_playback()`, `_voice_tts_done.set()`. Then:
   - if `generating` (audio streaming while the model still writes): also `self.agent.interrupt()`, matching the
     listener's generation branch (`cli_voice_mixin.py:413-418`); otherwise the stale reply keeps printing and
     `_pending_input` stays blocked behind the turn;
   - if `self._voice_barge_capture.is_set()`: the listener already owns the mic and will submit — do not start a
     second recording; just return;
   - else, unless a modal prompt is open (`:842-844`) or `_voice_processing`: `_voice_continuous = True` under the
     lock and start recording on a thread (existing `:860-871`).
4. **Escape hatch only here:** `(generating and not audible) or _voice_processing` → `_voice_continuous = False`,
   print a dim one-line notice, return.
5. `_agent_running and not generating and not speaking` (settle/render tail, a few hundred ms) → treat as idle.
6. Idle → modal-prompt guard, arm, record (unchanged).

Change the log at `:849` to stay `info` and include the state, so the next report can be verified from the log.

### E3. Keep the listener and the recorder from double-capturing
`_should_stop` (`cli_voice_mixin.py:385-393`) must also return `True` when `self._voice_recording` is set, checked
before the `_agent_running` clause. Otherwise, after E2 step 3, the listener and the recorder both capture the same
utterance and it is submitted twice. `_tui_after_turn`'s restart thread is already safe: `_voice_start_recording`
has an atomic check-and-set (`cli_voice_mixin.py:83-86`).

### E4. Batch TTS cancel
Add `self._voice_tts_cancel = threading.Event()`; clear it in `_voice_speak_response` beside `:300`; check it
before each `play_audio_file` in the loop at `:338-340`; set it in E2 step 3, in `_disable_voice_mode` (`:582-589`)
and in the listener's playback branch (`:401-407`). Also reset `self._voice_tts_stop = None` in
`_chat_release_turn_audio` (`cli_chat_turn_mixin.py:135`) so a stale event is never mistaken for a live pipeline.

### E5. Optional, needs DG's call: manual stop should not disarm
`cli_tui_mixin.py:830` turns continuous off whenever the key ends an utterance. If DG presses the key to send,
this is cause (d)2. Either drop the disarm there (the escape hatch in E2 step 4, Ctrl+C and "stop" remain as exits)
or gate it on a config key. Behaviour change, so it should be decided, not slipped in.

### E6. Ctrl+C / Ctrl+Q do not cut TTS
`_tui_handle_ctrl_c` (`cli_tui_mixin.py:926-949`) and `_tui_handle_ctrl_q` (`:951-966`) call
`request_hard_interrupt(self.agent)`, which only calls `agent.hard_interrupt()` / `agent.interrupt()`
(`agent/interrupt_compat.py:22-54`). Nothing sets `turn.stop_event` or calls `stop_playback()`:
the monitor sets `stop_event` only when a message arrives on `_interrupt_queue` (`cli_chat_turn_mixin.py:449-450`).
After Ctrl+C the agent thread ends, `_chat_settle_turn` sends the sentinel and joins (`:501-504`), and already-queued
sentences keep playing. Worse, in the "Working…" window the agent thread is already dead, so Ctrl+C sets an
interrupt flag on a finished agent; `_chat_resolve_interrupt` clears a stale flag only when `interrupt_msg` is set
(`:670-685`). Whether that flag then aborts the next turn depends on turn-start handling I did not trace —
flagged as a risk, not a confirmed bug.

Fix: factor E2 step 3's cut into `_voice_cut_tts(reason)` and call it from both handlers before the
`request_hard_interrupt` branch whenever `speaking` is true; when `speaking and not generating`, cut and return
without calling `request_hard_interrupt` and without arming the 2 s force-exit timer.

---

## (f) Tests

Existing (none exercised here — no pytest in the venv):
- `tests/tools/test_voice_cli_integration.py`: `TestVoiceFullDuplexListener` (`:507`; generation trip, arm at
  submit, stop phrase), `TestVoiceBargeCaptureSubmit` (`:387`), `TestVoiceStopAndTranscribeReal` (`:319`),
  `TestDisableVoiceModeReal` (`:236`), `TestVoiceSpeakResponseReal` (`:258`), `TestFallbackSpeakArmsBargeMonitor` (`:653`).
- `tests/hermes_cli/test_chat_turn_streaming_tts_arming.py` — streaming TTS arming only.
- `tests/hermes_cli/test_cli_ctrl_c_draft_priority.py` — Ctrl+C draft-vs-interrupt ordering only.
- `tests/hermes_cli/test_cli_status_bar.py:176` — key label rendering only.

**No test references `_tui_handle_voice_record`** (`grep -rn` over `tests/` returns nothing). The handler's state
machine is entirely uncovered, which is how the dead cut branch shipped.

New tests needed (stub CLI in the style of `test_cli_ctrl_c_draft_priority.py:17`):
1. Key during streaming playback after generation (`_agent_running=True`, `_turn_generating=False`, tts_done clear):
   stop event set, `stop_playback` called, continuous `True`, recording started.
2. Key during streaming playback while generating: as 1, plus `agent.interrupt()` called once.
3. Key during silent generation: continuous `False`, no cut, no recording, no interrupt.
4. Key while `_voice_processing`: disarm only.
5. Key while `_voice_barge_capture` is set: cut, no second recording.
6. Key during batch playback, agent idle: cancel event set and remaining chunk files are not played.
7. `_should_stop` returns `True` once `_voice_recording` is set.
8. Ctrl+C and Ctrl+Q during the playback tail: TTS cut, `request_hard_interrupt` not called, no force-exit arming.
9. Ctrl+C during generation with streaming TTS: both interrupt and cut.
10. Status bar shows the continuous fragment after case 1 (`cli_status_bar_mixin.py:980`).
11. Regression for (d)2 if E5 is accepted.

---

## (g) Risks and edge cases

- **Event-loop blocking.** The handler runs on the prompt_toolkit loop (`cli_tui_mixin.py:820-821`).
  `stop_playback()` takes `_playback_lock`, calls `proc.terminate()` and `sd.stop()` (`tools/voice_mode.py:991-1004`)
  — already called there today, short. `is_audio_output_active()` takes a tiny lock (`:432-435`). Do **not** call
  `agent.interrupt()` inline for the gateway-backed agent: `GatewayAgent.interrupt` does an RPC with `timeout=5`
  (`hermes_cli/gateway_agent.py:587-600`). Run the interrupt on a daemon thread.
- **`_voice_lock` races.** Three writers skip the lock (`cli_voice_mixin.py:271`, `:714`, `cli_chat_turn_mixin.py:575`).
  A key press that arms at `:860` can be overwritten by a concurrent no-speech disarm. Benign per write, but E2
  adds an arm path during a turn, so the failed-turn disarm at `:575` can now land after a fresh arm; take the lock there.
- **`_voice_tts_done.set()` from the handler** while the pipeline thread is still unwinding: `_tui_after_turn`'s
  restart waits on it then sleeps 0.3 s (`cli_tui_runtime_mixin.py:222-224`), so the mic can open up to ~0.5 s
  before the pipeline's queue poll notices the stop (`tools/tts_tool_speaker.py:379-381`). Audio is already
  killed by `stop_playback`, so bleed risk is low; this is existing behaviour.
- **Double capture** if E3 is skipped, and the `_voice_fd_active` re-arm race from (d).
- **Recording overlapping the tail of `chat()`** (new with E2): the transcript waits on `_pending_input` until
  `chat()` returns; the join returns quickly once the stop event is set. Thinking-sound already yields to
  `_voice_recording` (`cli_chat_turn_mixin.py:421-423`).
- **Termux recorder.** No silence auto-stop there (`cli_voice_mixin.py:129-134`), so the key is the only way to end
  an utterance — E5's "manual stop keeps continuous" would create a restart loop on Termux. Gate E5 on
  `supports_silence_autostop`. The full-duplex listener needs `sounddevice` and returns `None` without it
  (`tools/voice_mode.py:1428-1431`), so barge-in is already absent there.
- **Wake word.** `_on_wake_word` ignores wakes while `_agent_running` (`cli_voice_mixin.py:670`), so it is also
  deaf during the playback tail, and it forces continuous off (`:714`). Not changed by this proposal; if wake should
  work during playback it needs the same `speaking` test.
- **Gateway/TUI surface parity.** `tui_gateway/methods_voice.py` is a separate implementation and does not share
  the bug: its listener stops on "no session running and no TTS pending and no audio" (`:257-259`), has no
  `_voice_continuous` gate, and its trip always cuts TTS first then interrupts (`_fd_trip`, `:296-323`;
  `_tts_stream_stop`, `:194-211`). Fixes above are CLI-only; do not port the escape hatch to that surface. I did not
  trace how the Ink/desktop record key maps onto these methods.
- **Upstream drift.** Line numbers are for `3b42157858`; the fork carries local patches, so re-check before editing.
