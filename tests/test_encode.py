"""Tests for encode.py — frame-sequence detection for the BRQ-output encoder."""

import os

from flumen import encode


def _touch(d, *names):
    for n in names:
        open(os.path.join(d, n), "wb").close()


def test_find_sequence_detects_pattern_start_and_count(tmp_path):
    d = str(tmp_path)
    _touch(d, "frame_1001.png", "frame_1002.png", "frame_1003.png")
    seq = encode.find_sequence(d)
    assert seq == {"pattern": "frame_%04d.png", "start": 1001, "end": 1003,
                   "count": 3, "gaps": 0}


def test_find_sequence_longest_group_wins_and_gaps_counted(tmp_path):
    d = str(tmp_path)
    # a stray other render + a hole in the main sequence
    _touch(d, "old_0001.png", "old_0002.png",
           "shot_101.jpg", "shot_102.jpg", "shot_104.jpg")
    seq = encode.find_sequence(d)
    assert seq["pattern"] == "shot_%03d.jpg"
    assert (seq["start"], seq["end"], seq["count"], seq["gaps"]) == (101, 104,
                                                                    3, 1)


def test_find_sequence_ignores_non_frames_and_exr(tmp_path):
    d = str(tmp_path)
    _touch(d, "notes.txt", "shot.mp4", "linear_1001.exr", "linear_1002.exr")
    assert encode.find_sequence(d) is None
    assert encode.find_sequence(str(tmp_path / "missing")) is None


def test_ffmpeg_cmd_matches_dailies_recipe():
    cmd = encode._ffmpeg_cmd("ffmpeg", "/r/frame_%04d.png", 1001, 24,
                             "/r/out.mp4")
    assert cmd[0] == "ffmpeg"
    assert "-start_number" in cmd and cmd[cmd.index("-start_number") + 1] == "1001"
    assert "libx264" in cmd and "yuv420p" in cmd
    assert cmd[cmd.index("-crf") + 1] == "18"      # same look as dailies
