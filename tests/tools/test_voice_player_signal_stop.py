"""A player killed by a signal is a stop, not a failed player.

Plan-v2 W1b (DG local patch, from sweep-4). ``_run_system_player`` treated afplay's SIGTERM exit
(rc -15, from ``stop_playback()`` or an external kill) as "player failed" and fell through to
ffplay, which replayed the same sentence from the start. Every cutter (record key, Ctrl+C,
barge-in) cut and then replayed.
"""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from tests.waiting import wait_until
from tools import voice_mode as vm


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "sentence.mp3"
    path.write_bytes(b"\x00" * 64)
    return str(path)


def _two_players(audio_file, first):
    """Route playback through two system players: *first*, then a stand-in for ffplay."""
    return patch.object(vm, "_system_player_candidates",
                        return_value=[first, ["fallback-player", audio_file]])


def _fake_popen(returncode, launched):
    def popen(cmd, **_kw):
        launched.append(cmd[0])
        proc = MagicMock()
        proc.returncode = returncode
        proc.wait.return_value = returncode
        return proc
    return popen


@pytest.mark.parametrize("signal_rc", [-15, -9])
def test_player_killed_by_a_signal_launches_no_other_player(audio_file, signal_rc):
    launched: list[str] = []
    with _two_players(audio_file, ["afplay", audio_file]), \
            patch.object(vm.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
            patch.object(vm.subprocess, "Popen", side_effect=_fake_popen(signal_rc, launched)):
        assert vm._play_audio_file_impl(audio_file) is True
    assert launched == ["afplay"]


def test_player_that_genuinely_fails_still_falls_through(audio_file):
    launched: list[str] = []
    with _two_players(audio_file, ["afplay", audio_file]), \
            patch.object(vm.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
            patch.object(vm.subprocess, "Popen", side_effect=_fake_popen(1, launched)):
        vm._play_audio_file_impl(audio_file)
    assert launched == ["afplay", "fallback-player"]


@pytest.mark.platforms("posix")
def test_stop_playback_mid_sentence_plays_nothing_else(audio_file):
    """A real child process stands in for afplay; stop_playback() terminates it."""
    launched: list[str] = []
    real_popen = vm.subprocess.Popen

    def spy(cmd, **kw):
        launched.append(cmd[0])
        return real_popen(cmd, **kw)

    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    result: list[bool] = []
    with _two_players(audio_file, sleeper), \
            patch.object(vm.shutil, "which", side_effect=lambda name: name), \
            patch.object(vm.subprocess, "Popen", side_effect=spy):
        player = threading.Thread(target=lambda: result.append(vm._play_audio_file_impl(audio_file)), daemon=True)
        player.start()
        wait_until(lambda: vm._active_playback is not None, what="player registered")
        vm.stop_playback()
        player.join(timeout=10)
    assert not player.is_alive()
    assert launched == [sys.executable]
    assert result == [True]
