"""Tests for the screen width of `quiet`'s own lines.

The chain has printed inside 80 columns since it was written, because it wraps
its rows under a label. Every other line of the same screen was assembled as
one f-string, and nothing measured it: on a real corpus 22 of the 45 lines a
`quiet` run prints are wider than 80 columns, the widest 254. The number that
is cut off the right of a terminal is a number the reader cannot check, so the
same screen that says "printed as an absence and not as zero" was printing its
own evidence off-screen.

The contract this file pins:

* no line `quiet` prints is wider than the screen (80 columns);
* folding never loses or reorders a word - it only moves a line break;
* a break is not placed between a figure and its neighbour, because a number
  separated from its label or its unit is the reading this tool refuses
  elsewhere;
* folding is a rendering step: the payload, and therefore `--json`, keeps the
  full sentence.

Fixtures are synthetic and small unless a test says it is measuring width.
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
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
    }


def scan_of(tmp_path, files):
    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    for name, lines in files.items():
        (root / name).write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
    return quotamirror.scan_usage(root)


def wide_payload(tmp_path):
    """A payload whose numbers are the size real receipts carry.

    Every figure here is synthetic, but the digit count is the one that pushed
    a real screen past the width: the defect is about how many characters a
    total occupies, not about what it counts.
    """
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 36688873258),
                usage_line("m2", "2026-05-05T10:00:00Z", 1234567890),
            ]
        },
    )
    cache = cache_with(
        [("2026-05-04", 36688873258), ("2026-05-05", 1234567890), ("2026-05-06", 5555555)],
        messages=18962,
    )
    return quotamirror.quiet_days(cache, scan)


def test_no_line_of_a_wide_screen_is_wider_than_the_screen(tmp_path):
    payload = wide_payload(tmp_path)
    text = quotamirror.render_quiet(payload)

    over = [(len(line), line) for line in text.splitlines() if len(line) > 80]
    assert over == []


def test_a_refusal_reason_longer_than_the_screen_is_folded_not_truncated(tmp_path):
    payload = wide_payload(tmp_path)
    payload["available"] = False
    payload["verdict"] = "no_day_table"
    payload["reason"] = (
        "the receipt's span and the transcript span do not meet, so no day is "
        "counted on both sides and this run makes no claim about either store"
    )

    text = quotamirror.render_quiet(payload)

    assert all(len(line) <= 80 for line in text.splitlines()), text
    flat = " ".join(text.split())
    assert "the receipt's span and the transcript span do not meet" in flat
    assert text.rstrip().splitlines()[-1] == "VERDICT: no_day_table"


def test_the_payload_keeps_the_sentence_the_screen_folds(tmp_path):
    payload = wide_payload(tmp_path)
    payload["available"] = False
    payload["verdict"] = "no_day_table"
    reason = "a reason that is long enough to need folding on any terminal here"
    payload["reason"] = reason

    quotamirror.render_quiet(payload)

    assert payload["reason"] == reason
    assert " ".join(reason.split()) in " ".join(json.dumps(payload).split())


def test_a_line_that_fits_is_returned_byte_for_byte():
    line = "  totals      receipt 400 | transcripts 300 -- not the same measure"
    assert len(line) <= 80

    assert quotamirror._fold_lines([line]) == [line]


def test_folding_moves_a_line_break_and_nothing_else():
    line = "  DAYS: " + " | ".join(f"term{i} {i * 11111}" for i in range(12))

    folded = quotamirror._fold_lines([line])

    assert len(folded) > 1
    assert all(len(part) <= 80 for part in folded)
    assert " ".join(folded).split() == line.split()


def test_a_break_is_not_placed_between_a_figure_and_its_neighbour():
    # A figure that lands at the edge takes the word beside it down, so
    # "silent 138" and "2.44 GiB" are never split across two lines.
    line = "  x  " + " ".join(["word"] * 9) + " silent 138 2.44 GiB"

    folded = quotamirror._fold_lines([line])

    assert all(len(part) <= 80 for part in folded)
    joined = "\n".join(folded)
    assert "silent 138" in joined
    assert "2.44 GiB" in joined
    for line_part in folded:
        assert not line_part.strip().startswith("138")
        assert not line_part.strip().startswith("GiB")


def test_a_folded_row_keeps_its_label_column():
    line = "  meter check  " + " ".join(["word"] * 16)

    folded = quotamirror._fold_lines([line])

    assert len(folded) > 1
    # The continuation starts at the column where the first value started.
    value_col = len("  meter check  ")
    assert folded[1].startswith(" " * value_col)
    assert folded[1][value_col] != " "


def test_a_billed_day_with_no_record_is_said_on_one_line(tmp_path):
    """Folding is the fallback, not the plan.

    A row that only fits by being broken after "token(s)" leaves its date on one
    line and the absence on the next, so the reader has to pair them up. The
    row's own wording should be short enough to stand on one line at the sizes
    a real receipt carries.
    """
    scan = scan_of(
        tmp_path,
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-04T10:00:00Z", 1000000),
                usage_line("m2", "2026-05-06T10:00:00Z", 1000000),
            ]
        },
    )
    cache = cache_with(
        [("2026-05-04", 1000000), ("2026-05-05", 991588155), ("2026-05-06", 1000000)],
        messages=18962,
    )
    screen = quotamirror.render_quiet(quotamirror.quiet_days(cache, scan))
    rows = [line for line in screen.splitlines() if line.strip().startswith("2026-05-05")]
    assert len(rows) == 1, rows
    row = rows[0]
    assert len(row) <= 80, (len(row), row)
    assert "991,588,155" in row
    assert "no transcript" in row
