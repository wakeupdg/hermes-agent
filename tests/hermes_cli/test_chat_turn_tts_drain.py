"""Streaming-TTS drain at the end of a CLI turn (``_chat_settle_turn``).

Plan-v2 W2 (DG local patch). The settle step used to ``join(timeout=120)`` the speaker thread:
replies with more than 120 s of audio left were cut, a message typed while the reply was still
playing did not stop it, and a stopped or stalled pipeline held the turn for the full 120 s.
The drain now polls the speaker thread with explicit exits.

The speaker thread is a fake driven by a fake clock, so minutes of "audio" run instantly.
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

import hermes_cli.cli_chat_turn_mixin as turn_mod
from cli import HermesCLI, _ChatTurn


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _FakeSpeaker:
    """Stands in for the stream_tts_to_speaker thread; ``join`` advances the fake clock."""

    def __init__(self, clock, stop, *, plays_for=None, honours_stop=True, on_tick=None):
        self.clock, self.stop, self.start = clock, stop, clock.now
        self.plays_for, self.honours_stop, self.on_tick = plays_for, honours_stop, on_tick

    def is_alive(self):
        if self.honours_stop and self.stop.is_set():
            return False
        return self.plays_for is None or self.clock.now - self.start < self.plays_for

    def join(self, timeout=None):
        step = 10_000.0 if timeout is None else timeout
        if self.plays_for is not None:
            step = min(step, max(0.0, self.start + self.plays_for - self.clock.now))
        self.clock.now += step
        if self.on_tick:
            self.on_tick(self.clock.now - self.start)

    @property
    def elapsed(self):
        return self.clock.now - self.start


@pytest.fixture
def env(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(turn_mod, "time", SimpleNamespace(
        monotonic=clock.monotonic, time=time.time, sleep=lambda _s: None))
    audio = SimpleNamespace(active=True, stops=0)
    monkeypatch.setattr("tools.voice_mode.is_audio_output_active", lambda: audio.active)

    def _stop_playback():
        audio.stops += 1
    monkeypatch.setattr("tools.voice_mode.stop_playback", _stop_playback)
    return clock, audio


def _cli():
    cli = HermesCLI.__new__(HermesCLI)
    cli._prompt_start_time = None
    cli._flush_stream = lambda: None
    cli.agent = None
    cli._should_exit = False
    cli._interrupt_queue = queue.Queue()
    cli._voice_tts_done = threading.Event()
    return cli


def _turn(speaker_factory):
    turn = _ChatTurn()
    turn.use_streaming_tts = True
    turn.text_queue = queue.Queue()
    turn.stop_event = threading.Event()
    turn.tts_thread = speaker_factory(turn.stop_event)
    return turn


def test_reply_longer_than_two_minutes_is_not_cut(env):
    clock, _audio = env
    cli = _cli()
    turn = _turn(lambda stop: _FakeSpeaker(clock, stop, plays_for=300.0))
    cli._chat_settle_turn(turn)
    assert turn.tts_normal_exit
    assert not turn.stop_event.is_set()
    assert turn.tts_thread.elapsed == pytest.approx(300.0)


def test_message_typed_during_playback_cuts_the_audio(env):
    clock, audio = env
    cli = _cli()

    def typed_at_10s(elapsed):
        if elapsed >= 10 and cli._interrupt_queue.empty():
            cli._interrupt_queue.put("actually, stop")
    turn = _turn(lambda stop: _FakeSpeaker(clock, stop, plays_for=200.0, on_tick=typed_at_10s))
    cli._chat_settle_turn(turn)
    assert turn.stop_event.is_set()
    assert audio.stops >= 1
    assert turn.tts_thread.elapsed < 12
    assert cli._interrupt_queue.get_nowait() == "actually, stop"  # still runs as the next turn


def test_stopped_pipeline_that_does_not_wind_down_is_left_after_the_grace(env):
    clock, _audio = env
    cli = _cli()
    turn = _turn(lambda stop: _FakeSpeaker(clock, stop, honours_stop=False))
    turn.stop_event.set()  # e.g. the record key cut it; the thread is stuck in a network read
    cli._chat_settle_turn(turn)
    assert not turn.tts_normal_exit
    assert 5.0 <= turn.tts_thread.elapsed < 7.0


def test_stall_watchdog_stops_a_silent_pipeline(env, caplog):
    clock, audio = env
    audio.active = False
    cli = _cli()
    turn = _turn(lambda stop: _FakeSpeaker(clock, stop, honours_stop=False))
    cli._chat_settle_turn(turn)
    assert turn.stop_event.is_set()
    assert not turn.tts_normal_exit
    assert 45.0 <= turn.tts_thread.elapsed < 47.0
    assert "no audio" in caplog.text


def test_app_exit_leaves_the_drain(env):
    clock, _audio = env
    cli = _cli()

    def exit_at_3s(elapsed):
        if elapsed >= 3:
            cli._should_exit = True
    turn = _turn(lambda stop: _FakeSpeaker(clock, stop, on_tick=exit_at_3s))
    cli._chat_settle_turn(turn)
    assert turn.tts_thread.elapsed < 4
    assert not turn.tts_normal_exit


def test_tts_done_set_by_another_writer_does_not_cut_the_reply(env):
    clock, _audio = env
    cli = _cli()

    def other_writer(elapsed):
        if elapsed >= 5:
            cli._voice_tts_done.set()
    turn = _turn(lambda stop: _FakeSpeaker(clock, stop, plays_for=30.0, on_tick=other_writer))
    cli._chat_settle_turn(turn)
    assert turn.tts_normal_exit
    assert not turn.stop_event.is_set()
    assert turn.tts_thread.elapsed == pytest.approx(30.0)
