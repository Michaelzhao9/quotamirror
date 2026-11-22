"""Tests for `quiet`: which days are silent on both sides, and which only look it.

`recent --by-day` can already say that a day carries no token record. It cannot
say *why*, and the honest answer used to be "undecidable": an absent record and
a holiday have the same shape inside a single store.

The receipt and the transcripts are two independent stores, and each one carries
a positive claim of its own. A day the receipt counts 500 messages on but the
transcripts hold no token for is not a holiday - the receipt just said so. That
is the whole point of this command: it never claims "quiet", it names which side
is silent and refuses to turn a missing record into a zero.

All fixtures are synthetic.
"""

import json

import quotamirror


def cache_with(days, last_computed=None):
    """A receipt carrying only the fields this command reads."""
    payload = {
        "version": 3,
        "dailyActivity": [
            {"date": date, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
            for date, messages in days
        ],
    }
    if last_computed is not None:
        payload["lastComputedDate"] = last_computed
    return payload


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


def test_a_day_both_sides_record_is_measured_and_listed_as_no_gap(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 700)]})
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400)]), scan)

    assert result["days_measured"] == 1
    assert result["days_activity_only"] == 0
    assert result["days_tokens_only"] == 0
    assert result["days_silent"] == 0
    assert result["rows"] == []
    assert result["verdict"] == "all_measured"


def test_activity_without_a_token_record_is_named_and_not_called_quiet(tmp_path):
    # The transcripts reach 05-02 and 05-06 but hold nothing for 05-03, a day the
    # receipt counts 500 messages on. That day is not quiet, whatever the cause.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-02T10:00:00Z"),
                usage_line("m2", "2026-05-06T10:00:00Z"),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-02", 100), ("2026-05-03", 500), ("2026-05-06", 100)]), scan
    )

    assert result["window"] == ["2026-05-02", "2026-05-06"]
    assert result["days_activity_only"] == 1
    assert result["verdict"] == "activity_without_tokens"
    row = next(row for row in result["rows"] if row["date"] == "2026-05-03")
    assert row["kind"] == "activity_only"
    assert row["messages"] == 500
    # The receipt counted messages; the transcripts counted nothing. The absence
    # is not a zero: the row must not carry 0 tokens.
    assert row["tokens"] is None


def test_tokens_without_an_activity_row_are_a_table_gap(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-02T10:00:00Z"),
                usage_line("m2", "2026-05-03T10:00:00Z", 30),
                usage_line("m3", "2026-05-04T10:00:00Z"),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-02", 100), ("2026-05-04", 100)]), scan
    )

    assert result["days_tokens_only"] == 1
    assert result["verdict"] == "tokens_without_activity"
    row = next(row for row in result["rows"] if row["date"] == "2026-05-03")
    assert row["kind"] == "tokens_only"
    assert row["messages"] is None
    assert row["tokens"] == 30


def test_a_day_silent_on_both_sides_carries_no_number(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "only.jsonl": [
                usage_line("m1", "2026-05-02T10:00:00Z", 700),
                usage_line("m2", "2026-05-04T10:00:00Z", 700),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-02", 100), ("2026-05-04", 400)]), scan
    )

    assert result["days_in_window"] == 3
    assert result["days_silent"] == 1
    assert result["verdict"] == "silent_days"
    silents = [row for row in result["rows"] if row["kind"] == "silent"]
    assert [row["date"] for row in silents] == ["2026-05-03"]
    assert silents[0]["messages"] is None
    assert silents[0]["tokens"] is None


def test_last_computed_date_extends_the_receipts_span_but_not_past_it(tmp_path):
    # The table was computed through 05-10 while the last activity row is 05-08:
    # 05-09 and 05-10 are days the receipt computed and found empty, which is a
    # stronger statement than a day outside its span.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-08T10:00:00Z"),
                usage_line("m2", "2026-05-10T10:00:00Z"),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-08", 500)], last_computed="2026-05-10"), scan
    )

    assert result["receipt_span"] == ["2026-05-08", "2026-05-10"]
    assert result["window"] == ["2026-05-08", "2026-05-10"]
    assert result["days_in_window"] == 3
    assert result["days_silent"] == 1
    assert result["days_tokens_only"] == 1
    assert result["days_measured"] == 1


def test_disjoint_spans_claim_no_window_and_no_silence(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-11-04T10:00:00Z")]})
    result = quotamirror.quiet_days(cache_with([("2026-04-04", 500)]), scan)

    assert result["window"] is None
    assert result["verdict"] == "no_overlap"
    assert result["rows"] == []
    assert result["days_silent"] == 0
    assert result["days_measured"] == 0


def test_no_transcripts_is_not_measured(tmp_path):
    scan = scan_of(tmp_path, {})
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400)]), scan)

    assert result["verdict"] == "no_transcripts"
    assert result["rows"] == []
    assert result["days_activity_only"] == 0
    assert result["days_measured"] == 0


def test_a_receipt_without_a_day_table_cannot_be_compared(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z")]})
    result = quotamirror.quiet_days({"version": 3}, scan)

    assert result["verdict"] == "no_day_table"
    assert result["rows"] == []
    assert result["window"] is None


def test_measured_tokens_equal_the_scans_own_day_table(tmp_path):
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 700),
                usage_line("m2", "2026-05-05T10:00:00Z", 300),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-03", 500), ("2026-05-04", 400), ("2026-05-05", 400)]), scan
    )

    expected = sum(int(scan["days"][d]["total_tokens"]) for d in ("2026-05-04", "2026-05-05"))
    assert result["measured_tokens"] == expected == 1000
    assert result["measured_messages"] == 800
    assert result["transcript_tokens_in_window"] == expected


def test_cli_quiet_prints_json_and_text(tmp_path, capsys):
    root = tmp_path / "transcripts"
    root.mkdir()
    lines = [
        usage_line("m1", "2026-05-02T10:00:00Z"),
        usage_line("m2", "2026-05-06T10:00:00Z"),
    ]
    (root / "a.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            cache_with([("2026-05-02", 100), ("2026-05-03", 500), ("2026-05-06", 100)])
        ),
        encoding="utf-8",
    )

    code = quotamirror.main(["quiet", "--path", str(cache_path), "--transcripts", str(root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "activity_without_tokens"
    assert payload["days_activity_only"] == 1

    code = quotamirror.main(["quiet", "--path", str(cache_path), "--transcripts", str(root)])
    assert code == 0
    text = capsys.readouterr().out
    assert "2026-05-03" in text
    assert "no token record" in text
    assert "VERDICT: activity_without_tokens" in text


def test_render_counts_the_days_only_one_store_reaches(tmp_path):
    # The receipt reaches 05-01 and the transcripts reach 05-06, but neither
    # store speaks for the day the other one alone records. Those days are
    # counted in the payload, and a text reader has to see the count: the
    # DAYS line adds up to days_in_window only, so without it the printed
    # arithmetic silently looks complete.
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z"),
                usage_line("m2", "2026-05-06T10:00:00Z"),
            ]
        },
    )
    result = quotamirror.quiet_days(
        cache_with([("2026-05-01", 400), ("2026-05-04", 400), ("2026-05-05", 400)]), scan
    )

    assert result["window"] == ["2026-05-04", "2026-05-05"]
    assert result["days_outside_window"] == 2

    text = quotamirror.render_quiet(result)
    outside = [line for line in text.splitlines() if "outside" in line]
    assert len(outside) == 1
    assert "2 day(s)" in outside[0]
    assert "not judged" in outside[0]


def test_cli_quiet_prints_the_outside_count(tmp_path, capsys):
    root = tmp_path / "transcripts"
    root.mkdir()
    lines = [
        usage_line("m1", "2026-05-04T10:00:00Z"),
        usage_line("m2", "2026-05-06T10:00:00Z"),
    ]
    (root / "a.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            cache_with([("2026-05-01", 400), ("2026-05-04", 400), ("2026-05-05", 400)])
        ),
        encoding="utf-8",
    )

    code = quotamirror.main(
        ["quiet", "--path", str(cache_path), "--transcripts", str(root)]
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "outside" in text
    assert "2 day(s)" in text
