"""Tests for the meter check's per-day axis.

The aggregate comparison compares two sums taken over the same days. That is
weaker than it looks: two days can move in opposite directions (the receipt
heavy on one, the transcripts heavy on the other) and cancel exactly, so the
ratio lands on 1.00 while the two meters agree on no single day. This axis
states, per deciding day, whether that day's own pair is within tolerance, and
it never upgrades a cancelled-out aggregate to agreement.

All fixtures are synthetic.
"""

import json

import quotamirror


def cache_with(tokens):
    return {
        "version": 3,
        "dailyActivity": [
            {"date": date, "messageCount": 10, "sessionCount": 1, "toolCallCount": 0}
            for date, _ in tokens
        ],
        "dailyModelTokensVersion": 3,
        "dailyModelTokens": [
            {"date": date, "tokensByModel": {"claude-test": total}} for date, total in tokens
        ],
    }


def usage_line(message_id, ts, input_tokens):
    return {
        "type": "assistant",
        "uuid": f"u-{message_id}",
        "isSidechain": False,
        "timestamp": ts,
        "message": {
            "id": message_id,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def scan_of(tmp_path, lines_by_file):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    for name, lines in lines_by_file.items():
        (root / name).write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
    return quotamirror.scan_usage(root)


def test_two_days_that_cancel_out_are_not_called_agreement(tmp_path):
    # Day A: receipt 2,000,000, transcripts 1,000,000. Day B the other way
    # round. Both sums are 3,000,000, so the aggregate ratio is exactly 1.00 -
    # and neither day agrees with its own pair.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 2_000_000),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 2_000_000), ("2026-05-05", 1_000_000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["verdict"] == "both_match"
    assert block["ratio_deduped"] == 1.0
    assert block["per_day_verdict"] == "aggregate_only"
    assert block["days_matching_individually"] == 0
    assert block["days_receipt_heavy"] == 1
    assert block["days_transcript_heavy"] == 1
    assert block["max_day_ratio"] == 2.0
    assert block["min_day_ratio"] == 0.5


def test_a_day_that_agrees_is_counted_beside_a_day_that_does_not(tmp_path):
    # Aggregates match exactly (3,000,000 vs 3,000,000) because the 1.5x and
    # 0.5x days cancel; the middle day is the only one that agrees.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 1_000_000),
                usage_line("m3", "2026-05-06T10:00:00Z", 1_000_000),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with(
            [
                ("2026-05-04", 1_000_000),
                ("2026-05-05", 1_500_000),
                ("2026-05-06", 500_000),
            ]
        ),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["verdict"] == "both_match"
    assert block["per_day_verdict"] == "aggregate_only"
    assert block["days_compared"] == 3
    assert block["days_matching_individually"] == 1


def test_every_day_agreeing_is_named_per_day(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 2_000_000),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 1_000_000), ("2026-05-05", 2_000_000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["per_day_verdict"] == "every_day_agrees"
    assert block["days_matching_individually"] == 2
    assert block["max_day_ratio"] == 1.0
    assert block["min_day_ratio"] == 1.0


def test_no_shared_day_leaves_the_per_day_axis_unmeasured(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 10)]})
    result = quotamirror.quiet_days(
        cache_with([("2026-06-01", 5_000_000)]), scan, min_tokens=0
    )
    block = result["arbitration"]
    assert block["verdict"] == "no_shared_token_days"
    assert block["per_day_verdict"] == "not_measured"
    assert block["days_matching_individually"] is None


def test_days_below_the_floor_do_not_enter_the_per_day_axis(tmp_path):
    # The 10-token day would be 2.0x if it voted; it is below the floor.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 2_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 10),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 2_000_000), ("2026-05-05", 20)]),
        scan,
        min_tokens=1_000_000,
    )
    block = result["arbitration"]
    assert block["days_compared"] == 1
    assert block["days_matching_individually"] == 1
    assert block["max_day_ratio"] == 1.0


def test_the_extreme_days_carry_their_dates(tmp_path):
    # A ratio with no date cannot be checked against anything, so each extreme
    # names the day it came from.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 2_000_000),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 2_000_000), ("2026-05-05", 1_000_000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["max_day"] == "2026-05-04"
    assert block["min_day"] == "2026-05-05"
    text = quotamirror.render_quiet(result)
    per_day = next(line for line in text.splitlines() if "per day" in line)
    assert "2026-05-04" in per_day and "2026-05-05" in per_day


def test_the_per_day_line_is_printed(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 2_000_000),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 2_000_000), ("2026-05-05", 1_000_000)]),
        scan,
        min_tokens=0,
    )
    text = quotamirror.render_quiet(result)
    per_day = [line for line in text.splitlines() if "per day" in line]
    assert per_day, text
    assert "0 of 2" in per_day[0]
    assert "aggregate_only" in per_day[0]
