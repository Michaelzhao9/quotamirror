"""Tests for the divisor named on the meter check's per-day and per-model rows.

The block prints two transcript figures - every usage record as written, and one
message counted once. The aggregate ratios divide the bill by each of them and
name which; the per-day and per-model rows divided by one of the two without
saying which. On a real receipt that reads as a contradiction on adjacent
lines: "16 of 27 day(s) exact, and on those the ratio is 1.00x" sits directly
above "0 of 27 day(s) agree with their own pair", about the same 27 days and
the same bill. Both sentences are true - they divide by different figures - but
nothing on screen says so.

All fixtures are synthetic.
"""

import json

import quotamirror


def row_of(text, label):
    """The row starting with `label`, with the lines it folded into joined back."""
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


def cache_with_models(rows):
    """rows: [(date, {model: tokens})]"""
    return {
        "version": 5,
        "dailyActivity": [
            {"date": date, "messageCount": 10, "sessionCount": 1, "toolCallCount": 0}
            for date, _ in rows
        ],
        "dailyModelTokensVersion": 5,
        "dailyModelTokens": [
            {"date": date, "tokensByModel": dict(models)} for date, models in rows
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


def test_per_day_row_names_the_figure_it_divides_by(tmp_path):
    # The receipt bills four times what the transcripts record, so neither
    # aggregate figure matches and the per-day row divides by the deduped
    # total. The row has to say so, beside the aggregate ratios that name theirs.
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1_000)])
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-test": 4_000})]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["verdict"] == "neither"
    assert block["per_day_basis"] == "deduped"
    text = quotamirror.render_quiet(result)
    assert "against the deduped figure" in row_of(text, "per day")


def test_per_day_basis_names_the_aggregate_that_matched(tmp_path):
    # The same message is written twice, so the record sum is twice the deduped
    # total and the receipt matches the record sum. The per-day row then
    # divides by the record sum, and says that instead.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
            usage_line("m1", "2026-05-04T10:00:01Z", 1_000_000),
        ],
    )
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-test": 2_000_000})]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["verdict"] == "record_sum_matches"
    assert block["per_day_basis"] == "record_sum"
    text = quotamirror.render_quiet(result)
    assert "against the record sum" in row_of(text, "per day")


def test_both_figures_matching_names_both(tmp_path):
    # One write, so the record sum and the deduped total are the same number and
    # both aggregates match. A day agrees only when it agrees with both, and the
    # row says so rather than naming one of two identical figures.
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000)])
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-test": 1_000_000})]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["verdict"] == "both_match"
    assert block["per_day_basis"] == "both_figures"
    text = quotamirror.render_quiet(result)
    assert "against both transcript figures" in row_of(text, "per day")


def test_by_model_row_names_the_figure_it_divides_by(tmp_path):
    # The per-model table is built from the deduped totals, so the row divides by
    # that figure while the block above prints both - it has to name it.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1_000, model="claude-heavy"),
            usage_line("m2", "2026-05-04T11:00:00Z", 500, model="claude-flat"),
        ],
    )
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-heavy": 10_000, "claude-flat": 500})]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["by_model_basis"] == "deduped"
    text = quotamirror.render_quiet(result)
    assert "against the deduped figure" in row_of(text, "by model")


def test_evidence_line_names_the_record_sum(tmp_path):
    # "16 day(s) exact" was the only description of what the 1.00x ratio was
    # exact against: the record sum, not the deduped total the next row uses.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1_000_000),
            usage_line("m2", "2026-05-05T10:00:00Z", 500_000),
        ],
    )
    result = quotamirror.quiet_days(
        cache_with_models(
            [("2026-05-04", {"claude-test": 1_000_000}), ("2026-05-05", {"claude-test": 2_000_000})]
        ),
        scan,
        min_tokens=0,
    )
    text = quotamirror.render_quiet(result)
    evidence = row_of(text, "evidence")
    assert "match the record sum exactly" in evidence


def test_the_named_divisor_fits_the_screen(tmp_path):
    # Naming the figure lengthens both rows, and the width contract covers every
    # line quiet prints.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1_000, model="claude-heavy"),
            usage_line("m2", "2026-05-04T11:00:00Z", 500, model="claude-flat"),
        ],
    )
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-heavy": 10_000, "claude-flat": 500})]),
        scan,
        min_tokens=0,
    )
    text = quotamirror.render_quiet(result)
    over = [line for line in text.splitlines() if len(line) > 80]
    assert not over, over
