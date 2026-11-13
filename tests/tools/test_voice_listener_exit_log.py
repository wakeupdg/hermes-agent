"""The full-duplex listener logs one info line when it exits (plan-v2 D3).

The playback trigger (``PLAYBACK_MIN_TRIGGER``) is not tuned yet; the exit line records the
quiet floor, the effective playback trigger and the loudest block heard during playback, so the
decision can be made from ``agent.log`` instead of a ``HERMES_VOICE_DEBUG`` stderr session.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

np = pytest.importorskip("numpy")

from tools import voice_mode as vm


def test_listener_exit_logs_floor_trigger_and_peak_playback_rms(caplog):
    blocks = iter([200] * 20 + [900] * 10)  # quiet room, then playback bleed at ~900 RMS
    stream = MagicMock()
    stream.read.side_effect = lambda n: (np.full((n, 1), next(blocks), dtype="int16"), False)
    stream.__enter__.return_value = stream
    sd = MagicMock()
    sd.InputStream.return_value = stream
    reads = []

    def should_stop():
        reads.append(1)
        return len(reads) > 30

    with patch.object(vm, "_import_audio", return_value=(sd, np)), \
            caplog.at_level(logging.INFO, logger=vm.logger.name):
        assert vm.full_duplex_listen(should_stop, is_playing=lambda: len(reads) > 20) is None
    lines = [r.getMessage() for r in caplog.records if "Full-duplex listener exit" in r.getMessage()]
    assert len(lines) == 1
    assert "tripped=None" in lines[0]
    assert "peak_playback_rms=900" in lines[0]
    assert f"playback_trigger={vm.PLAYBACK_MIN_TRIGGER:.0f}" in lines[0]
