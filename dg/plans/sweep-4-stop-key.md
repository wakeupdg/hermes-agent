# Sweep 4: a dedicated "stop the voice" key

Read-only research. Fork `dg/local-patches` at `f619f23fc5` (W0 and W1 committed; `cli_chat_turn_mixin.py` was dirty because another agent is editing it). The install `~/.hermes/hermes-agent` is `v0.23.1`. prompt_toolkit is **3.0.53** (`~/.hermes/installs/ef88449a58b4d62f/environments/*/venv`). Line numbers refer to the fork unless they are marked `pt:` (prompt_toolkit) or `install:`.

## TL;DR
- **Blocker found (fix it first):** killing `afplay` does not stop the sentence. `_run_system_player` treats a SIGTERM exit (rc −15) as "player failed" and falls through to **`ffplay`**, which is installed at `/opt/homebrew/bin/ffplay` and replays the **same sentence from the start**. Hermes's own `stop_playback()` hits the same path. That breaks every cutter in plan-v2 and the `pkill afplay` workaround. I reproduced it (§4.1).
- **Key:** use **Escape**, bound `eager=True` with a filter so it acts **only while TTS is pending or audible**. At every other moment, Escape behaves exactly as it does today. Its latency is about 0.5 s (prompt_toolkit's `ttimeoutlen`). Re-feed the Escape when another key from the same read is queued, so Alt+Enter and other Alt combinations keep working. I verified this on pt 3.0.53 (§3.3). Hard-code Escape for now; add `voice.stop_key` only if DG ever wants a different key.
- **Interim workaround (no Hermes code):** Hammerspoon is installed and running (pid 968), but it has no `init.lua` yet. Bind a global hotkey to a short loop that kills `afplay` **and** `ffplay` until nothing has played for about 1.5 s. A single `pkill -x afplay` is not enough today: the sentence restarts in ffplay, and the next sentences keep playing.

---

## 1. Current keybindings (classic TUI)

`_tui_build_key_bindings`, `hermes_cli/cli_tui_mixin.py:1885-1951` (+ `_tui_bind_editor_and_stash` :1953-1977, `_tui_bind_overlay_navigation` :1979-2044):

| Key | Filter | Action | Line |
|---|---|---|---|
| `Keys.Ignore` | eager | swallow focus reports | 1902 |
| Enter / submit keys | — | `_bind_prompt_submit_keys` | 1903 |
| **Esc, Enter** (Alt+Enter) | — | newline | 1905 |
| c-j | multiline shortcuts | newline | 1910 |
| up / down | `_normal_input` | history | 1921-1922 |
| c-l / c-c / c-q / c-d / c-z | — | clear / interrupt-clear-exit / ctrl_q / delete-or-exit / suspend | 1923-1934 |
| **Esc** | `_modal_prompt_active` (secret, sudo, slash-confirm, connection), **eager** | `_tui_handle_escape_modal` (cancel) :1078 | 1932 |
| **Esc, Esc** | not modal | `_tui_handle_double_escape`: discard the draft and images (saved to history) :1045 | 1933 |
| record key (`voice.record_key`; DG uses `ctrl+space`, `~/.hermes/config.yaml:66`) | — | `_tui_handle_voice_record` :817 | 1936 |
| BracketedPaste (eager), c-v, **Esc, v** (Alt+V) | — | paste / image paste | 1937-1939 |
| c-t, f6 / c-r, f7 | not modal | subagent monitor / dock | 1941-1950 |
| c-g, **Esc, g** | editor filter | open in editor | 1959-1960 |
| c-s | stash filter | stash prompt | 1969 |
| up/down/enter/d/D, **Esc** | stash panel open, eager | stash panel nav / **close** | 1970-1976 |
| tab | eager | complete / suggestion | 1977 |
| clarify up/down/space/tab/s-tab/0-9 | `_clarify_nav` | clarify (no Esc binding) | 1981-2002 |
| approval / slash-confirm up/down/0-9 | state | (no Esc binding) | 2007-2010, 2039-2044 |
| model picker up/down/typing/backspace, **Esc** | `_picker`, eager | picker close | 2011-2023 |
| c-p, palette nav, **Esc** | palette, eager | palette close | 2026-2037 |

Configurable keys:
- `voice.record_key` is the only configurable key. Its default is `ctrl+b` (`hermes_cli/config_defaults.py:1212`).
- It is resolved in `cli_tui_mixin.py:2046-2090` through `voice_record_key_from_config` / `normalize_voice_record_key_for_prompt_toolkit` / `pt_key_to_sequence` (`hermes_cli/voice.py:43-96`).
- The normalizer requires **exactly one modifier plus a key** (`voice.py:72-74`). It rejects bare `escape` and F-keys, and maps them to `c-b`. So a `voice.stop_key` cannot reuse it as-is.
- There is no `voice.stop_key` today.

What Escape does today:
- **Editing mode:** emacs by default; vi when `display.vim_mode` is on (`cli_tui_runtime_mixin.py:436-439`). DG has `display.vim_mode: false`.
- **Default emacs bindings:** prompt_toolkit's emacs bindings ignore a lone Esc (pt: `key_binding/bindings/emacs.py:57`). Esc is also the meta prefix for Esc+b/f/c/d/l/u/y/backspace/… (emacs.py:78-86) and `Esc c-] Any` (:182).
- **Parser delay:** a lone `\x1b` waits **`ttimeoutlen = 0.5 s`** in the input parser before it becomes an Escape key (pt: `application/application.py:288, 716-722`). Hermes never overrides `ttimeoutlen` or `timeoutlen` (no matches in the repo).
- **Key-processor delay:** a lone Escape is a prefix of longer bindings, so the key processor waits a further **`timeoutlen = 1.0 s`** (pt: `application.py:297`, `key_processor.py:384-393`). The exception is an **eager** match, which fires at once and drops the longer candidates (pt: `key_processor.py:179-185`).
- **Net effect today:** a single Esc does nothing visible (about 1.5 s later it hits emacs ignore). Esc Esc clears the draft. In modal prompts, the stash panel, the picker and the palette, Esc closes or cancels after about 0.5 s.
- Completion menus have no Esc binding in Hermes.

## 2. Is Escape safe? Alternatives

| Candidate | Verdict |
|---|---|
| **Escape, eager, filtered on "TTS cuttable"** | **Best.** It is DG's preference and the universal "stop" key. Outside TTS the filter is false, so behaviour is byte-identical to today. Latency is 0.5 s (parser `ttimeoutlen`); it is not 1.5 s because the binding is eager. Collateral during playback: the first Esc only cuts audio. A second Esc does the normal thing (closes the overlay, or the first half of Esc Esc). Alt combinations survive through the re-feed (§3.3). |
| Ctrl+\\ | Zero latency, and it is unbound in Hermes (pt basic.py ignores it). But raw mode clears ISIG (pt: `input/vt100.py:268`). Whenever the terminal is cooked (Ctrl+Z suspend, external editor, `run_in_terminal`, startup and exit), Ctrl+\\ sends **SIGQUIT and kills Hermes**. Reject. |
| Ctrl+] | prompt_toolkit's emacs mode binds `c-] Any` (character search, pt: emacs.py:176), so it is ambiguous and needs eager. It is hard to reach on some layouts. A reasonable fallback only. |
| Ctrl+X | Prefix of emacs `c-x c-e`, `c-x r y`, `c-x (`… (pt: emacs.py:76-115, open_in_editor.py:37). It would shadow those. Reject. |
| F-keys | Mac keyboards send media keys unless Fn is held (F8 = Play/Pause!). F6 and F7 are already used. Delivery depends on the terminal. Reject as the primary key. |
| Ctrl+Space double-tap | Under plan-v2, Ctrl+Space already cuts **and starts recording** (W3.4). A double-tap gesture would fight its own press semantics. Reject. |

Terminals:
- Terminal.app, iTerm2 and Ghostty all send a bare `\x1b` for Esc.
- Orca is DG's terminal (`terminal.client_env_passthrough: ORCA_AGENT_PANE`). Its terminal is presumably xterm.js-style; I did not verify that it does not intercept Esc. Check with `cat -v`, then press Esc; it should show `^[`.
- tmux: not installed (`which tmux` is empty). If DG ever uses it, tmux's `escape-time` (default 500 ms in older versions, 10 ms in 3.5) adds to the latency.
- Kitty keyboard protocol: Ghostty supports it, but prompt_toolkit does not enable it, so it changes nothing.
- Optional latency trim: lower `app.ttimeoutlen` to about 0.1 s for local terminals. This affects every Alt-combo parse, so leave it out of the minimal change.

**Config recommendation:** hard-code Escape. The binding is inert unless TTS is active, so it needs no opt-out. Add `voice.stop_key` (default `escape`, with its own small parser that accepts bare `escape` / `f1-f24` / `ctrl+<x>`) only if someone asks for another key.

## 3. Minimal change on top of plan-v2

### 3.0 Prerequisite: no fall-through to the next player after a kill (`tools/voice_mode.py:1108-1113`)
```python
rc = proc.returncode
if rc == 0:
    return True
if rc is not None and rc < 0:      # killed by a signal: stop_playback() / external pkill — a stop, not a failure
    logger.debug("System player %s stopped by signal %d", cmd[0], -rc)
    return True                    # do NOT try ffplay/aplay with the same file
```
- Players that genuinely fail exit with a positive rc, so the existing fall-through test (`tests/tools/test_voice_mode.py:1422`, rc=1) is unaffected.
- This belongs in W1 (pipeline) of plan-v2. Without it, Ctrl+Space, Ctrl+C, barge-in and Esc all "cut" and then hear the sentence again through ffplay.

### 3.1 Predicate and handler in `hermes_cli/cli_voice_mixin.py` (beside W3's `_voice_cut_tts`)
W3.4 already defines "pending or audible". Name it once so the record key, Ctrl+C/Q (W4) and Esc share it:
```python
def _voice_tts_cuttable(self) -> bool:
    """TTS pending or audible: done-event clear, audio on the speakers, or a live un-set stop event."""
    done = getattr(self, "_voice_tts_done", None)
    stop = getattr(self, "_voice_tts_stop", None)
    try:
        from tools.voice_mode import is_audio_output_active
        audible = is_audio_output_active()
    except Exception:
        audible = False
    return bool((done is not None and not done.is_set()) or audible or (stop is not None and not stop.is_set()))

def _voice_stop_key_pressed(self, event) -> None:
    """Esc while TTS plays: cut audio only. Re-feed the Esc when the same read carried another
    key (Alt+Enter = ESC CR, Alt+V, unknown CSI) so Alt-combos still reach their bindings."""
    self._voice_cut_tts("stop_key")          # plan-v2 W3.2: sets stop, mark_speech_interrupted, stop_playback, sets _voice_tts_done
    kp = event.key_processor
    if kp.input_queue and kp.input_queue[0].key != Keys.Escape:
        from prompt_toolkit.key_binding.key_processor import KeyPress
        kp.feed(KeyPress(Keys.Escape, "\x1b"), first=True)   # filter is now False → normal prefix matching
    event.app.invalidate()
```
- `_voice_cut_tts` must set `_voice_tts_done` (or the stop event) **synchronously**. Otherwise the filter stays true and the re-fed Esc loops back into this handler.
- The handler is cheap and non-blocking. `stop_playback()` only calls `terminate()` (`tools/voice_mode.py:991-1003`), so it is safe on the UI thread, including while the TUI shows "Working…" (key bindings run on the event loop; the agent runs on its own thread).

### 3.2 One binding in `cli_tui_mixin.py` (net +1 line, keeps the size ratchet)
Register it **after** every other Escape binding. Among eager matches the last-registered one wins (`matches[-1]`, pt: `key_processor.py:188-189`). Right after line 1933 works, because the stash, picker and palette Esc bindings are registered earlier (:1912-1913 → :1976, 2023, 2035):
```python
kb.add('escape', filter=Condition(self._voice_tts_cuttable), eager=True)(self._voice_stop_key_pressed)
```
- **Filter:** cuttable only. Do not gate on `_voice_mode`: `/voice tts` and `auto_tts` can speak without it.
- **Modals:** while TTS plays, a modal Esc cuts audio first and cancels on the second press. If DG prefers "cut and cancel in one press", also re-feed when `_modal_prompt_active`, the picker, the palette or the stash panel is open (one extra `or`).
- **Vi mode:** the first Esc during TTS cuts audio and stays in insert mode; the second Esc enters navigation mode.

### 3.3 Verified dispatch (pt 3.0.53)
`$TMPDIR/probe_esc_refeed.py`: a KeyProcessor with Esc+Enter, Esc+Esc, Enter and the eager filtered Esc binding, run in an event loop.
```
Esc alone          : ['CUT_TTS']
Alt+Enter (ESC,CR) : ['CUT_TTS', 'refeed', 'alt-enter:newline']
Esc Esc same read  : ['CUT_TTS', 'single-esc(default)']      # draft NOT cleared by a hammered Esc
```
Without the re-feed, Alt+Enter during playback **submitted** the draft (first probe run, before the fix). Use `event.key_processor`, not `event.app.key_processor`.

### 3.4 Ink TUI (`hermes --tui`), only if DG ever uses it
- **Today:**
  - Esc Esc while busy → `turnController.interruptTurn` → `session.interrupt` → `_tts_stream_stop()` (`ui-tui/src/app/useInputHandlers.ts:139-156`; `tui_gateway/methods_session_interrupt.py:72`). That also interrupts the turn.
  - After the turn ends (`busy=false`), a single Esc or Esc Esc does not stop the drain (`resolveDoubleEscAction` → `'none'`, `useInputHandlers.ts:111-120`).
- **Needed:**
  - Add a `voice.stop_tts` RPC in `tui_gateway/methods_voice.py` that calls `_tts_stream_stop(user_barge=True)` (:194-211).
  - Emit TTS-active state, or have the client track `voice.status`.
  - In `useInput` (`useInputHandlers.ts:689`), before `handleEscBacktrack`: `if (key.escape && !isVoiceKey && ttsActive) { gw.request('voice.stop_tts'); return }`.
- Out of scope for the classic-TUI build.

## 4. Interim, system-wide, no Hermes code

### 4.1 What `pkill -x afplay` actually does today (reproduced)
`$TMPDIR/probe_kill_afplay.py` plays a 3 s silent WAV through the install's `tools.voice_mode.play_audio_file` and spies on `Popen`:
```
sigterm returned True after 5.25 s; players launched: [(0.56, 'afplay', …), (0.98, 'ffplay', …)]
stop    returned True after 5.39 s; players launched: [(0.47, 'afplay', …), (0.98, 'ffplay', …)]
```
The path, step by step:
1. The killed afplay returns rc −15.
2. `_run_system_player` logs "trying next" and returns False (`tools/voice_mode.py:1109-1113`).
3. `_play_audio_file_impl` then runs `ffplay -nodisp -autoexit <same file>` (:1092, :1133-1135), so **the same sentence plays again in full**.
4. Even when ffplay is not involved, the speaker consumer only skips a sentence when `stop_event` is set (`tools/tts_tool_speaker.py:256-260`; sync path :148). Killing the player therefore **skips to the next sentence**. It does not stop the reply.

### 4.2 Workaround that works today
Tools present:
- **Hammerspoon** (`/Applications`, running, `hs` at `/opt/homebrew/bin/hs`; `~/.hammerspoon` has only `Spoons/`, no `init.lua`).
- Raycast (running).
- Shortcuts (`/usr/bin/shortcuts`).
- skhd, BetterTouchTool and Karabiner are not installed.

Add this to `~/.hammerspoon/init.lua`. It is untested here because creating that config is DG's call.
```lua
-- Hyper-S: silence Hermes TTS now and keep killing until the queued sentences are gone.
hs.hotkey.bind({"ctrl","alt","cmd"}, "s", function()
  hs.task.new("/bin/sh", nil, {"-c",
    'q=0; i=0; while [ $q -lt 10 ] && [ $i -lt 200 ]; do ' ..
    'if pkill -x "afplay|ffplay"; then q=0; else q=$((q+1)); fi; ' ..
    'i=$((i+1)); sleep 0.15; done'}):start()
end)
```
- The loop ends after about 1.5 s with no player alive (30 s hard cap). It catches the ffplay fall-through and every already-synthesized sentence.
- Side effects:
  - It also kills Hermes beeps and any other `afplay`/`ffplay` on the Mac.
  - Hermes does not learn the reply was interrupted (no `mark_speech_interrupted`).
  - If generation is still running, sentences produced after the loop ends will play. Press the hotkey again.
- Hotkeys registered this way need no Accessibility permission.
- Raycast Script Command or Shortcuts "Run Shell Script" with the same body also works, but launches more slowly.

### 4.3 A cleaner external trigger (after code)
1. After §3.0, `pkill -x afplay` cleanly skips one sentence.
2. To stop **all** sentences from outside, the TUI must set the stop event:
   - Add `SIGUSR2` → `threading.Thread(target=self._voice_cut_tts, args=("external",), daemon=True).start()` in `_tui_install_signal_handlers` (`cli_tui_runtime_mixin.py:459-474`).
   - Add the same in `tui_gateway` → `_tts_stream_stop(True)`.
   - Target the process that is actually playing: `kill -USR2 $(ps -o ppid= -p $(pgrep -x afplay | head -1))`.
3. Do **not** use SIGUSR1, which is the gateway's drain-restart signal (`hermes_cli/gateway.py:259-276`).
4. Do not send USR2 before the handler ships: the default action **terminates** the process.

## 5. Tests (new files; follow plan-v2 W6 rules)
1. `tests/tools/test_voice_player_signal_stop.py`:
   - fake `Popen` with `returncode=-15` → `play_audio_file` launches exactly one player and does not call ffplay;
   - `returncode=1` still falls through (guards the existing behaviour);
   - `stop_playback()` mid-play → exactly one player.
   - Red on base: today's code launches ffplay.
2. `tests/hermes_cli/test_cli_voice_stop_key.py` (uses the `HermesCLI.__new__` stub; drives `kb` through a real `KeyProcessor` inside an asyncio loop, like the probe):
   - cuttable + Esc → `_voice_cut_tts("stop_key")` once, draft untouched;
   - not cuttable + Esc Esc → draft cleared (unchanged behaviour);
   - cuttable + [Esc, Enter] in one feed → cut **and** newline inserted, not submitted;
   - cuttable + [Esc, Esc] in one feed → cut, draft not cleared;
   - cuttable + modal or picker open → the first Esc cuts only and the modal stays open; the second Esc cancels;
   - the filter flips false right after the cut (no re-entry);
   - `_voice_tts_cuttable` covers the three sources (done clear / audible / live stop event).
3. Extend W4's Ctrl+C/Q tests to use the shared `_voice_tts_cuttable`.
4. DG's live checklist (add to plan-v2):
   - Esc during a long reply → silence within about 0.6 s, no replay of the sentence, the app stays open, and continuous mode follows its normal rules;
   - Esc while "Working…" before the first sentence → nothing is spoken for that turn;
   - Alt+Enter during playback inserts a newline.

## Evidence index
- Keybindings: `hermes_cli/cli_tui_mixin.py:1045-1095, 1885-2090`
- Record key: `hermes_cli/voice.py:43-109`, `hermes_cli/config_defaults.py:1212`
- prompt_toolkit 3.0.53: `application.py:288, 297, 716-722`; `key_processor.py:122-215, 209-219 (feed first=True), 230-287 (feed inside a binding is supported)`; `bindings/emacs.py:57, 76-115, 176-182`; `bindings/basic.py` (ignores c-\\, c-], f1-f24); `input/vt100.py:268`
- Player: `tools/voice_mode.py:981-1003, 1012-1020, 1085-1137`; `tools/tts_tool_speaker.py:45-69, 143-154, 256-260, 305-309, 331-406`
- Ink: `ui-tui/src/app/useInputHandlers.ts:102-160, 231-250, 689-715, 840-858`; `tui_gateway/methods_voice.py:194-211, 886-896`; `tui_gateway/methods_session_interrupt.py:70-72`
- Probes (scratch, not committed): `~/.hermes/cache/scratch/probe_kill_afplay.py`, `probe_esc_refeed.py`
