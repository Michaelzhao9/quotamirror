"""Tests for `crosscheck_days`: does a cache day hold records or messages?

Synthetic repeated transcript writes make a cache day match the naive record
sum rather than the merged message total. The rule compares both views, and
only days above a token floor may decide the verdict so tiny days cannot
outvote the day carrying the measured quantity.
"""

import json
from pathlib import Path

import quotamirror


def day(total_tokens, naive_tokens=None):
    naive = total_tokens if naive_tokens is None else naive_tokens
    return {
        "total_tokens": total_tokens,
        "naive_tokens": naive,
        "duplicate_tokens": naive - total_tokens,
        "records": 1,
        "by_bucket": {"input_tokens": total_tokens},
        "by_model": {},
    }


def scan(days):
    return {
        "days": days,
        "files": [],
        "records": 0,
        "messages": 0,
        "naive_tokens": sum(d["naive_tokens"] for d in days.values()),
        "tokens": {},
        "unattributed_tokens": 0,
    }


def test_cache_day_that_matches_the_record_sum_is_named_as_such():
    cache = {"2026-10-09": 5_415_374_398}
    transcripts = scan({"2026-10-09": day(2_500_000_000, naive_tokens=5_415_374_398)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["verdict"] == "record_sum_confirmed"
    assert result["days"][0]["closer_to"] == "record_sum"
    assert result["days"][0]["cache"] == 5_415_374_398
    assert result["days"][0]["merged"] == 2_500_000_000
    assert result["days"][0]["naive"] == 5_415_374_398


def test_cache_day_that_matches_the_merged_total_is_not_called_a_record_sum():
    cache = {"2026-10-10": 2_500_000_000}
    transcripts = scan({"2026-10-10": day(2_500_000_000, naive_tokens=5_415_374_398)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["verdict"] == "merged_confirmed"
    assert result["days"][0]["closer_to"] == "merged"


def test_days_below_the_token_floor_do_not_decide_the_verdict():
    cache = {"2026-10-01": 999_999, "2026-10-09": 5_415_374_398}
    transcripts = scan(
        {
            "2026-10-01": day(1, naive_tokens=999_999),  # a record sum, but tiny
            "2026-10-09": day(2_500_000_000, naive_tokens=5_415_374_398),
        }
    )

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["verdict"] == "record_sum_confirmed"
    assert result["days_compared"] == 1
    assert result["days_below_floor"] == 1


def test_no_transcript_scan_is_unavailable_not_zero():
    result = quotamirror.crosscheck_days({"2026-10-09": 10}, None)

    assert result["available"] is False
    assert result["verdict"] == "unavailable"
    assert result["reason"]


def test_a_day_present_in_one_source_only_is_counted_not_compared():
    cache = {"2026-10-09": 5_415_374_398}
    transcripts = scan({"2026-10-20": day(9_000_000)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["verdict"] == "no_overlap"
    assert result["days_compared"] == 0
    assert result["days_cache_only"] == 1
    assert result["days_transcript_only"] == 1


def test_worst_day_names_the_largest_duplicate_pile_not_the_largest_day():
    cache = {"2026-10-05": 1_000_000_000, "2026-10-09": 600_000_000}
    transcripts = scan(
        {
            "2026-10-05": day(990_000_000, naive_tokens=1_000_000_000),
            "2026-10-09": day(200_000_000, naive_tokens=600_000_000),
        }
    )

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["worst_day"]["date"] == "2026-10-09"
    assert result["worst_day"]["duplicate_tokens"] == 400_000_000


def test_compare_payload_carries_the_crosscheck_when_transcripts_are_given(tmp_path):
    cache = {
        "dailyModelTokens": [
            {"date": "2026-10-09", "tokensByModel": {"claude-opus-4": 5_000_000_000}}
        ],
        "modelUsage": {"claude-opus-4": {"inputTokens": 5_000_000_000}},
    }
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    record = {
        "type": "assistant",
        "timestamp": "2026-10-09T10:00:00Z",
        "message": {
            "id": "msg_1",
            "model": "claude-opus-4",
            "usage": {"input_tokens": 2_500_000_000, "output_tokens": 0},
        },
    }
    line = json.dumps(record)
    # The same message written twice, as a streaming partial write does.
    (transcripts / "session.jsonl").write_text(line + "\n" + line + "\n", encoding="utf-8")

    payload = quotamirror.compare_harnesses(
        tmp_path / "no-codex", cache, claude_transcripts=transcripts
    )

    assert payload["claude_day_crosscheck"]["available"] is True
    assert payload["claude_day_crosscheck"]["verdict"] == "record_sum_confirmed"


def test_render_compare_says_the_crosscheck_was_not_measured_without_transcripts(tmp_path):
    cache = {
        "dailyModelTokens": [
            {"date": "2026-10-09", "tokensByModel": {"claude-opus-4": 5_000_000_000}}
        ],
        "modelUsage": {"claude-opus-4": {"inputTokens": 5_000_000_000}},
    }
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    payload = quotamirror.compare_harnesses(tmp_path / "no-codex", cache)
    text = quotamirror.render_compare(payload)

    assert "DAY CROSS-CHECK" in text
    assert "not measured" in text
    assert payload["claude_day_crosscheck"]["verdict"] == "unavailable"


def test_render_states_the_cache_over_merged_ratio_and_says_none_when_zero():
    cache = {"2026-10-09": 5_000_000_000}
    transcripts = scan({"2026-10-09": day(2_500_000_000, naive_tokens=5_000_000_000)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)
    lines = []
    quotamirror._render_crosscheck(result, lines)
    text = "\n".join(lines)

    assert "2.000x" in text
    assert "none match the merged" in text
