"""Tests for the days `quiet` cannot see because the receipt stops first.

`quiet` builds its window as the intersection of the receipt's own span and the
transcripts' span. On a real machine the cache had stopped on 2026-10-25 while
the transcripts ran to 2026-11-26: thirty-two days of activity sat on one side
only, outside the window, and no line of the screen and no field of the payload
mentioned them. A reader who saw "138 silent days" and a stated window had no
way to learn that the receipt had stopped being written a month earlier - which
is the staleness hazard the README names, measured by the tool's own two stores.

The contract this file pins:

* days the transcripts record after the receipt's last stated day are counted,
  named and given a span and a token total;
* they are named on the screen, not only in `--json`, and they are named as
  outside the window rather than added to it;
* the window arithmetic is unchanged: a day off the end of the receipt is not
  judged, so it is neither silent nor measured;
* when the receipt is the later side, nothing is claimed about a tail.

Fixtures are synthetic.
"""

import json

import quotamirror


def row_of(text, label):
    """The row starting with `label`, with any folded continuation joined back."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(label):
            parts = [line.strip()]
            for following in lines[index + 1 :]:
                indent = len(following) - len(following.lstrip())
                if not following.strip() or indent < 8:
                    break
                parts.append(following.strip())
            return " ".join(parts)
    raise AssertionError(f"no row for {label!r} in:\n{text}")


def cache_with(days):
    """days: [(date, messages)] - the receipt's dailyActivity rows."""
    return {
        "version": 3,
        "dailyActivity": [
            {"date": date, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
            for date, messages in days
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


def receipts_written_on_05_04(tmp_path):
    return scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 100),
                usage_line("m2", "2026-05-07T10:00:00Z", 100),
                usage_line("m3", "2026-05-10T10:00:00Z", 100),
            ]
        },
    )


def test_days_after_the_receipt_are_counted_named_and_totalled(tmp_path):
    scan = receipts_written_on_05_04(tmp_path)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400)]), scan)

    # The window still ends on the receipt's last day: nothing off its end is
    # judged, because the receipt makes no claim about those days.
    assert result["window"] == ["2026-05-04", "2026-05-04"]
    assert result["days_after_receipt"] == 2
    assert result["transcript_tail"] == ["2026-05-07", "2026-05-10"]
    assert result["transcript_tail_tokens"] == 200


def test_the_screen_names_the_days_the_window_leaves_off_its_end(tmp_path):
    scan = receipts_written_on_05_04(tmp_path)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400)]), scan)

    text = quotamirror.render_quiet(result)
    tail = row_of(text, "tail")

    assert "2 day(s)" in tail
    assert "2026-05-07 .. 2026-05-10" in tail
    assert "200" in tail
    # Named as unseen, not folded into the verdict's own arithmetic.
    assert "not judged" in tail


def test_the_tail_is_not_added_to_the_window(tmp_path):
    scan = receipts_written_on_05_04(tmp_path)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400)]), scan)

    assert result["days_in_window"] == 1
    assert result["days_measured"] == 1
    assert result["days_silent"] == 0
    assert result["days_tokens_only"] == 0
    assert [row["date"] for row in result["rows"]] == []


def test_a_receipt_that_is_the_later_side_claims_no_tail(tmp_path):
    # The transcripts stop before the receipt does; there is no tail to name.
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 100)]})
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400), ("2026-05-09", 400)]), scan)

    assert result["days_after_receipt"] == 0
    assert result["transcript_tail"] is None
    assert result["transcript_tail_tokens"] == 0

    text = quotamirror.render_quiet(result)
    assert not [line for line in text.splitlines() if line.strip().startswith("tail")]


def test_disjoint_spans_still_name_the_tail_they_cannot_judge(tmp_path):
    # No overlap is its own verdict; the tail is the reason there is none, so
    # the same run has to state it.
    scan = scan_of(
        tmp_path,
        {"a.jsonl": [usage_line("m1", "2026-06-01T10:00:00Z", 100)]},
    )
    result = quotamirror.quiet_days(cache_with([("2026-05-01", 400)]), scan)

    assert result["verdict"] == "no_overlap"
    assert result["days_after_receipt"] == 1
    assert result["transcript_tail"] == ["2026-06-01", "2026-06-01"]

    text = quotamirror.render_quiet(result)
    assert "1 day(s)" in row_of(text, "tail")


def test_the_outside_count_says_it_already_holds_the_tail(tmp_path):
    # Two overlapping counts on one screen invite an addition, so the union
    # line says which part of itself is already named two lines above.
    scan = receipts_written_on_05_04(tmp_path)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400)]), scan)

    assert result["days_outside_window"] == 2

    outside = row_of(quotamirror.render_quiet(result), "outside")
    assert "2 day(s)" in outside
    assert "including the 2 tail day(s) named above" in outside


def test_the_outside_count_stays_a_plain_union_without_a_tail(tmp_path):
    scan = scan_of(tmp_path, {"a.jsonl": [usage_line("m1", "2026-05-04T10:00:00Z", 100)]})
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 400), ("2026-05-09", 400)]), scan)

    outside = row_of(quotamirror.render_quiet(result), "outside")
    assert "including" not in outside
