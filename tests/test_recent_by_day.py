"""Tests for `recent --by-day`: the window's days, one row each.

`recent` prints a total and, when days inside the window carry no record, a
sentence that the total is a floor. That sentence is easy to skip and impossible
to audit: the reader cannot see *which* days the total covers without reading a
comma list of dates, and cannot see that a blank day is not a zero.

Two contracts are tested here:

* a day the run read nothing on is `no record`, never `0` -- a measured zero and
  an absence of records must not print the same;
* the per-day rows are the *same* accounting as the total, not a second one that
  can drift: their sum must equal the printed total exactly.

All fixtures are synthetic.
"""

import json
import subprocess
import sys
from pathlib import Path

import quotamirror


def usage_line(message_id, ts, input_tokens=10, output_tokens=0, cache_read=0, model="opus"):
    record = {
        "type": "assistant",
        "uuid": f"u-{message_id}-{ts}",
        "isSidechain": False,
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
        (root / name).write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
    return root


def test_a_window_with_no_recorded_day_prints_an_absence_instead_of_a_zero(tmp_path):
    # The store has records, but `--until` places the window past all of them.
    # Every day in the window is uncovered, so there is no number to print: a 0
    # here would be the one reading the command exists to prevent.
    root = write_transcripts(
        tmp_path / "projects",
        {"a.jsonl": [usage_line("m1", "2026-05-02T10:00:00Z", 10)]},
    )
    payload = quotamirror.recent_window(root, days=7, until="2026-06-10")

    assert payload["verdict"] == "no_records_in_window"
    assert payload["days_with_records"] == 0
    assert payload["total_tokens"] is None
    assert payload["tokens"] is None

    text = quotamirror.render_recent_window(payload, by_day=True)
    assert "TOTAL" not in text
    assert "not measured" in text
    # nothing was printed as a zero, so the text must not call anything "this zero"
    assert "this zero" not in text
    # the command must still place the window it could not fill
    assert "2026-06-04..2026-06-10" in text
    # and the per-day rows still name every day, none of them as a number
    assert "2026-06-10  " in text
    assert "no row carries a number" in text


def test_by_day_has_one_row_per_day_in_the_window_in_chronological_order(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m2", "2026-05-05T10:00:00Z", 7),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=5)

    days = [row["day"] for row in payload["by_day"]]
    assert days == ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05"]
    assert [row["recorded"] for row in payload["by_day"]] == [True, False, False, False, True]
    assert payload["by_day"][1]["tokens"] is None


def test_by_day_rows_sum_to_the_total_so_they_are_not_a_second_accounting(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                # one message written twice: the rows must use the merged value,
                # exactly as the total does
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m1", "2026-05-01T10:00:01Z", 10),
                usage_line("m2", "2026-05-03T10:00:00Z", 5),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=4)
    nested = quotamirror.nested_audit(root)

    row_sum = sum(row["tokens"] for row in payload["by_day"] if row["tokens"] is not None)
    assert row_sum == payload["total_tokens"] == 15
    assert payload["total_tokens"] == nested["totals"]["all"]["total_tokens"]
    # the window is anchored on the newest recorded day (05-03), so 05-01 is the
    # second row and carries the merged value of the message written twice
    assert payload["by_day"][0]["day"] == "2026-04-30"
    assert payload["by_day"][1]["tokens"] == 10


def test_a_blank_day_prints_no_record_and_a_recorded_day_prints_its_number(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 1234),
                usage_line("m2", "2026-05-03T10:00:00Z", 7),
            ]
        },
    )
    payload = quotamirror.recent_window(root, days=3)
    text = quotamirror.render_recent_window(payload, by_day=True)

    rows = {line.split()[0]: line for line in text.splitlines() if line.strip().startswith("2026-")}
    assert "1,234" in rows["2026-05-01"]
    assert "no record" in rows["2026-05-02"]
    # what follows the date is the word, not a number: a blank day is an absence
    assert rows["2026-05-02"].split("2026-05-02")[1].strip() == "no record"
    assert "7" in rows["2026-05-03"]
    # the rows are the total, and the line says so with both numbers
    assert "1,241" in text


def test_cli_by_day_adds_the_section_and_its_absence_leaves_the_output_unchanged(tmp_path):
    root = write_transcripts(
        tmp_path / "projects", {"a.jsonl": [usage_line("m1", "2026-05-02T10:00:00Z", 10)]}
    )
    script = str(Path(quotamirror.__file__).resolve())

    plain = subprocess.run(
        [sys.executable, script, "recent", "--path", str(root), "--days", "3"],
        capture_output=True,
        text=True,
        check=False,
    )
    with_flag = subprocess.run(
        [sys.executable, script, "recent", "--path", str(root), "--days", "3", "--by-day"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert plain.returncode == 0, plain.stderr
    assert with_flag.returncode == 0, with_flag.stderr
    assert "BY DAY" not in plain.stdout
    assert "BY DAY" in with_flag.stdout
    # same measurement, one extra view of it
    assert "TOTAL" in plain.stdout and "TOTAL" in with_flag.stdout
