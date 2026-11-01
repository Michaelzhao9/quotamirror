"""Tests for the CUMULATIVE verdict (`units --cumulative`).

The failure this targets is arithmetic, not cosmetic: a monotone counter that
reports a running total is re-read every day and then summed. The sum counts
every earlier day again, so the same usage appears twice, three times, N times.
Two dated instances exist in the wild (a session-cumulative ACP usage field
charged again on resume; a day table built from a session's cumulative
cache_read, ~1.78x the deduped library).

A receipt cannot know a field's intent, so the tool does not claim to. It
applies three exact tests and says which one fired:

  * a date series whose last value equals a declared lifetime total while its
    sum does not  -> the series is a running total read as a daily increment
  * a date series that never decreases and whose sum is much larger than its
    own last value -> a sum over it is suspect, but only arithmetic is proven
  * a fine-grained sum above the same quantity declared at a coarser level
    -> double counting, and the ratio is printed with its denominator named

All fixtures synthetic.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import quotamirror


def usage(tokens=100):
    return {
        "inputTokens": tokens,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "webSearchRequests": 0,
        "costUSD": 0,
    }


def cache_with(activity=None, daily=None, models=None, total_messages=None, total_sessions=None):
    cache = {
        "version": 5,
        "lastComputedDate": "2026-10-25",
        "dailyActivity": activity or [],
        "dailyModelTokens": daily or [],
        "modelUsage": models or {},
    }
    if total_messages is not None:
        cache["totalMessages"] = total_messages
    if total_sessions is not None:
        cache["totalSessions"] = total_sessions
    return cache


def activity(*rows):
    return [
        {"date": f"2026-10-{i + 1:02d}", **row} for i, row in enumerate(rows)
    ]


def series_named(payload, name):
    for row in payload["series"]:
        assert "verdict" in row
        if row["name"] == name:
            return row
    raise AssertionError(f"no series named {name}: {[r['name'] for r in payload['series']]}")


# --- per-series shape tests -------------------------------------------------


def test_never_decreasing_series_is_not_called_incremental():
    """RED first: a running total re-read daily must not be scored as increments."""
    cache = cache_with(activity=activity({"messageCount": 10}, {"messageCount": 20},
                                         {"messageCount": 35}, {"messageCount": 55}))
    row = series_named(quotamirror.audit_cumulative(cache), "dailyActivity.messageCount")
    assert row["monotone"] is True
    assert row["verdict"] != "incremental"


def test_monotone_series_ending_at_declared_total_is_a_snapshot():
    cache = cache_with(activity=activity({"messageCount": 10}, {"messageCount": 20},
                                         {"messageCount": 35}, {"messageCount": 55}),
                       total_messages=55)
    row = series_named(quotamirror.audit_cumulative(cache), "dailyActivity.messageCount")
    assert row["verdict"] == "cumulative_snapshot"
    assert row["last"] == 55
    assert row["sum"] == 120
    assert row["last_matches"] == "totalMessages"


def test_monotone_series_with_no_declared_total_is_only_a_suspect():
    cache = cache_with(activity=activity({"toolCallCount": 10}, {"toolCallCount": 20},
                                         {"toolCallCount": 35}, {"toolCallCount": 55}))
    row = series_named(quotamirror.audit_cumulative(cache), "dailyActivity.toolCallCount")
    assert row["verdict"] == "cumulative_suspect"
    assert row["last_matches"] is None
    assert round(row["sum_over_last"], 2) == round(120 / 55, 2)


def test_bouncing_series_is_incremental():
    cache = cache_with(activity=activity({"messageCount": 5}, {"messageCount": 9},
                                         {"messageCount": 4}, {"messageCount": 12}))
    row = series_named(quotamirror.audit_cumulative(cache), "dailyActivity.messageCount")
    assert row["monotone"] is False
    assert row["verdict"] == "incremental"
    assert row["sum_over_last"] is None


def test_too_few_points_is_not_judged():
    cache = cache_with(activity=activity({"messageCount": 10}, {"messageCount": 20}))
    row = series_named(quotamirror.audit_cumulative(cache), "dailyActivity.messageCount")
    assert row["verdict"] == "insufficient"
    assert row["points"] == 2


def test_flat_series_at_zero_is_not_a_snapshot():
    """A series of zeros is monotone and its sum equals its last value: no risk."""
    cache = cache_with(activity=activity({"sessionCount": 0}, {"sessionCount": 0},
                                         {"sessionCount": 0}, {"sessionCount": 0}),
                       total_sessions=0)
    row = series_named(quotamirror.audit_cumulative(cache), "dailyActivity.sessionCount")
    assert row["verdict"] == "incremental"


def test_per_day_token_map_is_flattened_into_one_series():
    cache = cache_with(daily=[
        {"date": "2026-10-01", "tokensByModel": {"a": 10, "b": 5}},
        {"date": "2026-10-02", "tokensByModel": {"a": 20}},
        {"date": "2026-10-03", "tokensByModel": {"b": 30}},
    ])
    row = series_named(quotamirror.audit_cumulative(cache), "dailyModelTokens.tokensByModel")
    assert row["sum"] == 65
    assert row["points"] == 3


# --- cross-granularity tests ------------------------------------------------


def crosscheck_named(payload, fine):
    for row in payload["crosschecks"]:
        if row["fine"] == fine:
            return row
    raise AssertionError([r["fine"] for r in payload["crosschecks"]])


def test_fine_sum_above_coarse_total_is_double_counted():
    cache = cache_with(activity=activity({"messageCount": 60}, {"messageCount": 60},
                                         {"messageCount": 60}, {"messageCount": 20}),
                       total_messages=100)
    row = crosscheck_named(quotamirror.audit_cumulative(cache), "dailyActivity.messageCount")
    assert row["verdict"] == "double_counted"
    assert row["delta"] == 100
    assert row["coarse"] == "totalMessages"


def test_fine_sum_below_coarse_total_is_missing_records_not_double_counting():
    cache = cache_with(activity=activity({"messageCount": 20}, {"messageCount": 20},
                                         {"messageCount": 20}, {"messageCount": 20}),
                       total_messages=100)
    row = crosscheck_named(quotamirror.audit_cumulative(cache), "dailyActivity.messageCount")
    assert row["verdict"] == "missing_granularity"
    assert row["delta"] == -20
    assert row["factor"] < 1


def test_exact_match_is_reconciled():
    cache = cache_with(activity=activity({"sessionCount": 1}, {"sessionCount": 2}),
                       total_sessions=3)
    row = crosscheck_named(quotamirror.audit_cumulative(cache), "dailyActivity.sessionCount")
    assert row["verdict"] == "reconciled"
    assert row["delta"] == 0


def test_series_without_a_coarse_declaration_is_unverifiable():
    cache = cache_with(activity=activity({"toolCallCount": 5}, {"toolCallCount": 7}))
    row = crosscheck_named(quotamirror.audit_cumulative(cache), "dailyActivity.toolCallCount")
    assert row["verdict"] == "no_coarse_total"
    assert row["coarse_total"] is None


def test_token_crosscheck_uses_the_model_table_as_its_coarse_total():
    cache = cache_with(
        daily=[{"date": "2026-10-01", "tokensByModel": {"a": 40}},
               {"date": "2026-10-02", "tokensByModel": {"a": 60}}],
        models={"a": usage(120)},
    )
    row = crosscheck_named(quotamirror.audit_cumulative(cache), "dailyModelTokens.tokensByModel")
    assert row["coarse"] == "modelUsage"
    assert row["coarse_total"] == 120
    assert row["verdict"] == "missing_granularity"


# --- rendering --------------------------------------------------------------


def test_render_names_the_denominator_before_any_ratio():
    cache = cache_with(activity=activity({"messageCount": 60}, {"messageCount": 60},
                                         {"messageCount": 60}, {"messageCount": 20}),
                       total_messages=100)
    text = quotamirror.render_cumulative(quotamirror.audit_cumulative(cache))
    lines = text.splitlines()
    denom = next(i for i, line in enumerate(lines) if "denominator" in line.lower())
    ratio = next(i for i, line in enumerate(lines) if re.search(r"\d+\.\d{2}x", line))
    assert denom < ratio
    assert "cacheReadInputTokens" in text


def test_render_says_so_when_no_series_has_the_cumulative_shape():
    cache = cache_with(activity=activity({"messageCount": 5}, {"messageCount": 4},
                                         {"messageCount": 9}, {"messageCount": 2}))
    text = quotamirror.render_cumulative(quotamirror.audit_cumulative(cache))
    assert "no series in this receipt has the cumulative shape" in text


def test_render_does_not_call_a_short_fine_sum_double_counting():
    """Test intent: the *verdict* must not be double_counted for a short sum.

    (First version asserted the substring "double" was absent and failed on the
    sentence "this is NOT double counting" - an expectation bug, not a code bug.)
    """
    cache = cache_with(activity=activity({"messageCount": 20}, {"messageCount": 20}),
                       total_messages=100)
    text = quotamirror.render_cumulative(quotamirror.audit_cumulative(cache))
    assert "short by" in text
    assert "double_counted" not in text


# --- CLI --------------------------------------------------------------------


def run_cli(tmp_path, cache, *args):
    path = tmp_path / "stats-cache.json"
    path.write_text(json.dumps(cache))
    return subprocess.run(
        [sys.executable, str(Path(quotamirror.__file__)), "units", "--path", str(path), *args],
        capture_output=True, text=True,
    )


def test_render_does_not_round_a_near_one_ratio_down_to_1_00x():
    """Real output printed "1.00x" for a 0.35% shortfall, which reads as no gap at all."""
    cache = cache_with(activity=activity({"messageCount": 3321}, {"messageCount": 3322},
                                         {"messageCount": 3322}),
                       total_messages=10000)
    text = quotamirror.render_cumulative(quotamirror.audit_cumulative(cache))
    assert "0.9965x" in text
    assert "1.00x" not in text


def test_flat_series_is_not_called_cumulative():
    """4 equal points are monotone and sum to 4x the last value - but nothing grew.

    Found by running the tool on a synthetic fixture: a flat daily series was
    reported as `cumulative_suspect` at 38.00x, which is an artefact of the point
    count, not evidence of a running counter.
    """
    cache = cache_with(daily=[
        {"date": f"2026-10-0{i}", "tokensByModel": {"a": 1000}} for i in range(1, 5)
    ])
    row = series_named(quotamirror.audit_cumulative(cache), "dailyModelTokens.tokensByModel")
    assert row["monotone"] is True
    assert row["verdict"] == "incremental"


def test_growing_series_stays_suspect():
    cache = cache_with(daily=[
        {"date": "2026-10-01", "tokensByModel": {"a": 100}},
        {"date": "2026-10-02", "tokensByModel": {"a": 400}},
        {"date": "2026-10-03", "tokensByModel": {"a": 900}},
    ])
    row = series_named(quotamirror.audit_cumulative(cache), "dailyModelTokens.tokensByModel")
    assert row["verdict"] == "cumulative_suspect"


def test_barely_growing_series_is_not_cumulative():
    """A flat series with a rounding bump grew by 2 tokens over 4 points: not a counter."""
    cache = cache_with(daily=[
        {"date": "2026-10-01", "tokensByModel": {"a": 1000}},
        {"date": "2026-10-02", "tokensByModel": {"a": 1000}},
        {"date": "2026-10-03", "tokensByModel": {"a": 1000}},
        {"date": "2026-10-04", "tokensByModel": {"a": 1002}},
    ])
    row = series_named(quotamirror.audit_cumulative(cache), "dailyModelTokens.tokensByModel")
    assert row["verdict"] == "incremental"


def test_series_starting_at_zero_that_reaches_a_total_is_suspect():
    cache = cache_with(daily=[
        {"date": "2026-10-01", "tokensByModel": {"a": 0}},
        {"date": "2026-10-02", "tokensByModel": {"a": 30}},
        {"date": "2026-10-03", "tokensByModel": {"a": 90}},
    ])
    row = series_named(quotamirror.audit_cumulative(cache), "dailyModelTokens.tokensByModel")
    assert row["verdict"] == "cumulative_suspect"


def test_units_gains_cumulative_only_when_asked(tmp_path):
    cache = cache_with(activity=activity({"messageCount": 10}, {"messageCount": 20},
                                         {"messageCount": 35}, {"messageCount": 55}),
                       total_messages=55)
    plain = run_cli(tmp_path, cache, "--json")
    assert plain.returncode == 0, plain.stderr
    assert "cumulative" not in json.loads(plain.stdout)

    asked = run_cli(tmp_path, cache, "--json", "--cumulative")
    assert asked.returncode == 0, asked.stderr
    payload = json.loads(asked.stdout)
    assert payload["cumulative"]["series"][0]["verdict"] == "cumulative_snapshot"


def test_units_text_with_cumulative_prints_the_section(tmp_path):
    cache = cache_with(activity=activity({"messageCount": 10}, {"messageCount": 20},
                                         {"messageCount": 35}, {"messageCount": 55}),
                       total_messages=55)
    proc = run_cli(tmp_path, cache, "--cumulative")
    assert proc.returncode == 0, proc.stderr
    assert "CUMULATIVE" in proc.stdout
    assert "cumulative_snapshot" in proc.stdout
