"""`quiet` names its silent day set with a first..last range. That range is a
description of the set only when the set has no holes.

On this machine 138 silent days print as `2026-05-05 .. 2026-10-18`. That span
holds 167 days and 29 of them carry a record (21 measured days and 8 days the
receipt bills, whose rows the same screen lists one by one). A reader who counts
the dates in the range gets a number the line's own count contradicts, and a
reader who does not count reads 29 working days as silent.

Pinned here:

* a silent set in more than one run states how many runs it has, so the range
  cannot be read as the set;
* the count of days inside the span that do carry a record is printed, and it
  is arithmetic the reader can check against the span;
* a contiguous silent set keeps the plain range, unchanged, because there the
  range is the set.

All fixtures are synthetic.
"""

import json

import quotamirror


def cache_with(dates, tokens=1000, messages=10):
    return {
        "version": 5,
        "dailyActivity": [
            {"date": day, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
            for day in dates
        ],
        "dailyModelTokensVersion": 5,
        "dailyModelTokens": [
            {"date": day, "tokensByModel": {"claude-test": tokens}} for day in dates
        ],
    }


def usage_line(message_id, ts, tokens):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": "claude-test",
            "usage": {
                "input_tokens": tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def scan_of(tmp_path, lines):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return quotamirror.scan_usage(root)


def flat(text):
    return " ".join(text.split())


def interleaved(tmp_path):
    """Billed and recorded on 05-04, 05-06, 05-08; 05-05 and 05-07 on neither."""
    days = ["2026-05-04", "2026-05-06", "2026-05-08"]
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m2", "2026-05-06T10:00:00Z", 1000),
            usage_line("m3", "2026-05-08T10:00:00Z", 1000),
        ],
    )
    return quotamirror.quiet_days(cache_with(days), scan, min_tokens=0)


def contiguous(tmp_path):
    """Silent days 05-05 and 05-06 sit between two days with records."""
    days = ["2026-05-04", "2026-05-07"]
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m2", "2026-05-07T10:00:00Z", 1000),
        ],
    )
    return quotamirror.quiet_days(cache_with(days), scan, min_tokens=0)


def test_a_silent_set_with_holes_states_how_many_runs_it_has(tmp_path):
    text = flat(quotamirror.render_quiet(interleaved(tmp_path)))
    assert "silent 2 day(s) in 2 run(s), 2026-05-05 .. 2026-05-07" in text
    # The old line claimed the span itself was silent.
    assert "silent 2 day(s): 2026-05-05 .. 2026-05-07" not in text


def test_the_matrix_of_record_days_inside_the_span_is_printed(tmp_path):
    text = flat(quotamirror.render_quiet(interleaved(tmp_path)))
    # 05-06 holds a record; 3 day(s) span - 2 day(s) silent = 1.
    assert "1 day(s) holding a record inside that span" in text
    assert "do not bound the set" in text


def test_a_contiguous_silent_set_keeps_the_plain_range(tmp_path):
    text = flat(quotamirror.render_quiet(contiguous(tmp_path)))
    assert "silent 2 day(s): 2026-05-05 .. 2026-05-06" in text
    assert "run(s)" not in text.split("VERDICT")[0]
