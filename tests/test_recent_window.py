"""Tests for `recent`: tokens inside a declared trailing window.

Every other command measures the whole store (`nested`), the whole receipt
(`scan`) or a window *pair* (`compare`). The question a user asks about a quota
is narrower: "the last N days". That question has its own three traps, and this
command exists to answer it without falling into any of them:

* the window must be placed by a recorded day, not by the clock -- a store whose
  newest record is three weeks old must not report an empty last week;
* days inside the window that carry no record make the total a floor, and the
  floor must be printed rather than smoothed away;
* records with no timestamp are outside every window, so they are named.

All fixtures are synthetic.
"""

import json

import quotamirror


def usage_line(message_id, ts, input_tokens=10, output_tokens=0, cache_read=0, sidechain=False, model="opus"):
    record = {
        "type": "assistant",
        "uuid": f"u-{message_id}-{ts}",
        "isSidechain": sidechain,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if ts:
        record["timestamp"] = ts
    return record


def write_transcripts(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, lines in files.items():
        (root / name).write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return root


def test_the_window_ends_on_the_newest_recorded_day_and_names_its_basis(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z"),
                usage_line("m2", "2026-05-05T10:00:00Z"),
                usage_line("m3", "2026-05-09T10:00:00Z"),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=3)

    # The clock is 2026-05-09's record, not "today": a store whose newest record
    # is old must not be read as an empty last week.
    assert payload["window"] == {
        "first": "2026-05-07",
        "last": "2026-05-09",
        "days": 3,
        "basis": "record_timestamp",
    }
    assert payload["anchor"] == {"day": "2026-05-09", "source": "record"}
    assert payload["total_tokens"] == 10
    assert payload["verdict"] == "window_measured"


def test_days_inside_the_window_with_no_record_make_the_total_a_floor(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {"a.jsonl": [usage_line("m1", "2026-05-01T10:00:00Z", 10), usage_line("m2", "2026-05-10T10:00:00Z", 5)]},
    )
    payload = quotamirror.recent_window(root, days=10)

    assert payload["days_with_records"] == 2
    assert payload["missing_count"] == 8
    assert payload["missing_days"][0] == "2026-05-02"
    assert payload["floor"] is True
    text = quotamirror.render_recent_window(payload)
    assert "2 of 10 day(s)" in text
    assert "floor" in text


def test_a_repeated_message_id_is_merged_so_the_window_cannot_exceed_nested(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m1", "2026-05-01T10:00:01Z", 10),
                usage_line("m2", "2026-05-02T10:00:00Z", 7),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=7)
    nested = quotamirror.nested_audit(root)

    assert payload["records"] == 3
    assert payload["messages"] == 2
    assert payload["total_tokens"] == 17
    # One merge rule for one corpus: the window cannot be a second, larger bill.
    assert payload["total_tokens"] == nested["totals"]["all"]["total_tokens"]


def test_until_moves_the_anchor_and_says_the_anchor_was_given_not_measured(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-02T10:00:00Z", 4),
                usage_line("m2", "2026-05-04T10:00:00Z", 6),
                usage_line("m3", "2026-05-09T10:00:00Z", 100),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=3, until="2026-05-05")

    assert payload["window"]["last"] == "2026-05-05"
    assert payload["anchor"] == {"day": "2026-05-05", "source": "given"}
    # Only the day inside 05-03..05-05 is counted; `--until` puts real days on
    # both sides of the window and each side is named rather than dropped.
    assert payload["total_tokens"] == 6
    assert payload["outside_window"]["tokens_before_window"] == 4
    assert payload["outside_window"]["tokens_after_window"] == 100
    assert payload["outside_window"]["tokens"] == 104
    text = quotamirror.render_recent_window(payload)
    assert "given, not measured" in text


def test_an_empty_store_is_unobserved_and_prints_no_window(tmp_path):
    payload = quotamirror.recent_window(tmp_path, days=7)

    assert payload["observation"]["verdict"] == "store_empty"
    assert payload["window"] is None
    assert payload["total_tokens"] is None
    text = quotamirror.render_recent_window(payload)
    assert "unobserved" in text
    assert "not measured" in text


def test_a_record_without_a_timestamp_is_named_as_outside_the_window(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {"a.jsonl": [usage_line("m1", "2026-05-02T10:00:00Z", 10), usage_line("m2", None, 500)]},
    )
    payload = quotamirror.recent_window(root, days=7)

    assert payload["outside_window"]["records_without_timestamp"] == 1
    assert payload["outside_window"]["tokens"] == 500
    text = quotamirror.render_recent_window(payload)
    assert "outside the window" in text


def test_the_share_names_its_denominator_and_excludes_cache_read(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-02T10:00:00Z", 30, model="opus"),
                usage_line("m2", "2026-05-02T11:00:00Z", 10, cache_read=1000, model="sonnet"),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=7)

    assert payload["share_basis"] == {"name": "non_cache_read_tokens", "tokens": 40}
    rows = {row["model"]: row for row in payload["rows"]}
    assert rows["opus"]["share_pct"] == 75.0
    assert rows["sonnet"]["share_pct"] == 25.0
    text = quotamirror.render_recent_window(payload)
    assert "of 40 non_cache_read_tokens" in text


def test_cli_recent_prints_json_that_carries_the_verdict(tmp_path):
    import subprocess
    import sys

    root = write_transcripts(
        tmp_path / "projects", {"a.jsonl": [usage_line("m1", "2026-05-02T10:00:00Z", 10)]}
    )
    script = str(__import__("pathlib").Path(quotamirror.__file__).resolve())
    proc = subprocess.run(
        [sys.executable, script, "recent", "--path", str(root), "--days", "7", "--json"],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["verdict"] == "window_measured"
    assert payload["window"]["days"] == 7


def test_cli_recent_refuses_a_bad_day_count_or_an_unparseable_date(tmp_path):
    import subprocess
    import sys

    root = write_transcripts(
        tmp_path / "projects", {"a.jsonl": [usage_line("m1", "2026-05-02T10:00:00Z", 10)]}
    )
    script = str(__import__("pathlib").Path(quotamirror.__file__).resolve())

    zero = subprocess.run(
        [sys.executable, script, "recent", "--path", str(root), "--days", "0"],
        capture_output=True,
        text=True,
    )
    bad_date = subprocess.run(
        [sys.executable, script, "recent", "--path", str(root), "--until", "2026-13-01"],
        capture_output=True,
        text=True,
    )

    assert zero.returncode == 2
    assert "at least 1" in zero.stderr
    assert bad_date.returncode == 2
    assert "YYYY-MM-DD" in bad_date.stderr
