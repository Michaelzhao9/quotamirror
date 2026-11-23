"""Tests for `quiet`'s meter check: do the two token meters count the same thing?

`quiet` prints the receipt's in-window token total beside the transcripts' total.
That pair alone invites the reader to subtract them, and the difference has a
name only if it is measured. Both stores can differ in exactly one mechanical
way that this machine can see: a streamed response writes its usage record more
than once, so the transcript corpus holds a *record sum* (every write) and a
*deduped* total (each message id once, largest value per bucket).

So this block compares the receipt's day table against BOTH transcript figures
over the days where both meters state a number, and it says `neither` when
neither figure is within tolerance. It never names a cause that the numbers do
not support, and it never calls the receipt wrong for counting writes.

All fixtures are synthetic.
"""

import json

import quotamirror


def cache_with(days, tokens):
    return {
        "version": 3,
        "dailyActivity": [
            {"date": date, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
            for date, messages in days
        ],
        "dailyModelTokensVersion": 3,
        "dailyModelTokens": [
            {"date": date, "tokensByModel": {"claude-test": total}} for date, total in tokens
        ],
    }


def usage_line(message_id, ts, input_tokens=10):
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


def test_the_receipt_matching_the_record_sum_is_named_as_such(tmp_path):
    # One message, written twice (a streamed response writing usage twice).
    # The transcripts hold 1,000 deduped and 2,000 as written; the receipt
    # states 2,000. That is the record sum, not the deduped total.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)],
            "b.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)],
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 400)], tokens=[("2026-05-04", 2000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]

    assert block["verdict"] == "record_sum_matches"
    assert block["days_compared"] == 1
    assert block["receipt_tokens"] == 2000
    assert block["transcript_deduped"] == 1000
    assert block["transcript_record_sum"] == 2000

    text = quotamirror.render_quiet(result)
    assert "record sum" in text
    assert "not explained" not in text


def test_the_receipt_matching_the_deduped_total_is_named_as_such(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)],
            "b.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)],
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 400)], tokens=[("2026-05-04", 1000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]

    assert block["verdict"] == "deduped_matches"
    assert block["receipt_tokens"] == 1000
    assert block["transcript_record_sum"] == 2000
    assert "one message counted once" in quotamirror.render_quiet(result)


def test_a_gap_that_is_not_a_duplicate_is_left_unexplained(tmp_path):
    # 5,000 in the receipt against 1,000/1,000 in the transcripts: neither
    # candidate is within tolerance, so no cause is claimed.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)],
            "b.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)],
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 400)], tokens=[("2026-05-04", 5000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]

    assert block["verdict"] == "neither"
    assert block["ratio_record_sum"] == 2.5
    assert block["ratio_deduped"] == 5.0

    text = quotamirror.render_quiet(result)
    assert "neither" in text
    assert "not explained here" in text


def test_a_day_below_the_floor_is_counted_and_does_not_decide(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
                usage_line("m2", "2026-05-05T10:00:00Z", 10),
            ],
            "b.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000)],
        },
    )
    result = quotamirror.quiet_days(
        cache_with(
            [("2026-05-04", 400), ("2026-05-05", 400)],
            tokens=[("2026-05-04", 2_000_000), ("2026-05-05", 10)],
        ),
        scan,
        min_tokens=1_000_000,
    )
    block = result["arbitration"]

    assert block["verdict"] == "record_sum_matches"
    assert block["days_compared"] == 1
    assert block["days_below_floor"] == 1
    assert block["receipt_tokens"] == 2_000_000
    assert "1 day(s) below" in quotamirror.render_quiet(result)


def test_no_day_is_stated_by_both_token_meters(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-06T10:00:00Z", 1000)]})
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 400)], tokens=[("2026-05-04", 2000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]

    assert block["verdict"] == "no_shared_token_days"
    assert block["days_compared"] == 0
    text = quotamirror.render_quiet(result)
    assert "receipt / as written" not in text


def test_a_receipt_without_a_token_meter_attributes_nothing(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)]})
    cache = cache_with([("2026-05-04", 400)], tokens=[])
    del cache["dailyModelTokens"]
    result = quotamirror.quiet_days(cache, scan, min_tokens=0)

    assert result["arbitration"]["verdict"] == "no_token_meter"
    assert result["arbitration"]["receipt_tokens"] is None
    assert "no_token_meter" in quotamirror.render_quiet(result)


def test_the_tolerance_in_force_is_printed_and_settable(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 1000)]})
    cache = cache_with([("2026-05-04", 400)], tokens=[("2026-05-04", 1300)])

    tight = quotamirror.quiet_days(cache, scan, min_tokens=0, tolerance=0.02)
    assert tight["arbitration"]["verdict"] == "neither"

    loose = quotamirror.quiet_days(cache, scan, min_tokens=0, tolerance=0.5)
    # One write, so both transcript figures are the same number: at 50% both match.
    assert loose["arbitration"]["verdict"] == "both_match"
    assert "50%" in quotamirror.render_quiet(loose)


def test_a_scan_without_a_record_sum_is_refused_not_substituted(tmp_path):
    # An older or synthetic payload may carry only the deduped total. Treating
    # it as the record sum would make the two candidates identical and turn a
    # missing measurement into a "both_match" verdict.
    scan = {"days": {"2026-05-04": {"total_tokens": 1000}}}
    result = quotamirror.quiet_days(
        cache_with([("2026-05-04", 400)], tokens=[("2026-05-04", 1000)]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]

    assert block["verdict"] == "no_record_sum"
    assert block["available"] is False
    assert block["transcript_record_sum"] is None
    assert "no_record_sum" in quotamirror.render_quiet(result)


def test_cli_refuses_a_negative_floor_with_exit_2(tmp_path, capsys):
    root = tmp_path / "transcripts"
    root.mkdir()
    cache = tmp_path / "stats-cache.json"
    cache.write_text(json.dumps(cache_with([("2026-05-04", 400)], tokens=[])), encoding="utf-8")
    code = quotamirror.main(
        ["quiet", "--path", str(cache), "--transcripts", str(root), "--min-tokens", "-5"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err
    assert "min_tokens" in captured.err
