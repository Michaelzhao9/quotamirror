"""Tests for the meter check's per-model axis.

The aggregate and per-day axes both compare sums taken across every model at
once. A gap in one model and an opposite gap in another therefore cancel, and
the reader learns only that "the totals differ". This axis splits the same
deciding days by model name and reports, per model, both sides' totals - so a
residual that lives in one model is separated from one that spreads across all
of them, and a model one side never names is not silently dropped.

All fixtures are synthetic.
"""

import json

import quotamirror


def cache_with_models(rows, messages=10):
    """rows: [(date, {model: tokens})]"""
    return {
        "version": 5,
        "dailyActivity": [
            {"date": date, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
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


def test_by_model_names_each_model_that_agrees(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 400, model="claude-alpha"),
            usage_line("m2", "2026-05-04T11:00:00Z", 600, model="claude-beta"),
        ],
    )
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-alpha": 400, "claude-beta": 600})]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["by_model_verdict"] == "every_model_agrees"
    assert block["models_compared"] == 2
    assert block["models_agreeing"] == 2
    rows = {row["model"]: row for row in block["by_model_rows"]}
    assert rows["claude-alpha"]["verdict"] == "agree"
    assert rows["claude-alpha"]["receipt_tokens"] == 400
    assert rows["claude-alpha"]["transcript_tokens"] == 400


def test_by_model_says_which_model_holds_the_gap(tmp_path):
    # claude-heavy is billed ten times its transcript record; claude-flat
    # agrees. The aggregate is receipt-heavy either way, but only this axis can
    # say the excess sits in one model.
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
    assert block["by_model_verdict"] == "mixed"
    assert block["models_receipt_heavy"] == 1
    assert block["models_transcript_heavy"] == 0
    assert block["models_agreeing"] == 1
    assert block["largest_gap_model"] == "claude-heavy"
    assert block["largest_gap_share"] == 1.0


def test_by_model_names_a_model_only_one_side_counts(tmp_path):
    # A model the receipt bills and the transcripts never mention must be
    # listed, not dropped: dropping it would leave the per-model rows summing
    # to less than either total with nothing on screen to explain the hole.
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 700)])
    result = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-ghost": 5_000})]),
        scan,
        min_tokens=0,
    )
    block = result["arbitration"]
    assert block["by_model_verdict"] == "no_shared_model"
    assert block["models_compared"] == 0
    assert block["models_receipt_only"] == ["claude-ghost"]
    assert block["models_transcript_only"] == ["claude-test"]


def test_quiet_output_prints_the_model_axis(tmp_path):
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1_000, model="claude-heavy"),
            usage_line("m2", "2026-05-04T11:00:00Z", 500, model="claude-flat"),
        ],
    )
    payload = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-heavy": 10_000, "claude-flat": 500})]),
        scan,
        min_tokens=0,
    )
    text = quotamirror.render_quiet(payload)
    assert "\n    by model" in text
    assert "1 of 2 model(s) agree" in text
    assert "largest gap claude-heavy" in text
    assert "-- mixed" in text


def test_by_model_reports_the_ratio_spread(tmp_path):
    # Two models, one at 10x and one at 1x: the counts alone cannot say whether
    # the residual is one constant factor or a range, which is what decides
    # whether a single scaling bug is a plausible cause.
    scan = scan_of(
        tmp_path,
        [
            usage_line("m1", "2026-05-04T10:00:00Z", 1_000, model="claude-heavy"),
            usage_line("m2", "2026-05-04T11:00:00Z", 500, model="claude-flat"),
        ],
    )
    payload = quotamirror.quiet_days(
        cache_with_models([("2026-05-04", {"claude-heavy": 10_000, "claude-flat": 500})]),
        scan,
        min_tokens=0,
    )
    block = payload["arbitration"]
    assert block["model_ratio_min"] == 1.0
    assert block["model_ratio_max"] == 10.0
    text = quotamirror.render_quiet(payload)
    assert "ratio 1.00x..10.00x" in text
