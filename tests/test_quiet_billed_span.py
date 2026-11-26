"""Tests for what the `quiet` verdict says about days the receipt bills and the
transcripts do not hold.

The verdict names a cause: why a day can carry a token number in the receipt's
own day table while the transcript corpus holds no record for it. The window
this command judges is the intersection of the two spans, so every day it
reports on lies between the first and the last day the transcripts reach. A
sentence that blames the edge of the corpus for such a day is therefore
contradicted by the payload printed beside it.

The honest statement is the one the data supports: the corpus reaches those
dates and holds nothing for them.

All fixtures are synthetic.
"""

import json

import quotamirror


def usage_line(message_id, ts, input_tokens=10, model="claude-test"):
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


def cache_with(days, tokens):
    """A receipt with a message table and a per-day token meter."""
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


def scan_of(tmp_path, days):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    lines = [usage_line(f"m{index}", f"{day}T10:00:00Z") for index, day in enumerate(days)]
    (root / "a.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return quotamirror.scan_usage(root)


def test_the_reason_does_not_blame_the_corpus_edge_for_a_day_inside_the_span(tmp_path):
    # The transcripts hold records on 05-02 and 05-06, so their span covers
    # 05-03. The receipt bills 05-03 and the transcripts hold nothing for it.
    # That day is inside the corpus, not past its end.
    scan = scan_of(tmp_path, ["2026-05-02", "2026-05-06"])
    cache = cache_with(
        [("2026-05-02", 100), ("2026-05-03", 500), ("2026-05-06", 100)],
        [("2026-05-03", 5000)],
    )

    result = quotamirror.quiet_days(cache, scan)

    assert result["verdict"] == "billed_without_transcripts"
    assert result["transcript_span"] == ["2026-05-02", "2026-05-06"]
    reason = result["reason"]
    assert "does not reach" not in reason
    assert "inside the transcript span 2026-05-02..2026-05-06" in reason
    # The sentence's evidence travels in the payload, not only in its prose: a
    # reader who does not trust the wording can count the days themselves.
    assert result["receipt_billed_days_inside_span"] == 1


def test_a_gap_that_reaches_past_the_span_is_not_called_a_hole():
    # Guard for the other wording: this branch is not reachable through
    # `quiet_days` while the window is the intersection of the two spans, but
    # the sentence must not call a gap a hole when the counts say otherwise.
    reason = quotamirror._billed_reason(3, 9000, 2, ("2026-05-02", "2026-05-06"))

    assert "2 of 3 lie inside the transcript span 2026-05-02..2026-05-06" in reason
    assert "1 fall outside it" in reason
    assert "not the edge of it" not in reason
