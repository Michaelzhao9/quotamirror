"""Tests for the meter check's replay axis: of the tokens a witnessed day bills,
how many are the same message written twice?

The witness axis (`test_meter_witness.py`) already tells apart three states of a
billed day: exact (the receipt's total equals the sum of the usage records on
disk), remnant (records are short), absent (no record at all). On a witnessed
day the record set is demonstrably complete, so the difference between the
record sum and the deduped total on that day cannot be missing evidence - it is
the same message written more than once, which is how a streamed response lands
in the corpus.

That difference is worth its own number because it is the only way to say, on a
day whose evidence survives, that the tokens billed are not tokens spent: the
record sum counts every streamed write. It must never be computed on a day with
short or absent records - a dedupe over a lossy record set is a floor on the
repeat count, not a share of it.

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


def replay_of(result):
    return result["arbitration"]["replay"]


def test_a_witnessed_day_measures_its_own_repeated_writes(tmp_path):
    # One message, written twice by the streamed response: the record sum is
    # 2,000 and the deduped total 1,000, and the receipt bills the record sum.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1000),
        ],
    )
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 2000)]), scan, min_tokens=0)
    assert result["arbitration"]["witness"]["days"]["exact"] == 1
    replay = replay_of(result)
    assert replay["available"] is True
    assert replay["days"] == 1
    assert replay["written_tokens"] == 2000
    assert replay["deduped_tokens"] == 1000
    assert replay["replay_tokens"] == 1000
    assert replay["replay_share"] == 0.5
    assert replay["days_excluded"] == 0
    assert replay["verdict"] == "repeat_measured"


def test_a_short_day_is_counted_as_excluded_and_never_pooled(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1000),
            usage_line("m2", "2026-05-05T10:00:00Z", 100),
        ],
    )
    cache = cache_with([("2026-05-04", 2000), ("2026-05-05", 900)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=0)
    witness = result["arbitration"]["witness"]
    assert witness["days"]["exact"] == 1
    assert witness["days"]["remnant"] == 1
    replay = replay_of(result)
    assert replay["days"] == 1
    # The remnant day's 100 written tokens are not in either side of the share.
    assert replay["written_tokens"] == 2000
    assert replay["deduped_tokens"] == 1000
    assert replay["days_excluded"] == 1


def test_without_a_witnessed_day_the_share_is_refused_not_zeroed(tmp_path):
    # Every billed day is short, so a dedupe here would measure the loss, not
    # the repeats.
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 100)])
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 900)]), scan, min_tokens=0)
    replay = replay_of(result)
    assert replay["available"] is False
    assert replay["verdict"] == "no_exact_day"
    assert replay["replay_share"] is None
    assert replay["replay_tokens"] == 0
    assert "incomplete" in replay["reason"]


def test_a_witnessed_day_without_repeats_reports_zero_over_one_day(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    replay = replay_of(result)
    assert replay["available"] is True
    assert replay["days"] == 1
    assert replay["replay_tokens"] == 0
    assert replay["replay_share"] == 0.0
    assert replay["verdict"] == "no_repeat_observed"


def test_a_below_floor_day_does_not_lend_its_repeats_to_the_share(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1000),
            usage_line("m9", "2026-05-05T10:00:00Z", 5),
            usage_line("m9", "2026-05-05T10:00:01Z", 5),
        ],
    )
    cache = cache_with([("2026-05-04", 2000), ("2026-05-05", 10)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=1000)
    replay = replay_of(result)
    assert replay["days"] == 1
    assert replay["written_tokens"] == 2000
    assert replay["days_excluded"] == 0


def test_quiet_text_states_the_share_with_its_denominator(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1000),
        ],
    )
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 2000)]), scan, min_tokens=0)
    text = quotamirror.render_quiet(payload)
    assert "repeat" in text
    assert "1,000 of 2,000 token(s) written on 1 day(s)" in text
    assert "50.0%" in text


def test_quiet_text_refuses_the_share_in_words_when_no_day_is_witnessed(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 100)])
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 900)]), scan, min_tokens=0)
    text = quotamirror.render_quiet(payload)
    assert "repeat share not measured" in text
    # The refused share must not be rendered as a measured zero.
    assert "token(s) written on" not in text
    json.dumps(payload, indent=2, sort_keys=True)
