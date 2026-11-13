"""Classic-CLI record key (Ctrl+Space for DG) and the full-duplex listener guards.

Plan-v2 W3 (DG local patch). ``_agent_running`` spans the whole streaming-TTS drain, so the old
handler's "agent running" branch disarmed continuous mode and returned before its TTS cut: the
key could not stop a reply and killed barge-in for the turn. Now (decision D2) a mid-turn press
cuts TTS, keeps continuous mode and records; the turn is interrupted only once that recording
yields a transcript. Ending your own take with the key keeps continuous mode (D1) where silence
auto-stop exists.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli import HermesCLI


@pytest.fixture
def audio(monkeypatch):
    state = SimpleNamespace(active=False, stops=0, marks=0, played=[])
    monkeypatch.setattr("tools.voice_mode.is_audio_output_active", lambda: state.active)

    def _stop():
        state.stops += 1
    monkeypatch.setattr("tools.voice_mode.stop_playback", _stop)

    def _mark():
        state.marks += 1
    monkeypatch.setattr("tools.tts_streaming.mark_speech_interrupted", _mark)
    monkeypatch.setattr("cli._cprint", lambda *_a, **_k: None)
    return state


class _Turn:
    """An agent thread that stays alive until finish()."""

    def __init__(self):
        self._done = threading.Event()
        self.thread = threading.Thread(target=self._done.wait, args=(30,), daemon=True)
        self.thread.start()

    def finish(self):
        self._done.set()
        self.thread.join(timeout=5)


@pytest.fixture
def turn():
    t = _Turn()
    yield t
    t.finish()


def _cli(**overrides):
    cli = HermesCLI.__new__(HermesCLI)
    cli._voice_lock = threading.Lock()
    cli._voice_mode = True
    cli._voice_tts = True
    cli._voice_recorder = MagicMock(supports_silence_autostop=True)
    cli._voice_recording = False
    cli._voice_processing = False
    cli._voice_continuous = True
    cli._voice_tts_done = threading.Event()
    cli._voice_tts_done.set()
    cli._voice_tts_stop = None
    cli._voice_barge_capture = threading.Event()
    cli._voice_last_tts_text = ""
    cli._voice_barge_phase = None
    cli._pending_input = queue.Queue()
    cli._interrupt_queue = queue.Queue()
    cli._attached_images = []
    cli._app = None
    cli._agent_running = False
    cli.agent = MagicMock()
    for name in ("_clarify_state", "_sudo_state", "_approval_state", "_slash_confirm_state", "_connection_state"):
        setattr(cli, name, None)
    cli.recording_started = threading.Event()
    cli._voice_start_recording = cli.recording_started.set
    for k, v in overrides.items():
        setattr(cli, k, v)
    return cli


EVENT = SimpleNamespace(app=SimpleNamespace(invalidate=lambda: None))


def _streaming_turn(cli, turn=None, *, generating):
    """A streaming-TTS turn: pipeline armed (done clear, live stop event), agent busy."""
    cli._agent_running = True
    cli._voice_tts_done.clear()
    cli._voice_tts_stop = threading.Event()
    if generating:
        cli._turn_agent_thread = turn.thread
    else:
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        cli._turn_agent_thread = dead
    return cli._voice_tts_stop


def _assert_cut_and_recording(cli, stop, audio):
    assert stop.is_set()
    assert audio.stops >= 1
    assert cli._voice_tts_done.is_set()
    assert cli._voice_continuous is True
    assert cli.recording_started.wait(timeout=5)


def test_key_during_playback_tail_cuts_and_records(audio):
    """S1 §f.1: the model finished, the reply is still playing ("Working…" window)."""
    cli = _cli()
    stop = _streaming_turn(cli, generating=False)
    audio.active = True
    cli._tui_handle_voice_record(EVENT)
    _assert_cut_and_recording(cli, stop, audio)
    cli.agent.interrupt.assert_not_called()


def test_key_while_generating_and_speaking_cuts_records_and_does_not_interrupt(audio, turn):
    """S1 §f.2 under D2: no interrupt on the press itself."""
    cli = _cli()
    stop = _streaming_turn(cli, turn, generating=True)
    audio.active = True
    cli._tui_handle_voice_record(EVENT)
    _assert_cut_and_recording(cli, stop, audio)
    cli.agent.interrupt.assert_not_called()


def test_key_in_a_gap_between_sentences_still_cuts(audio, turn):
    """Review §4.1: nothing audible this instant (between sentences), model still generating."""
    cli = _cli()
    stop = _streaming_turn(cli, turn, generating=True)
    audio.active = False
    cli._tui_handle_voice_record(EVENT)
    _assert_cut_and_recording(cli, stop, audio)


def test_key_during_silent_generation_cuts_the_pending_reply_and_records(audio, turn):
    """S1 §f.3 under D2 (no escape-hatch disarm)."""
    cli = _cli(_voice_continuous=False)
    stop = _streaming_turn(cli, turn, generating=True)
    cli._tui_handle_voice_record(EVENT)
    _assert_cut_and_recording(cli, stop, audio)
    cli.agent.interrupt.assert_not_called()


def test_key_while_transcribing_is_ignored(audio):
    """S1 §f.4: STT running — no disarm, no cut, no second recording."""
    cli = _cli(_voice_processing=True)
    stop = _streaming_turn(cli, generating=False)
    cli._tui_handle_voice_record(EVENT)
    assert cli._voice_continuous is True
    assert not stop.is_set()
    assert not cli.recording_started.is_set()


def test_key_while_listener_captures_cuts_without_a_second_recording(audio):
    """S1 §f.5."""
    cli = _cli()
    stop = _streaming_turn(cli, generating=False)
    cli._voice_barge_capture.set()
    cli._tui_handle_voice_record(EVENT)
    assert stop.is_set()
    assert not cli.recording_started.is_set()


def test_key_with_a_modal_prompt_open_only_cuts(audio, turn):
    """Review §4.2: approval prompt open while a sentence is spoken."""
    cli = _cli()
    stop = _streaming_turn(cli, turn, generating=True)
    cli._approval_state = {"command": "rm -rf build"}
    audio.active = True
    cli._tui_handle_voice_record(EVENT)
    assert stop.is_set()
    assert not cli.recording_started.is_set()
    assert cli._approval_state == {"command": "rm -rf build"}
    cli.agent.interrupt.assert_not_called()


def test_key_ending_your_own_take_keeps_continuous_mode(audio):
    """D1, where the recorder has silence auto-stop."""
    stopped = threading.Event()
    cli = _cli(_voice_recording=True)
    cli._voice_stop_and_transcribe = stopped.set
    cli._tui_handle_voice_record(EVENT)
    assert stopped.wait(timeout=5)
    assert cli._voice_continuous is True


def test_key_ending_a_take_without_silence_autostop_still_ends_continuous(audio):
    """D1 exception (Termux): the key is the only way to end a take there."""
    stopped = threading.Event()
    cli = _cli(_voice_recording=True)
    cli._voice_recorder.supports_silence_autostop = False
    cli._voice_stop_and_transcribe = stopped.set
    cli._tui_handle_voice_record(EVENT)
    assert stopped.wait(timeout=5)
    assert cli._voice_continuous is False


def test_key_when_idle_arms_continuous_and_records(audio):
    cli = _cli(_voice_continuous=False)
    cli._tui_handle_voice_record(EVENT)
    assert cli._voice_continuous is True
    assert cli.recording_started.wait(timeout=5)
    assert audio.stops == 0


# ── Deferred interrupt (D2) ─────────────────────────────────────────────────────────────────


def _transcribing_cli(monkeypatch, tmp_path, transcript="actually, wait"):
    wav = tmp_path / "take.wav"
    wav.write_bytes(b"RIFF")
    cli = _cli()
    cli._voice_recorder.stop.return_value = str(wav)
    cli._voice_beep = lambda **_k: None
    cli._voice_stt_model = lambda: None
    cli._voice_stt_provider = lambda: ""
    cli._voice_restart_recording_async = lambda: None
    monkeypatch.setattr("tools.voice_mode.transcribe_recording",
                        lambda path, model=None: {"success": True, "transcript": transcript})
    interrupted = threading.Event()
    cli.agent.interrupt.side_effect = lambda *a: interrupted.set()
    return cli, interrupted


def _press_mid_turn_then_finish_take(cli, turn):
    _streaming_turn(cli, turn, generating=True)
    cli._tui_handle_voice_record(EVENT)
    assert cli.recording_started.wait(timeout=5)
    cli._voice_recording = True  # the real start sets it; the stub only signals
    cli._voice_stop_and_transcribe()


def test_transcript_from_a_mid_turn_take_interrupts_the_live_turn_once(audio, monkeypatch, tmp_path, turn):
    cli, interrupted = _transcribing_cli(monkeypatch, tmp_path)
    _press_mid_turn_then_finish_take(cli, turn)
    assert interrupted.wait(timeout=5)
    assert cli.agent.interrupt.call_count == 1
    assert str(cli._pending_input.get_nowait()) == "actually, wait"  # never via _interrupt_queue
    assert cli._interrupt_queue.empty()


def test_no_interrupt_when_the_turn_already_ended(audio, monkeypatch, tmp_path, turn):
    cli, _interrupted = _transcribing_cli(monkeypatch, tmp_path)
    _streaming_turn(cli, turn, generating=True)
    cli._tui_handle_voice_record(EVENT)
    assert cli.recording_started.wait(timeout=5)
    cli._voice_recording = True
    turn.finish()
    cli._voice_stop_and_transcribe()
    cli.agent.interrupt.assert_not_called()  # decided synchronously: no thread was started
    assert str(cli._pending_input.get_nowait()) == "actually, wait"


def test_no_interrupt_for_an_empty_take(audio, monkeypatch, tmp_path, turn):
    cli, _interrupted = _transcribing_cli(monkeypatch, tmp_path, transcript="   ")
    _press_mid_turn_then_finish_take(cli, turn)
    cli.agent.interrupt.assert_not_called()  # decided synchronously: no thread was started
    assert cli._pending_input.empty()


def test_no_interrupt_while_a_modal_prompt_is_open(audio, monkeypatch, tmp_path, turn):
    cli, _interrupted = _transcribing_cli(monkeypatch, tmp_path)
    _streaming_turn(cli, turn, generating=True)
    cli._tui_handle_voice_record(EVENT)
    assert cli.recording_started.wait(timeout=5)
    cli._voice_recording = True
    cli._clarify_state = {"question": "which one?"}
    cli._voice_stop_and_transcribe()
    cli.agent.interrupt.assert_not_called()  # decided synchronously: no thread was started


# ── Full-duplex listener guards ─────────────────────────────────────────────────────────────


def _listener_cli(monkeypatch, listen):
    cli = _cli()
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"voice": {"barge_in": True}})
    monkeypatch.setattr("tools.voice_mode.full_duplex_listen", listen)
    return cli


def test_listener_stops_once_the_record_key_is_recording(audio, monkeypatch):
    """S1 §f.7: no double capture of one utterance."""
    seen = []

    def listen(should_stop, **_kw):
        seen.append(should_stop())
        cli._voice_recording = True
        seen.append(should_stop())
    cli = _listener_cli(monkeypatch, listen)
    cli._agent_running = True
    cli._voice_full_duplex_listener()
    assert seen == [False, True]


def test_listener_trip_is_discarded_while_the_record_key_records(audio, monkeypatch, tmp_path):
    wav = tmp_path / "trip.wav"
    wav.write_bytes(b"RIFF")
    cli = None

    def listen(should_stop, on_trigger=None, **_kw):
        cli._voice_recording = True
        on_trigger("playback")
        return str(wav)
    cli = _listener_cli(monkeypatch, listen)
    cli._agent_running = True
    stop = cli._voice_tts_stop = threading.Event()
    cli._voice_full_duplex_listener()
    assert not stop.is_set()
    assert not cli._voice_barge_capture.is_set()
    assert cli._pending_input.empty()
    assert not wav.exists()


# ── Batch (non-streaming) TTS ───────────────────────────────────────────────────────────────


def _batch_cli(monkeypatch, audio, *, on_synthesis=None):
    import json
    cli = _cli()
    played = []

    def fake_tts(text, output_path):
        if on_synthesis:
            on_synthesis(cli)
        return json.dumps({"success": True, "file_paths": [output_path + ".1.mp3", output_path + ".2.mp3"]})
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    monkeypatch.setattr("tools.voice_mode.play_audio_file", played.append)
    monkeypatch.setattr("os.path.isfile", lambda _p: True)
    monkeypatch.setattr("os.path.getsize", lambda _p: 10)
    return cli, played


def test_batch_cut_during_synthesis_plays_nothing(audio, monkeypatch):
    """Review §4.12: the record key cuts while the batch reply is still being synthesised."""
    cli, played = _batch_cli(monkeypatch, audio, on_synthesis=lambda c: c._tui_handle_voice_record(EVENT))
    cli._voice_speak_response("A reply long enough to be chunked.")
    assert played == []
    assert cli._voice_tts_stop is None  # the batch slot is released when the speaker ends


def test_batch_reply_is_not_spoken_over_a_recording(audio, monkeypatch):
    """Review MINOR 4: a key press between the agent's end and the batch speak call."""
    cli, played = _batch_cli(monkeypatch, audio)
    cli._voice_recording = True
    cli._voice_speak_response_async("A reply.")
    assert cli._voice_tts_done.is_set()
    assert played == []


# ── No non-user turn under a voice take (W3.7) ─────────────────────────────────────────────


def test_idle_tick_starts_no_notification_loop_or_goal_turn_while_recording():
    cli = _cli(_voice_recording=True)
    calls = []
    cli._check_config_mcp_changes = lambda: None
    cli._check_termios_drift = lambda: calls.append("termios")
    cli._drain_process_notifications = lambda consumer: calls.append("notifications")
    cli._maybe_fire_loop_tick = lambda: calls.append("loop")
    cli._maybe_resume_parked_goal = lambda: calls.append("goal")
    cli._tui_idle_tick()
    assert calls == ["termios"]
    cli._voice_recording = False
    cli._tui_idle_tick()
    assert calls[1:] == ["termios", "notifications", "loop", "goal"]


def test_after_turn_skips_goal_continuation_and_notifications_while_recording():
    cli = _cli(_voice_recording=True)
    calls = []
    cli._app = SimpleNamespace(invalidate=lambda: None)
    cli._pending_tool_info = {}
    cli._pet_react_turn_end = cli._turn_summary_emit = lambda: None
    cli._last_turn_interrupted = False
    cli._drain_interrupt_queue_to_pending_input = lambda: None
    cli._maybe_continue_goal_after_turn = lambda: calls.append("goal")
    cli._maybe_complete_loop_tick_after_turn = lambda: calls.append("loop")
    cli._drain_process_notifications = lambda consumer: calls.append("notifications")
    cli._tui_after_turn()
    assert calls == ["loop"]
