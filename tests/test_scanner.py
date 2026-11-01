"""Tests for `scan_usage`: one reader for the Claude transcript corpus.

`nested` and `compare --claude-transcripts` each grew their own ~90-line copy of
the same loop (open every *.jsonl, keep lines that mention usage, merge by
message id keeping the larger value per bucket, bucket the days). The only thing
stopping the two copies from drifting was an agreement test on the totals.

This module pins the reader itself, and adds the one thing neither copy could
answer: what a day's tokens were, per model and per bucket -- which is what the
cache's day table cannot tell you and the 19.6B shared-day gap needs.
"""

import json

import quotamirror


def usage(input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }


def record(
    message_id,
    *,
    model="claude-x",
    tokens=None,
    ts="2026-05-01T10:00:00Z",
    uuid="u1",
    sidechain=False,
):
    return {
        "type": "assistant",
        "uuid": uuid,
        "isSidechain": sidechain,
        "timestamp": ts,
        "message": {"id": message_id, "model": model, "usage": tokens or usage()},
    }


def write_lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def test_a_repeated_message_id_is_counted_once_keeping_the_larger_value_per_bucket(tmp_path):
    write_lines(
        tmp_path / "s.jsonl",
        [
            record("m1", tokens=usage(output_tokens=10, cache_read=100)),
            record("m1", tokens=usage(output_tokens=10, cache_read=400)),
        ],
    )

    scan = quotamirror.scan_usage(tmp_path)

    assert scan["records"] == 2
    assert scan["messages"] == 1
    assert scan["tokens"]["cache_read_input_tokens"] == 400
    assert scan["tokens"]["output_tokens"] == 10
    # The merge is per bucket: taking the later record wholesale would have
    # dropped nothing here, so the assertion is on the sum, not on one key.
    assert sum(scan["tokens"].values()) == 410


def test_a_day_reports_its_tokens_per_model_and_per_bucket(tmp_path):
    write_lines(
        tmp_path / "s.jsonl",
        [
            record(
                "m1",
                model="claude-a",
                tokens=usage(output_tokens=5, cache_read=1000),
                ts="2026-05-01T09:00:00Z",
            ),
            record(
                "m2",
                model="claude-b",
                tokens=usage(output_tokens=7),
                ts="2026-05-02T09:00:00Z",
            ),
        ],
    )

    scan = quotamirror.scan_usage(tmp_path)

    day = scan["days"]["2026-05-01"]
    assert day["total_tokens"] == 1005
    assert day["by_bucket"]["cache_read_input_tokens"] == 1000
    assert day["by_model"]["claude-a"]["total_tokens"] == 1005
    assert day["by_model"]["claude-a"]["by_bucket"]["output_tokens"] == 5
    assert "claude-b" not in day["by_model"]
    # A day table that does not add up to the stamped total is a second,
    # slightly different quantity wearing the same name.
    assert sum(d["total_tokens"] for d in scan["days"].values()) == sum(
        scan["tokens"].values()
    )


def test_a_message_recorded_on_two_days_is_attributed_to_the_earlier_one(tmp_path):
    write_lines(
        tmp_path / "s.jsonl",
        [
            record("m1", tokens=usage(output_tokens=3), ts="2026-05-03T23:59:00Z"),
            record("m1", tokens=usage(output_tokens=3), ts="2026-05-04T00:01:00Z"),
        ],
    )

    scan = quotamirror.scan_usage(tmp_path)

    assert scan["messages"] == 1
    assert list(scan["days"]) == ["2026-05-03"]
    assert scan["days"]["2026-05-03"]["total_tokens"] == 3


def test_tokens_from_untimed_records_are_reported_outside_the_day_table(tmp_path):
    write_lines(
        tmp_path / "s.jsonl",
        [
            record("m1", tokens=usage(output_tokens=4), ts="2026-05-01T00:00:00Z"),
            record("m2", tokens=usage(output_tokens=6), ts=None),
        ],
    )

    scan = quotamirror.scan_usage(tmp_path)

    # A day table that quietly omits undated tokens reads as if it accounts for
    # everything. The remainder is printed instead of being absorbed.
    assert scan["unattributed_tokens"] == 6
    assert sum(d["total_tokens"] for d in scan["days"].values()) == 4
    assert sum(d["total_tokens"] for d in scan["days"].values()) + scan[
        "unattributed_tokens"
    ] == sum(scan["tokens"].values())


def test_a_day_reports_how_much_of_its_record_sum_is_duplicate(tmp_path):
    write_lines(
        tmp_path / "s.jsonl",
        [
            record("m1", tokens=usage(cache_read=1000), ts="2026-05-01T00:00:00Z"),
            record("m1", tokens=usage(cache_read=1000), ts="2026-05-01T00:00:01Z"),
            record("m2", tokens=usage(output_tokens=10), ts="2026-05-01T00:00:02Z"),
        ],
    )

    scan = quotamirror.scan_usage(tmp_path)

    day = scan["days"]["2026-05-01"]
    assert day["records"] == 3
    assert day["naive_tokens"] == 2010
    assert day["total_tokens"] == 1010
    assert day["duplicate_tokens"] == 1000
    # The per-day duplicate counts are the global one, split, not a second number.
    assert sum(d["duplicate_tokens"] for d in scan["days"].values()) == (
        scan["naive_tokens"] - sum(scan["tokens"].values())
    )
