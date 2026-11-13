"""Escape stops TTS in the classic CLI (plan-v2 W3b, from sweep-4).

Bound eager with a filter, so it acts only while TTS is pending or audible; at every other moment
Escape behaves as before (Esc Esc clears the draft, modal Esc cancels, Alt+<key> combinations).
These tests drive the real Hermes key bindings through a real prompt_toolkit Application reading
a pipe, so the VT100 parser's escape timeout and the key processor's prefix matching are real.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from cli import HermesCLI
from tests.waiting import async_wait_until

_STATES = ("_clarify_state", "_approval_state", "_slash_confirm_state", "_sudo_state", "_secret_state",
           "_connection_state", "_model_picker_state", "_command_palette_state", "_clarify_freetext")


@pytest.fixture
def audio(monkeypatch):
    state = SimpleNamespace(active=False, stops=0)
    monkeypatch.setattr("tools.voice_mode.is_audio_output_active", lambda: state.active)

    def _stop():
        state.stops += 1
        state.active = False
    monkeypatch.setattr("tools.voice_mode.stop_playback", _stop)
    monkeypatch.setattr("tools.tts_streaming.mark_speech_interrupted", lambda: None)
    return state


def _cli(*, speaking):
    cli = HermesCLI.__new__(HermesCLI)
    cli.config = {}
    for name in _STATES:
        setattr(cli, name, None)
    cli._prompt_stash = SimpleNamespace(panel_open=False, __len__=lambda: 0)
    cli._attached_images = []
    cli._agent_running = False
    cli._voice_continuous = False
    cli._voice_tts_done = threading.Event()
    cli._voice_tts_stop = None
    if speaking:
        cli._voice_tts_stop = threading.Event()  # a live pipeline (streaming or batch)
    else:
        cli._voice_tts_done.set()
    cli.submitted = []
    cli._tui_handle_enter = lambda event: cli.submitted.append(event.current_buffer.text)
    cli.modal_cancels = []
    cli._tui_handle_escape_modal = lambda event: cli.modal_cancels.append(True)
    return cli


def _drive(cli, script):
    """Run the real key bindings; *script(send, app, buf)* is an async scenario."""
    kb = cli._tui_build_key_bindings()

    async def main():
        with create_pipe_input() as pipe:
            buf = Buffer(multiline=True)
            buf.text = "draft"
            buf.cursor_position = len(buf.text)
            app = Application(layout=Layout(Window(BufferControl(buffer=buf))), key_bindings=kb,
                              input=pipe, output=DummyOutput())
            pressed = []  # one entry per key the processor received (flushes excluded)
            app.key_processor.before_key_press += lambda _kp: pressed.append(1)
            task = asyncio.ensure_future(app.run_async())
            try:
                await async_wait_until(lambda: app.is_running, what="app running")
                await script(pipe.send_text, app, buf, pressed)
            finally:
                app.exit()
                await task

    asyncio.run(main())


def _idle(app):
    return not app.key_processor.key_buffer and not app.key_processor.input_queue


def test_escape_alone_cuts_the_speech_and_keeps_the_draft(audio):
    cli = _cli(speaking=True)
    audio.active = True
    stop = cli._voice_tts_stop

    async def script(send, app, buf, _esc):
        send("\x1b")
        await async_wait_until(stop.is_set, what="TTS cut")
        assert buf.text == "draft"
    _drive(cli, script)
    assert audio.stops == 1
    assert cli._voice_tts_done.is_set()


def test_escape_while_working_before_the_first_sentence_silences_the_turn(audio):
    """Nothing audible yet, pipeline armed: the stop event means nothing will be spoken."""
    cli = _cli(speaking=True)
    stop = cli._voice_tts_stop

    async def script(send, app, buf, _esc):
        send("\x1b")
        await async_wait_until(stop.is_set, what="pipeline stopped")
    _drive(cli, script)


def test_alt_enter_during_playback_cuts_then_inserts_a_newline(audio):
    cli = _cli(speaking=True)
    audio.active = True
    stop = cli._voice_tts_stop

    async def script(send, app, buf, _esc):
        send("\x1b\r")
        await async_wait_until(lambda: buf.text == "draft\n", what="newline inserted")
    _drive(cli, script)
    assert stop.is_set()
    assert audio.stops == 1  # cut once: the re-fed Escape did not re-enter the stop key
    assert cli.submitted == []


def test_two_fast_escapes_cut_and_do_not_clear_the_draft(audio):
    cli = _cli(speaking=True)
    audio.active = True
    stop = cli._voice_tts_stop

    async def script(send, app, buf, pressed):
        send("\x1b\x1b")
        await async_wait_until(lambda: stop.is_set() and len(pressed) >= 2 and _idle(app),
                               what="both escapes processed")
        assert buf.text == "draft"
    _drive(cli, script)


def test_escape_with_a_dialog_open_cuts_first_and_cancels_on_the_second_press(audio):
    cli = _cli(speaking=True)
    cli._slash_confirm_state = {"command": "/reset"}
    audio.active = True
    stop = cli._voice_tts_stop

    async def script(send, app, buf, _esc):
        send("\x1b")
        await async_wait_until(stop.is_set, what="TTS cut")
        assert cli.modal_cancels == []
        send("\x1b")
        await async_wait_until(lambda: cli.modal_cancels, what="dialog cancelled")
    _drive(cli, script)


def test_escape_without_speech_keeps_todays_double_escape(audio):
    cli = _cli(speaking=False)

    async def script(send, app, buf, _esc):
        send("\x1b\x1b")
        await async_wait_until(lambda: buf.text == "", what="draft discarded")
    _drive(cli, script)
    assert audio.stops == 0


@pytest.mark.parametrize("source", ["done_clear", "audible", "live_stop"])
def test_tts_cuttable_covers_its_three_sources(audio, source):
    cli = _cli(speaking=False)
    assert not cli._voice_tts_cuttable()
    if source == "done_clear":
        cli._voice_tts_done.clear()
    elif source == "audible":
        audio.active = True
    else:
        cli._voice_tts_stop = threading.Event()
    assert cli._voice_tts_cuttable()


def test_alt_enter_while_the_killed_player_still_reports_audio(audio, monkeypatch):
    """The audio-active flag drops only when the killed player's wait returns. The re-fed Escape
    must not re-enter the stop key meanwhile (that would re-feed forever)."""
    cli = _cli(speaking=True)
    audio.active = True
    monkeypatch.setattr("tools.voice_mode.stop_playback", lambda: setattr(audio, "stops", audio.stops + 1))

    async def script(send, app, buf, _esc):
        send("\x1b\r")
        await async_wait_until(lambda: buf.text == "draft\n", what="newline inserted")
    _drive(cli, script)
    assert audio.stops == 1
