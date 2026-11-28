"""Tests for the composition of the meter check's aggregate ratio.

`quiet` prints one ratio for a whole comparison - `receipt / as written 3.01x`
on this machine - and the reader takes it as a factor between two meters. It is
not one. The same screen splits the billed days a few lines later into exact,
short and absent, and the aggregate divides the entire bill by the records that
survived. Here the 27 days the meter check decides hold 16 exact days, where the
ratio is 1.00x to the digit, and 11 short ones, and the gap the ratio names is
exactly the amount the witness line counts as missing - 17,041,262,748 tokens,
the same figure printed twice on one screen and linked by nothing.

Pinned here:

* the payload splits the deciding days by whether their records survive, and
  states the ratio over the days that do;
* the text prints that split beside the aggregate, so the ratio cannot be read
  as a constant factor between two meters;
* the short-day gap appears once on the screen: when it is the same day set the
  witness line counts, the text points at that line instead of printing the
  figure again, and it prints its own number when the day sets differ.

All fixtures are synthetic.
"""

import json
from datetime import date

import quotamirror


def cache_with(rows, messages=10):
    """rows: [(date, tokens)]"""
    return {
        "version": 5,
        "dailyActivity": [
            {"date": date, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
            for date, _ in rows
        ],
        "dailyModelTokensVersion": 5,
        "dailyModelTokens": [
            {"date": date, "tokensByModel": {"claude-test": tokens}} for date, tokens in rows
        ],
    }


def usage_line(message_id, ts, input_tokens, model="claude-test"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
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


def mixed_corpus(tmp_path):
    """One exact day and one short day, both inside the window."""
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m2", "2026-05-05T10:00:00Z", 400),
        ],
    )
    cache = cache_with([("2026-05-04", 1000), ("2026-05-05", 1000)])
    return quotamirror.quiet_days(cache, scan, min_tokens=0)


def test_meter_check_splits_its_ratio_by_surviving_evidence(tmp_path):
    block = mixed_corpus(tmp_path)["arbitration"]
    evidence = block["evidence"]
    assert evidence["verdict"] == "mixed"
    assert evidence["days_compared"] == 2
    assert evidence["exact_days"] == 1
    assert evidence["exact_receipt"] == 1000
    assert evidence["exact_written"] == 1000
    assert evidence["exact_ratio"] == 1.0
    assert evidence["short_days"] == 1
    assert evidence["short_receipt"] == 1000
    assert evidence["short_gap"] == 600
    # The witness axis counts the same 600 on the same day, so the two numbers
    # a reader sees on one screen are one quantity.
    assert evidence["gap_is_the_witness_residual"] is True


def test_text_prints_the_exact_day_ratio_beside_the_aggregate(tmp_path):
    text = flat(quotamirror.render_quiet(mixed_corpus(tmp_path)))
    # "exact" is exact against the record sum, and the row now says so: the
    # per-day row below it divides by the deduped figure instead.
    assert "1 of 2 day(s) match the record sum exactly" in text
    assert "on those the ratio is 1.00x" in text


def test_the_short_day_gap_is_printed_once_on_the_screen(tmp_path):
    # The witness line already carries 600 as the missing amount. Repeating it
    # in the evidence line would print one quantity twice without saying so.
    text = flat(quotamirror.render_quiet(mixed_corpus(tmp_path)))
    assert text.count("600") == 1
    assert "the missing amount the witness line counts" in text


def test_a_short_day_the_window_leaves_out_is_printed_as_its_own_number():
    # The witness axis is deliberately not window-limited, the meter check is:
    # when a short day sits outside the window the two numbers are different
    # quantities, and the line must not call one the other.
    first = date(2026, 5, 1)
    fourth = date(2026, 5, 4)
    fifth = date(2026, 5, 5)
    block = quotamirror._quiet_meter_check(
        {first: 1000, fourth: 1000, fifth: 1000},
        {fourth: 800, fifth: 1000},
        {first: 100, fourth: 800, fifth: 1000},
        (fourth, fifth),
        0,
        0.02,
    )
    evidence = block["evidence"]
    assert evidence["short_gap"] == 200
    assert block["witness"]["residual_tokens"] == 1100
    assert evidence["gap_is_the_witness_residual"] is False
    line = quotamirror._evidence_line(block)
    assert "short by 200 token(s)" in line
    assert "not the amount the witness line counts" in line


def test_no_evidence_line_when_every_day_holds_its_records(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    cache = cache_with([("2026-05-04", 1000)])
    text = flat(quotamirror.render_quiet(quotamirror.quiet_days(cache, scan, min_tokens=0)))
    assert "on those the ratio is" not in text
