"""Streaming TTS speaker pipeline: backpressure, stop and logging contracts.

Plan-v2 W1 (DG local patch). On the temp-file path (macOS, or no sounddevice) the playback
worker drains a sentence only after the previous one finished playing, so a bounded per-sentence
queue plus ``put(timeout=30)`` truncated long sentences queued behind long playback ("TTS CUT …
prefetch failed"). A stop must never strand the text pump or the prefetch threads.
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from unittest.mock import MagicMock, patch

import pytest

from tools import tts_tool
from tools.tts_tool_speaker import _play_via_tempfile, _StreamerPlayback

pytest.importorskip("numpy")

CHUNK = b"\x01\x00" * 4096  # 8192 bytes, Kokoro's chunk size


class _Streamer:
    sample_rate = 24000
    channels = 1

    def __init__(self, per_text):
        self._per_text = per_text

    def stream(self, text):
        return self._per_text(text)


def _no_sounddevice():
    """Route playback through temp WAV files: the path taken without a usable sounddevice."""
    return patch.object(tts_tool, "_import_sounddevice", side_effect=ImportError("no sounddevice"))


def _frames(path: str) -> int:
    with open(path, "rb") as fh, wave.open(io.BytesIO(fh.read())) as wf:
        return wf.getnframes()


def _finish_in_background(pb: _StreamerPlayback) -> threading.Thread:
    t = threading.Thread(target=pb.finish, daemon=True)
    t.start()
    return t


def test_long_sentence_queued_behind_slow_playback_is_not_truncated():
    """A sentence of more than 64 chunks waiting behind a long first sentence plays in full."""
    first_playing, release_first = threading.Event(), threading.Event()
    second_fully_prefetched = threading.Event()
    played: list[int] = []

    def per_text(text):
        if text == "one":
            yield CHUNK
            return
        for _ in range(100):
            yield CHUNK
        second_fully_prefetched.set()

    def fake_play(path):
        played.append(_frames(path))
        if len(played) == 1:
            first_playing.set()
            release_first.wait(timeout=10)

    stop = threading.Event()
    with _no_sounddevice(), patch("tools.voice_mode.play_audio_file", side_effect=fake_play):
        pb = _StreamerPlayback(_Streamer(per_text), stop)
        try:
            pb.speak("one")
            pb.speak("two")
            assert first_playing.wait(timeout=5)
            assert second_fully_prefetched.wait(timeout=5), (
                "prefetch of the queued sentence stalled behind playback (bounded chunk queue)")
        finally:
            release_first.set()
        finisher = _finish_in_background(pb)
        finisher.join(timeout=10)
        assert not finisher.is_alive()
    assert played == [len(CHUNK) // 2, 100 * len(CHUNK) // 2]


class _CountingSemaphore(threading.Semaphore):
    def __init__(self, value, *, on_nth, nth):
        super().__init__(value)
        self._calls, self._on_nth, self._nth = 0, on_nth, nth

    def acquire(self, blocking=True, timeout=None):
        self._calls += 1
        if self._calls == self._nth:
            self._on_nth.set()
        return super().acquire(blocking, timeout)


def test_stop_releases_a_speak_blocked_on_prefetch_capacity():
    """All three prefetch slots held by stalled requests: a stop must free the fourth speak()."""
    release_streams = threading.Event()
    started = threading.Semaphore(0)

    def per_text(_text):
        started.release()
        release_streams.wait(timeout=10)
        yield CHUNK

    stop = threading.Event()
    fourth_acquire = threading.Event()
    with _no_sounddevice(), patch("tools.voice_mode.play_audio_file"):
        pb = _StreamerPlayback(_Streamer(per_text), stop)
        pb._prefetch_sem = _CountingSemaphore(3, on_nth=fourth_acquire, nth=4)
        try:
            for text in ("a", "b", "c"):
                pb.speak(text)
            for _ in range(3):
                assert started.acquire(timeout=5)
            fourth = threading.Thread(target=pb.speak, args=("d",), daemon=True)
            fourth.start()
            assert fourth_acquire.wait(timeout=5), "fourth speak() never reached the semaphore"
            stop.set()
            fourth.join(timeout=3)
            assert not fourth.is_alive(), "speak() stayed blocked on prefetch capacity after stop"
        finally:
            stop.set()
            release_streams.set()
        finisher = _finish_in_background(pb)
        finisher.join(timeout=10)
        assert not finisher.is_alive()


def test_stop_between_wav_write_and_play_never_starts_the_player():
    stop = threading.Event()

    def chunks():
        yield CHUNK
        stop.set()  # the barge-in lands while the WAV is being finalised

    with patch("tools.voice_mode.play_audio_file") as play:
        _play_via_tempfile(chunks(), stop)
    play.assert_not_called()


def test_prefetch_failure_log_names_the_exception_and_sentence(caplog):
    """An exception with an empty message (queue.Full, TimeoutError) must still be diagnosable."""

    def per_text(_text):
        yield CHUNK
        raise TimeoutError()

    stop = threading.Event()
    with _no_sounddevice(), patch("tools.voice_mode.play_audio_file"), \
            caplog.at_level(logging.WARNING, logger="tools.tts_tool"):
        pb = _StreamerPlayback(_Streamer(per_text), stop)
        pb.speak("Hello there, a sentence.")
        finisher = _finish_in_background(pb)
        finisher.join(timeout=10)
    lines = [r.getMessage() for r in caplog.records if "prefetch failed" in r.getMessage()]
    assert lines, caplog.text
    assert "TimeoutError" in lines[0]
    assert "sentence 1" in lines[0] and "24 chars" in lines[0] and f"{len(CHUNK)} bytes queued" in lines[0]


def test_stop_during_a_portaudio_sentence_lets_finish_return():
    """PortAudio route (Linux/Windows): a producer blocked on the bounded queue must not hold
    finish() for its 10 s join after a stop. Mocked sounddevice; the route is chosen through
    ``_device_usable``, which stands for "sounddevice output is usable here"."""
    write_entered, release_write = threading.Event(), threading.Event()
    produced = [0]

    def blocking_write(_frames):
        write_entered.set()
        release_write.wait(timeout=10)

    sd = MagicMock()
    sd.OutputStream.return_value.write.side_effect = blocking_write

    def per_text(_text):
        for _ in range(200):
            produced[0] += 1
            yield CHUNK

    stop = threading.Event()
    with patch.object(_StreamerPlayback, "_device_usable", return_value=True), \
            patch.object(tts_tool, "_import_sounddevice", return_value=sd):
        pb = _StreamerPlayback(_Streamer(per_text), stop)
        try:
            pb.speak("A long sentence that fills the bounded queue.")
            assert write_entered.wait(timeout=5)
            from tests.waiting import wait_until
            wait_until(lambda: produced[0] > _StreamerPlayback._CHUNK_QUEUE_MAX + 1, timeout=5,
                       what="producer blocked on a full chunk queue")
            stop.set()
        finally:
            release_write.set()
        finisher = _finish_in_background(pb)
        finisher.join(timeout=5)
        assert not finisher.is_alive(), "finish() blocked on a stranded prefetch thread after stop"
