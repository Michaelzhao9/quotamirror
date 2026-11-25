"""Tests for the meter check's witness axis: does the corpus still hold the
records the receipt summed?

The aggregate ratio divides the receipt's day table by whatever the transcript
corpus still contains. That is only a meter disagreement if the corpus holds
the same records the meter read. This machine can tell the two apart: when a
day's usage records survive, the receipt's day total matches their sum exactly
(the receipt counts every write), so an unequal day is a day whose records are
partly or entirely gone.

The axis therefore splits the billed days into three states - exact, remnant,
absent - and reports the receipt tokens behind each, so a total ratio is never
read as a meter factor when most of its weight sits on days no store can check.

All fixtures are synthetic.
"""

import json

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
        "uuid": f"u-{message_id}",
        "isSidechain": False,
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


def witness_of(result):
    return result["arbitration"]["witness"]


def test_exact_day_is_reported_as_witnessed_to_the_digit(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    witness = witness_of(result)
    assert witness["days"]["exact"] == 1
    assert witness["days"]["remnant"] == 0
    assert witness["days"]["absent"] == 0
    assert witness["tokens"]["exact"] == 1000
    assert witness["checked_share"] == 1.0
    assert witness["verdict"] == "every_day_exact"


def test_a_day_with_a_short_record_sum_is_remnant_and_its_residual_is_counted(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 200)])
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    witness = witness_of(result)
    assert witness["days"]["remnant"] == 1
    assert witness["days"]["exact"] == 0
    assert witness["tokens"]["remnant"] == 1000
    assert witness["residual_tokens"] == 800
    assert witness["checked_share"] == 0.0
    assert witness["verdict"] == "no_exact_day"


def test_a_day_with_no_record_at_all_is_absent_not_remnant(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    cache = cache_with([("2026-05-04", 1000), ("2026-05-05", 500)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=0)
    witness = witness_of(result)
    assert witness["days"]["exact"] == 1
    assert witness["days"]["absent"] == 1
    assert witness["days"]["remnant"] == 0
    assert witness["tokens"]["absent"] == 500
    # An absent day cannot be subtracted from anything: it is not a residual.
    assert witness["residual_tokens"] == 0
    assert witness["receipt_total"] == 1500
    assert witness["checked_share"] == round(1000 / 1500, 4)


def test_verdict_says_the_meter_matches_where_evidence_survives(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m2", "2026-05-05T10:00:00Z", 100),
        ],
    )
    cache = cache_with([("2026-05-04", 1000), ("2026-05-05", 900), ("2026-05-06", 400)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=0)
    witness = witness_of(result)
    assert witness["days"]["exact"] == 1
    assert witness["days"]["remnant"] == 1
    assert witness["days"]["absent"] == 1
    assert witness["verdict"] == "exact_where_evidence_survives"
    assert witness["residual_tokens"] == 800
    assert witness["unwitnessed_tokens"] == 900 + 400


def test_days_below_the_floor_do_not_join_the_witness_split(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    cache = cache_with([("2026-05-04", 1000), ("2026-05-05", 5)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=1000)
    witness = witness_of(result)
    assert witness["days"]["exact"] == 1
    assert witness["days"]["absent"] == 0
    assert witness["receipt_total"] == 1000


def test_quiet_text_prints_the_witness_split(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m2", "2026-05-05T10:00:00Z", 100),
        ],
    )
    cache = cache_with([("2026-05-04", 1000), ("2026-05-05", 900), ("2026-05-06", 400)])
    payload = quotamirror.quiet_days(cache, scan, min_tokens=0)
    text = quotamirror.render_quiet(payload)
    assert "witness" in text
    assert "1 day(s) exact" in text
    assert "exact where evidence survives" in " ".join(text.split())


def test_a_short_day_is_json_serialisable_and_named_as_a_string(tmp_path):
    # The receipt's own day keys are dates, not strings: the real run reached the
    # JSON writer with a date object in `worst_day` and crashed there.
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 200)])
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    witness = witness_of(result)
    assert witness["worst_day"] == "2026-05-04"
    json.dumps(result, indent=2, sort_keys=True)
