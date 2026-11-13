"""Ctrl+C / Ctrl+Q stop TTS in the classic CLI without exiting or poisoning the next turn.

Plan-v2 W4 (DG local patch). The handlers never touched playback: during the streaming-TTS tail
Ctrl+C hard-interrupted an agent that had already finished (and armed the 2 s force exit), and
with batch TTS and an idle agent it went straight to "clear or exit" and quit the app on an empty
prompt, leaving the reply playing. Now pending or audible TTS is cut first; while the model is
still generating the press then continues to the normal interrupt.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.buffer import Buffer

import hermes_cli.cli_tui_mixin as tui_mod
from cli import HermesCLI


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(stops=0, hard_interrupts=[])

    def _stop():
        state.stops += 1
    monkeypatch.setattr("tools.voice_mode.stop_playback", _stop)
    monkeypatch.setattr("tools.voice_mode.is_audio_output_active", lambda: False)
    monkeypatch.setattr("tools.tts_streaming.mark_speech_interrupted", lambda: None)
    monkeypatch.setattr(tui_mod, "request_hard_interrupt", state.hard_interrupts.append)
    return state


def _cli(*, voice_mode=True, agent_running=False, generating=None):
    cli = HermesCLI.__new__(HermesCLI)
    cli._voice_lock = threading.Lock()
    cli._voice_mode = voice_mode
    cli._voice_recording = False
    cli._voice_recorder = None
    cli._voice_continuous = True
    cli._voice_tts_done = threading.Event()
    cli._voice_tts_done.set()  # nothing pending
    cli._voice_tts_stop = None
    cli._agent_running = agent_running
    cli.agent = MagicMock()
    cli._attached_images = []
    cli._last_ctrl_c_time = 0.0
    cli._should_exit = False
    cli._pending_input = queue.Queue()
    cli._backtrack_state = None
    cli._backtrack_cancel_edit = lambda: False
    for name in ("_slash_confirm_state", "_model_picker_state", "_command_palette_state", "_gateway_command_wait",
                 "_sudo_state", "_secret_state", "_approval_state", "_clarify_state", "_connection_state"):
        setattr(cli, name, None)
    cli._turn_agent_thread = generating
    return cli


def _speaking(cli):
    """A reply is being spoken: done clear, live stop event."""
    cli._voice_tts_done.clear()
    cli._voice_tts_stop = threading.Event()
    return cli._voice_tts_stop


def _event(cli, text=""):
    buffer = Buffer()
    buffer.text = text
    app = SimpleNamespace(current_buffer=buffer, invalidate=lambda: None,
                          exit=lambda: setattr(cli, "_should_exit", True))
    return SimpleNamespace(app=app)


@pytest.fixture
def live_turn():
    done = threading.Event()
    thread = threading.Thread(target=done.wait, args=(30,), daemon=True)
    thread.start()
    yield thread
    done.set()


HANDLERS = ["_tui_handle_ctrl_c", "_tui_handle_ctrl_q"]


@pytest.mark.parametrize("handler", HANDLERS)
def test_batch_speech_with_idle_agent_is_cut_and_the_app_stays_open(env, handler):
    """Review §4.7."""
    cli = _cli()
    stop = _speaking(cli)
    getattr(cli, handler)(_event(cli))
    assert stop.is_set() and env.stops >= 1 and cli._voice_tts_done.is_set()
    assert not cli._should_exit
    assert env.hard_interrupts == []


@pytest.mark.parametrize("handler", HANDLERS)
def test_playback_tail_is_cut_without_a_hard_interrupt(env, handler):
    """S1 §f.8: the model finished, chat() is still draining the speech."""
    cli = _cli(agent_running=True)
    stop = _speaking(cli)
    getattr(cli, handler)(_event(cli))
    assert stop.is_set()
    assert env.hard_interrupts == []
    assert cli._last_ctrl_c_time == 0.0  # no 2 s force-exit arming
    assert not cli._should_exit


@pytest.mark.parametrize("handler", HANDLERS)
def test_ctrl_key_while_generating_cuts_and_interrupts(env, handler, live_turn):
    """S1 §f.9."""
    cli = _cli(agent_running=True, generating=live_turn)
    stop = _speaking(cli)
    getattr(cli, handler)(_event(cli))
    assert stop.is_set()
    assert env.hard_interrupts == [cli.agent]


def test_ctrl_c_with_a_typed_draft_and_speech_cuts_first_then_clears(env):
    """Review §4.8: the first press only cuts the speech; the second clears the draft."""
    cli = _cli()
    stop = _speaking(cli)
    event = _event(cli, "half typed")
    cli._tui_handle_ctrl_c(event)
    assert stop.is_set()
    assert event.app.current_buffer.text == "half typed"
    cli._tui_handle_ctrl_c(event)
    assert event.app.current_buffer.text == ""
    assert not cli._should_exit


@pytest.mark.parametrize("handler", HANDLERS)
def test_no_speech_keeps_todays_behaviour(env, handler):
    cli = _cli()
    getattr(cli, handler)(_event(cli))
    assert cli._should_exit  # idle, empty prompt: exit, as before
    assert env.stops == 0


def test_outside_voice_mode_ctrl_c_never_cuts(env):
    cli = _cli(voice_mode=False)
    _speaking(cli)
    cli._tui_handle_ctrl_c(_event(cli, "draft"))
    assert env.stops == 0


def test_backtrack_picker_override_still_wins(env):
    cli = _cli()
    stop = _speaking(cli)
    closed = []
    cli._backtrack_state = {"open": True}
    cli._close_backtrack_picker = lambda: closed.append(True)
    cli._tui_handle_ctrl_c(_event(cli))
    assert closed == [True]
    assert not stop.is_set()
