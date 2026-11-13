# Sweep 2: background review vs. the CLI busy flag, "Working...", and voice input

Read-only diagnosis. No code, config or process was changed.

**All `file:line` references are in the repo `hermes-agent`** (fork clone `/Users/dganesh/!MY PROJECTS/hermes-agent-dg`, branch `dg/local-patches`, head `5cb7923f15`; installed copy `/Users/dganesh/.hermes/hermes-agent` at `3b42157858`). The ten files this report leans on are byte-identical in both copies (checked with `diff -q`).

Log: `/Users/dganesh/.hermes/logs/agent.log`. `S` below means session `local-824c4c4d...a668ab`.

## Verdict

**The hypothesis is refuted.** Background review never sets `_agent_running` and never drives the spinner or "Working...".

**The real cause is the streaming-TTS drain.** `chat()` blocks until the spoken reply has finished playing, and `_agent_running` stays `True` for that whole time. The review happens to start at the same instant, which is why the two looked linked in the log.

What is and is not proven:

| Claim | Status |
|---|---|
| Review runs on its own thread, in a different process from the TUI in this setup | Proven by code and by the log |
| Review has no reference to `_agent_running` or the spinner | Proven by grep (zero hits) |
| `_agent_running` stays `True` while streaming TTS plays | Proven by code |
| The windows at 00:07 and 00:17 were TTS playback | Inferred. The CLI does not log TTS start/end or `_agent_running`, so the log cannot show it directly. Timing and the user's own message fit |

## (a) How background review works

### Trigger

- It is spawned at the end of a foreground turn, from `finalize_turn` via `_mirror_turn_to_memory`: `agent/turn_finalizer.py:601-616`, called at `agent/turn_finalizer.py:871-875`.
- It fires only if the turn produced a final response, was not interrupted, `skip_background_review` is false, and at least one of two counters is due (`agent/turn_finalizer.py:605-610`):
  - **Memory**: every `memory.nudge_interval` user turns (`agent/turn_context.py:819-828`).
  - **Skills**: once `_iters_since_skill` reaches `skills.creation_nudge_interval` tool iterations (`agent/turn_finalizer.py:863-869`; incremented at `agent/turn_iteration_prep.py:166-167`).
- Further gates in `_spawn_background_review` (`run_agent.py:759-790`): not for delegated children (`run_agent.py:769`), `auxiliary.background_review.enabled` (`run_agent.py:773-776`, loader at `agent/background_review.py:228-250`), and idle-deferral when the review would run on the managed local llama-server (`run_agent.py:786-789`, `run_agent.py:172-175`).
- It is not "after almost every reply". In session `S` the log shows 9 reviews against 43 foreground turns.

### Thread and process

- A daemon thread named `bg-review`: `run_agent.py:827`. The spawn returns at once; nothing joins it.
- The thread builds a **separate** `AIAgent` fork (`agent/background_review.py:979-1061`) with `quiet_mode=True` and no UI callbacks (`agent/background_review.py:942-950`), `suppress_status_output=True` (`agent/background_review.py:1016`), and stdout/stderr silenced for that thread only (`agent/background_review.py:1299-1300`).
- **In this setup the fork is not even in the TUI process.** The CLI's `self.agent` is a `GatewayAgent` proxy (`hermes_cli/gateway_agent.py:87`, `hermes_cli/cli_shutdown.py:159-163`). Every prompt in the log goes through `gateway.run: inbound message: platform=local`. `ps` shows the gateway as pid 53924 (no tty, started 22:17) and the TUI as pid 97973 (tty s006). The real `AIAgent` and its review fork live in the gateway.
- The proxy's `run_conversation` returns when the gateway sends `message.complete` (`hermes_cli/gateway_agent.py:873-875`, `hermes_cli/gateway_agent.py:804-806`). The gateway logs `response ready` before the review turn starts (see timelines), so the review cannot hold the CLI turn open.

### UI state it touches

- `_agent_running`: **none.** `grep -rl _agent_running agent/ run_agent.py gateway/ tui_gateway/ tools/` returns no files. The only writers are `hermes_cli/cli_tui_runtime_mixin.py:159` (True) and `:187` (False), plus initialisers at `hermes_cli/cli_init_mixin.py:511` and `hermes_cli/cli_tui_mixin.py:1819`.
- `_spinner_text`: none. All writers are in `hermes_cli/` stream and command code (see section b).
- The one visible output is the end-of-review summary line: `agent/background_review.py:1253-1258`, sent to the TUI as a `review.summary` event (`tui_gateway/session_registry.py:131`) and printed by `hermes_cli/gateway_agent.py:1097-1100` or, between turns, `hermes_cli/gateway_tools_notify.py:63-69`.
- Parent-agent state it touches: `_background_review_run`, `_background_review_agent` and `_active_children` (`agent/background_review.py:1093-1113`). The gateway's `session["running"]` is not set (`tui_gateway/session_lifecycle.py:524-529`).

### How new input supersedes it

- Every foreground `run_conversation` begins by cancelling a live review: `agent/turn_facade.py:35-41`.
- `cancel_background_review_for_live_turn` (`agent/background_review.py:129-167`) sets the cancel flag, hard-interrupts the fork from a helper thread (`agent/background_review.py:102-126`), then waits at most 2.0 s (`agent/background_review.py:25`, `:162`) before letting the live turn go ahead. This is the `Interrupt requested (hard): background review superseded` line.
- The gateway queue path does the same: `gateway/session_authority.py:1278-1287`.
- **Voice input takes the same route as typed input.** A transcript goes onto `_pending_input`, the process loop picks it up (`hermes_cli/cli_tui_runtime_mixin.py:41`), and `chat()` submits it. Nothing in the voice path knows the review exists.
- **Full-duplex barge-in** (`hermes_cli/cli_voice_mixin.py:350-433`) reads only `_agent_running`, `_voice_tts_done` and audio-output state (`:385-392`). Its generation-phase trigger calls `self.agent.interrupt()` only when `_agent_running` is true (`:416-418`), which targets the foreground turn. It does not see or cancel the review; the next submitted turn does that.
- Measured cost of superseding, from gateway inbound to the foreground turn start: 373 ms (00:07:58.707 to 00:07:59.080), 169 ms (00:18:47.061 to .230), 220 ms (01:02:10.873 to 01:02:11.093). No `did not acknowledge cancellation` warnings in the log.

## (b) Everything that can show "Working..." after the visible reply

"Working..." is the fallback text when `_agent_running` is true and `_spinner_text` is empty: `hermes_cli/cli_modern_layout.py:232-237`. The spinner rule shows when `_agent_running` or `_command_running` is true: `hermes_cli/cli_modern_layout.py:270-280`. The footer hints switch to interrupt/stop on `_agent_running`: `hermes_cli/cli_modern_layout.py:294-299`. The modern layout is the active one (`display.cli_layout` default `modern`, `hermes_cli/config_defaults.py:824`; `hermes_cli/cli_init_mixin.py:148`).

`_agent_running` covers the whole of `chat()`, not just generation: set at `hermes_cli/cli_tui_runtime_mixin.py:159`, cleared in `_tui_after_turn` at `:187`, which runs in the `finally` after `chat()` returns (`:163-166`).

| # | Cause | Evidence | Applies here? |
|---|---|---|---|
| 1 | **Streaming-TTS drain.** After the agent thread ends, `_chat_settle_turn` sends the end sentinel and joins the TTS thread for up to 120 s | `hermes_cli/cli_chat_turn_mixin.py:501-507`; the TTS thread exits only after playback finishes (`tools/tts_tool_speaker.py:398-406`) | **Yes. This is the cause.** `voice.auto_tts: true`, provider `kokoro-mlx` |
| 2 | Spinner text is cleared only in `_tui_after_turn`, so during the drain the last tool/status text or "Working..." stays up | `hermes_cli/cli_tui_runtime_mixin.py:188` | Yes, same window |
| 3 | A notification turn: a process or background-subagent completion is queued as a `TimelineNotification` and runs as a real turn | `hermes_cli/cli_process_notifications.py:68-84`, `hermes_cli/gateway_tools_notify.py:87`, idle tick at `hermes_cli/cli_tui_runtime_mixin.py:78` | Possible in general; a subagent (`20261113_002115_2fb872`) ran 00:21 to past 01:01 |
| 4 | A gateway-admitted turn the viewer adopts (`/queue`, loop tick, goal continuation) | `hermes_cli/gateway_agent.py:759-767`, `hermes_cli/cli_tui_runtime_mixin.py:79-80` | Not seen in the two windows |
| 5 | A slash or shell command in flight (`_command_running`) | `hermes_cli/cli_stream_mixin.py:620-628`, `hermes_cli/cli_modern_layout.py:234-235` | No |
| 6 | A `/bg` task writing `_spinner_text` while idle. This shows text but not the spinner rule, since the rule needs `busy` | `hermes_cli/cli_commands_mixin.py:1388-1391`, `:1425-1426` | No |
| 7 | End-of-turn reads in the gateway proxy (receipt, stats), each bounded by its own timeout | `hermes_cli/gateway_agent.py:851-853`, `:813` | Short |
| 8 | A fixed 0.15 s settle sleep | `hermes_cli/cli_chat_turn_mixin.py:510-511` | Negligible |

Not causes: the subagent monitor only reads `_agent_running` for display (`hermes_cli/cli_subagent_monitor.py:85`). The classic-layout spacer and spinner helpers at `hermes_cli/cli_status_bar_mixin.py:544-580` are driven by the same flag and are off in the modern layout (`:544`, `:551`).

### Why this breaks Ctrl+Space

The record-key handler, `hermes_cli/cli_tui_mixin.py:826-872`, runs in this order:

1. Recording active: stop and transcribe (`:826-833`).
2. `_agent_running or _voice_processing`: set `_voice_continuous = False` and return (`:836-840`).
3. Otherwise cut TTS, then start recording (`:846-871`).

With streaming TTS, "TTS still speaking" implies "`chat()` is still in the join" implies "`_agent_running` is true". So step 3's TTS cut cannot be reached while streaming TTS plays. It is reachable only for batch TTS (`hermes_cli/cli_chat_turn_mixin.py:607-608`) or after the 120 s join times out.

There is a second effect. Step 2 sets `_voice_continuous = False`, and the full-duplex listener stops as soon as that flag is false (`hermes_cli/cli_voice_mixin.py:385-387`). So one press of Ctrl+Space during playback also kills barge-in for the rest of that reply, and the reply keeps talking.

## (c) Log timelines

The CLI process does not log `_agent_running` or TTS playback start/end, so the "TTS still playing" rows are inferred.

### 00:07:04 to 00:07:59

| Time | Event |
|---|---|
| 00:06:56.704 | Foreground turn starts (voice input: "go on a long rant so I have the opportunity to interrupt you") |
| 00:07:04.079 | Foreground `Turn ended`, `response_len=1226` |
| 00:07:04.105 | Gateway `response ready ... time=7.5s` |
| 00:07:04.164 | Review turn starts, `origin=background_review` |
| 00:07:36.260 | `TTS CUT: streaming TTS prefetch failed mid-sentence` (speech still being produced 32 s after the reply ended) |
| 00:07:58.707 | User types: "i tried to barge in but did not work. ctrl+space did not work either" |
| 00:07:58.796 | `Interrupt requested (hard): background review superseded` |
| 00:07:59.029 | `Background review complete: thread=bg-review calls=0 ... result=none` |
| 00:07:59.080 | New foreground turn starts |

A 1226-character reply is over a minute of speech. The user's complaint is exactly the section (b) failure.

### 00:17:55 to 00:18:47

| Time | Event |
|---|---|
| 00:17:55.062 | Foreground `Turn ended`, `response_len=713` |
| 00:17:55.122 | Gateway `response ready ... time=46.8s` |
| 00:17:55.191 | Review turn starts (skill review) |
| 00:18:47.061 | User types "i meant fable-5.2 as the model for the subagent" |
| 00:18:47.142 | Review superseded; complete at .191 with `calls=0` |
| 00:18:47.230 | New foreground turn starts |
| 00:18:48.582 | `Voice recording started` |

Note: this TUI was restarted at about 00:19 (pid 97973 started 00:19; plugin registration lines at 00:19:27), so these two windows are from the previous TUI process.

### 01:01:14 to 01:02:11

| Time | Event |
|---|---|
| 01:01:14.808 | Foreground `Turn ended`, `response_len=388` |
| 01:01:14.942 | Review turn starts |
| 01:01:59.800 | `Voice recording started` |
| 01:02:10.873 | Voice message arrives |
| 01:02:10.945 | Review superseded after 3 API calls, `result=skill` (it had already written a skill) |
| 01:02:11.093 | Next turn starts with "[Note: the user interrupted your previous spoken reply before it finished.]" |

That note is set only by the record-key TTS cut (`hermes_cli/cli_tui_mixin.py:851`) or the barge-in playback trigger (`hermes_cli/cli_voice_mixin.py:404`). So speech was still being cut tens of seconds after the reply text had finished.

### Reviews that were not superseded

Reviews ran 30 s to 2 min 23 s (for example 00:04:08 to 00:06:31, 00:24:30 to 00:26:26). In session `S`: 9 reviews, 3 superseded.

## (d) Config knobs and current values

Values read with `hermes config get <key>`.

| Key | Current | Default and source | Effect |
|---|---|---|---|
| `auxiliary.background_review.enabled` | `true` | `true`, `hermes_cli/config_defaults.py:795` | `false` stops automatic reviews; `/refine` still works (`run_agent.py:763-766`) |
| `auxiliary.background_review.provider` / `.model` | `auto` / `''` | `hermes_cli/config_defaults_auxiliary.py:10` | `auto` means the main model replays the whole conversation from warm cache; another model gets a short digest (`agent/background_review.py:170-172`) |
| `auxiliary.background_review.timeout` | `120` | `config_defaults.py:795` | Per-request timeout |
| `auxiliary.background_review.reasoning_effort` | `''` | same | Ignored while the review stays on the main model (`config_defaults.py:791-794`) |
| `auxiliary.background_review.max_input_tokens` | not set | derived from context window (`agent/background_review.py:173-183`) | Caps total input tokens over the review loop |
| `auxiliary.background_review.defer` | not set (`auto`) | `agent/review_idle_queue.py:31-34` | Idle-deferral, **only** when the review targets the managed local llama-server (`run_agent.py:172-175`). No effect on Anthropic |
| `auxiliary.background_review.defer_max_age_s` | not set (1800) | `agent/review_idle_queue.py:27`, `:37-42` | Max wait for a deferred review |
| `memory.nudge_interval` | `10` | `10`, `config_defaults.py:1306` | User turns between memory reviews; `0` disables (`agent/turn_context.py:821`) |
| `skills.creation_nudge_interval` | not set (10) | hard-coded `10`, `agent/agent_init.py:1236-1238`; not in `config_defaults.py` | Tool iterations between skill reviews; `0` disables (`agent/turn_finalizer.py:864`) |
| `memory.memory_enabled` / `user_profile_enabled` | `true` / `true` | `config_defaults.py` memory block | Memory review needs a memory store (`agent/turn_context.py:822-823`) |
| `memory.write_approval` | `false` | `config_defaults.py:1302` | `true` stages review writes for approval |
| `display.memory_notifications` | `on` | `config_defaults.py:846` | Controls the review summary line |
| `curator.*` | `enabled: true`, `interval_hours: 168`, `min_idle_hours: 2`, `consolidate: false` | `config_defaults.py:1482-1490` | A different job (weekly skill pruning). Not the post-turn review |
| Fixed in code | n/a | `agent/background_review.py:25`, `:173` | 2.0 s cancel wait; 16 iterations max |
| `voice.auto_tts`, `voice.barge_in`, `tts.provider` | `true`, `true`, `kokoro-mlx` | n/a | The settings that produce the drain window |

There is no knob that ties review to voice mode.

## (e) Recommendation

### 1. Fix the voice busy check, but not by looking at background review

Distinguishing "background review" from "foreground turn" in the busy check would change nothing: the review never sets the flag. The distinction that matters is **agent still generating** versus **reply finished, TTS draining**.

Recommended signal: **a new explicit flag**, for example `self._turn_tts_draining`, set to `True` in `_chat_settle_turn` just before the TTS join (`hermes_cli/cli_chat_turn_mixin.py:501-504`) and cleared after the join and again in `_tui_after_turn`.

Then in `_tui_handle_voice_record`, before the busy check at `hermes_cli/cli_tui_mixin.py:836`: if the flag is set, run the existing TTS-cut block (`:846-857`), keep `_voice_continuous` true, and start recording.

Why not the existing attributes:

| Candidate | Problem |
|---|---|
| `_interactive_turn` | Set and cleared together with `_agent_running` (`hermes_cli/cli_tui_runtime_mixin.py:159`, `:194`). Same lifetime. It only gates the turn-summary line (`hermes_cli/cli_status_bar_mixin.py:606`) |
| Turn origin (`_turn_origin`) | Lives on the review fork in the gateway process (`agent/background_review.py:1005`). The CLI cannot see it, and it is not the cause |
| `not _voice_tts_done.is_set()` | Cleared when the TTS thread starts, at turn start (`tools/tts_tool_speaker.py:340`). It is unset during generation as well as playback |
| `_agent_running and _prompt_start_time is None` | Works today (`hermes_cli/cli_chat_turn_mixin.py:488-490`) but is an accident of the timer code. Fragile |
| `is_audio_output_active()` (`tools/voice_mode.py:432`) | Usable as a second condition, but false in the gaps between sentences |

Also change the "busy" branch so that Ctrl+Space during playback no longer sets `_voice_continuous = False`. That flag is what the barge-in listener polls (`hermes_cli/cli_voice_mixin.py:385-387`).

Smaller, separate UI point: while draining, `activity_text` could say "Speaking..." in place of "Working..." (`hermes_cli/cli_modern_layout.py:237`), since the flag would exist.

### 2. Background review during voice mode: leave it on for now

Arguments for skipping or deferring it in voice mode:

- Voice replies are short and frequent, so reviews are superseded more often (3 of 9 here). A superseded review that had a request in flight for 52 to 55 s is wasted spend.
- It adds 0.2 to 0.4 s (worst case 2.0 s) to the next turn's start.
- It shares the provider's rate limit with the foreground.

Arguments against:

- It is not the cause of the reported symptom. Turning it off would not fix "Working..." or Ctrl+Space.
- Voice sessions would stop learning skills and memory.
- A config-only alternative exists: raise `memory.nudge_interval` and set `skills.creation_nudge_interval`, or route the review to a cheaper model with `auxiliary.background_review.model`.

If deferral is wanted later, the existing idle queue (`agent/review_idle_queue.py`) is the place. It already waits for a 15 s quiet window (`agent/review_idle_queue.py:25`) but only for the managed local runtime.

## (f) Side effects and risks

| Option | Risks |
|---|---|
| **A. New drain flag + cut TTS on Ctrl+Space (recommended)** | Recording starts while `chat()` is still unwinding; `_voice_start_recording` has no `_agent_running` guard (`hermes_cli/cli_voice_mixin.py:62-107`), so it starts, but the full-duplex listener may still hold the mic for a moment (`:385-392`). Needs a check for two captures at once. Setting the stop event marks the reply as interrupted, so the next turn carries the "user interrupted" note (`hermes_cli/cli_chat_turn_mixin.py:327-329`); that is the intended meaning. The join returns with the thread dead, so `tts_normal_exit` is true and the release path does not double-cut (`hermes_cli/cli_chat_turn_mixin.py:505-507`, `:154-156`). Patch must be carried on `dg/local-patches` across upstream updates |
| **B. Reuse `_prompt_start_time is None`** | No new state, but breaks silently if the timer code is reordered |
| **C. Stop joining TTS inside `chat()`** (return at once, let TTS play with `_agent_running` false) | Makes the existing cut path at `cli_tui_mixin.py:846` reachable with no handler change, and removes "Working..." during speech. But the join is what keeps continuous mode from reopening the mic over its own voice, orders the response panel, and bounds a runaway TTS worker. Wider behaviour change; the wake-word and no-speech logic that read `_agent_running` (`hermes_cli/cli_voice_mixin.py:263-265`, `:670`, `:735-739`) already also check `_voice_tts_done`, but each would need re-testing |
| **D. Make the busy branch distinguish background review** | No effect. Wrong diagnosis |
| **E. Disable review (`auxiliary.background_review.enabled: false`)** | Does not fix the symptom. Stops automatic skill and memory capture in every session, not just voice |
| **F. Skip or defer review while voice mode is on** | Does not fix the symptom. Needs the CLI's voice state passed to the gateway, which is a new cross-process signal. Deferred reviews can pile up behind a long voice chat, and a later review of a stale snapshot may be superseded anyway |
| **G. Raise nudge intervals** | Config only, reversible. Fewer reviews everywhere. Does not fix the symptom |

## Open points

- Why VAD barge-in itself did not trip at 00:07 is outside this sweep. The repeated `TTS CUT: streaming TTS prefetch failed mid-sentence` warnings (00:07:36, 00:09:19, 00:14:00 to 00:14:03, 00:15:52 to 00:15:53) have an empty error string and deserve a look.
- A debug log line at the TTS join (start, end, duration) would turn the inferred rows in section (c) into measured ones.
