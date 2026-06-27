"""Tests for animpipe.bugreport (pure) and workspace_app.applog helpers."""

import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import bugreport as B


def test_environment_text_includes_fields():
    txt = B.environment_text(version="0.2.0", platform_str="Darwin 25.5",
                             project="Legami [LEGAMI]", user="marco",
                             remote_root="/shared/Legami", local_root="/x")
    assert "0.2.0" in txt and "marco" in txt and "Darwin 25.5" in txt
    assert "**App version:**" in txt


def test_environment_text_blanks_become_dash():
    assert "—" in B.environment_text(version="0.1.0")


def test_issue_body_sections():
    body = B.issue_body("It broke", "- **User:** marco", "line1\nline2",
                        ["log.txt", "screenshot.png"])
    assert "It broke" in body
    assert "## Environment" in body and "marco" in body
    assert "## Log (tail)" in body and "```" in body and "line2" in body
    assert "## Attachments" in body
    assert "`log.txt`" in body and "`screenshot.png`" in body


def test_issue_body_empty_description_placeholder():
    body = B.issue_body("", "env", "", None)
    assert "_(no description provided)_" in body
    assert "## Attachments" not in body  # nothing attached
    assert "## Log (tail)" not in body   # no tail


def test_new_issue_url_encoded():
    url = B.new_issue_url("Crash & burn", "body <here>", repo="o/r", labels="bug")
    assert url.startswith("https://github.com/o/r/issues/new?")
    q = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert q["title"] == ["Crash & burn"]
    assert q["body"] == ["body <here>"]
    assert q["labels"] == ["bug"]


def test_build_issue_caps_url_and_keeps_recent_lines():
    env = "- **User:** marco"
    big_tail = "\n".join(f"log line {i}" for i in range(5000))  # huge
    url, body = B.build_issue("T", "desc", env, big_tail,
                              ["log.txt"], max_url=4000)
    assert len(url) <= 4000
    # If any tail survived, it must be the most-recent lines (high indices), not old.
    if "log line" in body:
        assert "log line 4999" in body
        assert "log line 0\n" not in body


def test_build_issue_small_tail_untrimmed():
    url, body = B.build_issue("T", "d", "env", "only one line", ["log.txt"])
    assert "only one line" in body
    assert len(url) < B.DEFAULT_MAX_URL


def test_applog_tee_and_read_tail(tmp_path):
    import io
    from workspace_app import applog
    a, b = io.StringIO(), io.StringIO()
    tee = applog._Tee(a, b, None)  # None stream tolerated
    tee.write("hello")
    tee.flush()
    assert a.getvalue() == "hello" and b.getvalue() == "hello"

    log = tmp_path / "w.log"
    log.write_text("".join(f"line{i}\n" for i in range(50)))
    tail = applog.read_tail(str(log), n_lines=5)
    assert tail.splitlines() == ["line45", "line46", "line47", "line48", "line49"]
    assert applog.read_tail(str(tmp_path / "missing.log")) == ""
