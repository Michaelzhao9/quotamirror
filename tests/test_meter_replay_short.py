"""Tests for the replay axis on days whose record set is short.

The witnessed share (`test_meter_replay.py`) refuses every day that is not
`exact`, and it is right to: a dedupe over a lossy record set cannot describe the
bill. But refusing the *share* is not the same as having nothing to say. On a
short day the corpus still holds records, and those records still dedupe, so the
repeat rate among the records that survived is measurable - it is a rate over
the records present, not a share of the tokens the day bills.

That difference matters because most of the bill sits on days that are not exact.
Until the surviving records are measured, a repeat whose first write landed on a
short day contributes to no number at all, and a reader cannot tell whether such
days are repeat-free or simply unmeasured.

All fixtures are synthetic.
"""

import json

from test_meter_replay import cache_with, scan_of, usage_line

import quotamirror


def replay_of(result):
    return result["arbitration"]["replay"]


def exact_day_plus_short_day(tmp_path, short_lines):
    """2026-05-04 is exact (2 records of 1,000 for one message, billed 2,000).

    2026-05-05 bills 900 but holds only the records in `short_lines`.
    """
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1000),
            *short_lines,
        ],
    )
    cache = cache_with([("2026-05-04", 2000), ("2026-05-05", 900)])
    return quotamirror.quiet_days(cache, scan, min_tokens=0)


def short_of(result):
    return replay_of(result)["short_days"]


def test_a_short_day_is_measured_over_the_records_it_holds(tmp_path):
    result = exact_day_plus_short_day(
        tmp_path, [usage_line("m2", "2026-05-05T10:00:00Z", 100)]
    )
    short = short_of(result)
    assert short["days"] == 1
    assert short["written_tokens"] == 100
    assert short["deduped_tokens"] == 100
    assert short["replay_tokens"] == 0
    assert short["share"] == 0.0
    assert short["billed_tokens"] == 900
    assert short["scope"] == "records_present"
    assert short["verdict"] == "no_repeat_observed"


def test_short_day_records_are_not_pooled_into_the_witnessed_share(tmp_path):
    result = exact_day_plus_short_day(
        tmp_path,
        [
            usage_line("m2", "2026-05-05T10:00:00Z", 50),
            usage_line("m2", "2026-05-05T10:00:01Z", 50),
        ],
    )
    replay = replay_of(result)
    # The witnessed share still describes only the exact day.
    assert replay["written_tokens"] == 2000
    assert replay["deduped_tokens"] == 1000
    assert replay["replay_share"] == 0.5
    # The short day's own repeats are reported beside it, not inside it.
    short = replay["short_days"]
    assert short["days"] == 1
    assert short["written_tokens"] == 100
    assert short["deduped_tokens"] == 50
    assert short["replay_tokens"] == 50
    assert short["share"] == 0.5
    assert short["billed_tokens"] == 900
    assert short["verdict"] == "repeat_measured"


def test_with_no_exact_day_the_short_days_are_still_measured(tmp_path):
    # Nothing is witnessed, so the share stays refused - but the records that
    # do survive on the short day are still counted.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 50),
            usage_line("m1", "2026-05-04T10:00:01Z", 50),
        ],
    )
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 900)]), scan, min_tokens=0)
    replay = replay_of(result)
    assert replay["available"] is False
    assert replay["verdict"] == "no_exact_day"
    assert replay["replay_share"] is None
    short = replay["short_days"]
    assert short["days"] == 1
    assert short["replay_tokens"] == 50
    assert short["share"] == 0.5
    assert short["verdict"] == "repeat_measured"


def test_an_absent_day_is_counted_and_holds_no_records_to_measure(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1000),
            usage_line("m2", "2026-05-05T10:00:00Z", 100),
        ],
    )
    cache = cache_with([("2026-05-04", 2000), ("2026-05-05", 900), ("2026-05-06", 700)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=0)
    short = short_of(result)
    assert short["days"] == 1
    assert short["absent_days"] == 1
    assert short["billed_tokens"] == 900
    assert short["written_tokens"] == 100


def test_no_short_day_leaves_the_block_empty_and_unprinted(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    short = short_of(payload)
    assert short["days"] == 0
    assert short["verdict"] == "no_short_day"
    assert short["share"] is None
    assert "repeat/short" not in quotamirror.render_quiet(payload)


def test_quiet_text_prints_the_short_day_rate_apart_from_the_share(tmp_path):
    result = exact_day_plus_short_day(
        tmp_path,
        [
            usage_line("m2", "2026-05-05T10:00:00Z", 50),
            usage_line("m2", "2026-05-05T10:00:01Z", 50),
        ],
    )
    text = quotamirror.render_quiet(result)
    assert "repeat/short" in text
    assert "50 of the 100 token(s)" in text
    assert "1 short day(s)" in text
    assert "records present" in text
    # The share of the bill is refused on this line, so the bill is named.
    assert "900" in text
    json.dumps(result, indent=2, sort_keys=True)


def test_quiet_text_states_the_short_day_rate_even_when_the_share_is_refused(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 100)])
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 900)]), scan, min_tokens=0)
    text = quotamirror.render_quiet(payload)
    assert "repeat share not measured" in text
    assert "repeat/short" in text
