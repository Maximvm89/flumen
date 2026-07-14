"""Publish progress protocol: percent, ETA, line format/parse round-trip, and
publish_task reporting cumulative byte progress across files."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import progress as P
from flumen import tasks
from test_tasks import FakeSrv


def test_percent_clamped_and_zero_total():
    assert P.percent(0, 100) == 0
    assert P.percent(50, 100) == 50
    assert P.percent(100, 100) == 100
    assert P.percent(200, 100) == 100      # clamp
    assert P.percent(5, 0) == 100          # empty total -> complete


def test_eta_seconds_needs_signal_then_estimates():
    assert P.eta_seconds(0, 100, 0) is None     # nothing yet
    assert P.eta_seconds(0, 100, 5) is None      # no bytes yet
    # 50 of 100 bytes in 5s -> 10 B/s -> 50 left -> ~5s
    assert P.eta_seconds(50, 100, 5) == 5.0


def test_format_and_parse_round_trip():
    line = P.format_line(50, 100, 5, "uploading panda_model_v001.blend")
    assert line.startswith(P.PREFIX)
    pct, eta, msg = P.parse_line(line)
    assert pct == 50 and eta == 5.0 and msg == "uploading panda_model_v001.blend"


def test_parse_ignores_non_progress_and_blank_eta():
    assert P.parse_line("just some log line") is None
    assert P.parse_line("") is None
    pct, eta, msg = P.parse_line(P.format_line(0, 100, 0, "starting"))
    assert pct == 0 and eta is None and msg == "starting"   # eta blank early


def test_human_eta_units():
    assert P.human_eta(None) == ""
    assert P.human_eta(8) == "~8s left"
    assert P.human_eta(125) == "~2m left"


def test_publish_task_reports_cumulative_progress(tmp_path):
    # Two real files of known sizes so getsize works and progress is byte-weighted.
    a = tmp_path / "panda_model_v001.blend"
    a.write_bytes(b"BLENDER" + b"x" * 293)    # valid header, still 300 bytes
    b = tmp_path / "panda_model_v001.fbx"
    b.write_bytes(b"y" * 100)
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))

    seen = []
    tasks.publish_task(s, "/r", "marco", [str(a), str(b)], t["id"],
                       progress=lambda done, total, name: seen.append((done, total, name)))
    assert seen, "progress should have been reported"
    total = seen[-1][1]
    assert total == 400                       # 300 + 100 bytes
    assert seen[-1][0] == 400                 # ends fully done
    assert all(d <= total for d, _, _ in seen)
    # monotonic non-decreasing cumulative bytes
    assert [d for d, _, _ in seen] == sorted(d for d, _, _ in seen)
