"""Tests for quotamirror's receipt over a usage-meter cache.

All fixtures are synthetic. No real user data is stored in this repository.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import quotamirror


def build_cache(models=None, daily=None, activity=None, last=None):
    """Small synthetic cache shaped like Claude Code's stats-cache.json."""
    return {
        "version": 5,
        "lastComputedDate": last,
        "dailyActivity": activity if activity is not None else [],
        "dailyModelTokens": daily if daily is not None else [],
        "modelUsage": models if models is not None else {},
        "totalSessions": 1,
        "totalMessages": 2,
    }


def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0, cost_usd=0):
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadInputTokens": cache_read,
        "cacheCreationInputTokens": cache_creation,
        "webSearchRequests": 0,
        "costUSD": cost_usd,
    }


def day(date, **tokens_by_model):
    return {"date": date, "tokensByModel": dict(tokens_by_model)}


# --- Cycle A: token totals and the dollars question -------------------------


def test_totals_tokens_per_model_across_all_four_buckets():
    cache = build_cache(
        models={
            "claude-opus": usage(input_tokens=10, output_tokens=90, cache_read=800, cache_creation=100),
            "claude-haiku": usage(input_tokens=1, output_tokens=9, cache_read=40, cache_creation=0),
        }
    )

    payload = quotamirror.summarize(cache)

    assert payload["tokens"]["by_model"]["claude-opus"]["tokens"] == 1000
    assert payload["tokens"]["by_model"]["claude-haiku"]["tokens"] == 50
    assert payload["tokens"]["total"] == 1050
    assert payload["tokens"]["by_model"]["claude-opus"]["share"] == pytest.approx(1000 / 1050)


def test_zero_cost_field_is_unknown_not_a_price():
    cache = build_cache(
        models={"claude-opus": usage(input_tokens=1000, output_tokens=1000, cost_usd=0)},
    )

    payload = quotamirror.summarize(cache)

    assert payload["dollars"]["status"] == "unknown"
    assert payload["dollars"]["usd"] is None
    assert "0.00" in payload["dollars"]["reason"] or "zero" in payload["dollars"]["reason"].lower()


def test_nonzero_cost_is_reported_as_vendor_figure_not_derived():
    cache = build_cache(
        models={
            "claude-opus": usage(input_tokens=5, output_tokens=5, cost_usd=1.25),
            "claude-haiku": usage(input_tokens=5, output_tokens=5, cost_usd=0.25),
            "claude-legacy": usage(input_tokens=7, output_tokens=7, cost_usd=0),
        }
    )

    payload = quotamirror.summarize(cache)

    assert payload["dollars"]["status"] == "vendor_reported"
    assert payload["dollars"]["usd"] == pytest.approx(1.5)
    assert payload["dollars"]["models_with_zero_unpriced"] == 1


# --- Cycle B: do the two lifetime views in one file agree? ------------------


def test_gap_between_daily_view_and_model_view_is_reported_per_model():
    cache = build_cache(
        models={
            "claude-opus": usage(input_tokens=0, output_tokens=500),
            "claude-sonnet": usage(input_tokens=0, output_tokens=100),
        },
        daily=[day("2026-05-01", **{"claude-opus": 400, "claude-sonnet": 100})],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["daily_total"] == 500
    assert attribution["model_total"] == 600
    assert attribution["gap"] == 100
    assert attribution["gaps"] == [
        {"model": "claude-opus", "daily": 400, "model_total": 500, "gap": 100}
    ]


def test_models_whose_two_views_agree_are_counted_as_reconciled():
    cache = build_cache(
        models={
            "claude-opus": usage(output_tokens=500),
            "claude-sonnet": usage(output_tokens=100),
            "claude-haiku": usage(output_tokens=10),
        },
        daily=[
            day("2026-05-01", **{"claude-opus": 500, "claude-sonnet": 100}),
        ],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["reconciled_models"] == 2
    assert attribution["models_compared"] == 3
    assert attribution["gap_share"] == pytest.approx(10 / 610)


def test_a_model_present_only_in_the_model_view_is_an_unattributed_gap():
    cache = build_cache(
        models={"claude-sonnet": usage(input_tokens=0, output_tokens=294_166)},
        daily=[day("2026-05-01", **{"claude-opus": 1000})],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["gaps"][0]["model"] == "claude-sonnet"
    assert attribution["gaps"][0]["daily"] == 0
    assert attribution["gap"] == 294_166 - 1000


# --- Cycle C: coverage of the calendar and staleness of the snapshot --------


def test_days_with_messages_but_no_token_record_are_listed():
    cache = build_cache(
        models={"claude-opus": usage(output_tokens=10)},
        daily=[day("2026-05-03", **{"claude-opus": 10})],
        activity=[
            {"date": "2026-04-01", "messageCount": 3, "sessionCount": 1, "toolCallCount": 2},
            {"date": "2026-05-03", "messageCount": 4, "sessionCount": 1, "toolCallCount": 1},
        ],
    )

    coverage = quotamirror.summarize(cache)["coverage"]

    assert coverage["activity_days"] == 2
    assert coverage["token_days"] == 1
    assert coverage["activity_without_tokens"] == ["2026-04-01"]


def test_snapshot_staleness_is_measured_against_today():
    cache = build_cache(
        models={"claude-opus": usage(output_tokens=10)},
        last="2026-10-25",
    )

    payload = quotamirror.summarize(cache, today="2026-10-28")

    assert payload["as_of"] == "2026-10-25"
    assert payload["stale_days"] == 3


def test_staleness_is_unknown_without_a_today_argument():
    cache = build_cache(models={"claude-opus": usage(output_tokens=10)}, last="2026-10-25")

    payload = quotamirror.summarize(cache)

    assert payload["as_of"] == "2026-10-25"
    assert payload["stale_days"] is None


def test_day_series_is_ordered_and_names_the_busiest_day():
    cache = build_cache(
        models={"claude-opus": usage(output_tokens=600)},
        daily=[
            day("2026-05-02", **{"claude-opus": 500}),
            day("2026-05-01", **{"claude-opus": 100}),
        ],
    )

    payload = quotamirror.summarize(cache)

    assert [d["date"] for d in payload["days"]] == ["2026-05-01", "2026-05-02"]
    assert payload["days"][1]["tokens"] == 500
    assert payload["busiest_day"] == {"date": "2026-05-02", "tokens": 500}


# --- Cycle D: the receipt a human reads, and the CLI contract --------------


def load_mixed_cache():
    return build_cache(
        models={
            "claude-opus": usage(input_tokens=100, output_tokens=900, cache_read=9000, cost_usd=0),
            "claude-sonnet": usage(input_tokens=0, output_tokens=294_166, cost_usd=0),
        },
        daily=[day("2026-10-05", **{"claude-opus": 5_000})],
        activity=[
            {"date": "2026-04-01", "messageCount": 3, "sessionCount": 1, "toolCallCount": 2},
            {"date": "2026-10-05", "messageCount": 4, "sessionCount": 1, "toolCallCount": 1},
        ],
        last="2026-10-25",
    )


def test_text_receipt_never_prints_a_zero_price_for_an_unknown_cost():
    payload = quotamirror.summarize(load_mixed_cache(), today="2026-10-28")

    text = quotamirror.render_text(payload, source="fixture.json")

    assert "$0" not in text
    assert "dollars" in text.lower()
    assert "unknown" in text.lower()


def test_text_receipt_states_gap_staleness_and_missing_days():
    payload = quotamirror.summarize(load_mixed_cache(), today="2026-10-28")

    text = quotamirror.render_text(payload, source="fixture.json")

    assert "2026-10-25" in text and "3 days" in text
    assert "294,166" in text
    assert "2026-04-01" in text
    assert "10,000" in text  # total tokens across all four buckets


def write_fixture(tmp_path, cache, name="stats-cache.json"):
    path = tmp_path / name
    path.write_text(json.dumps(cache), encoding="utf-8")
    return path


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(Path(quotamirror.__file__)), *args],
        capture_output=True,
        text=True,
    )


def test_cli_emits_parseable_json(tmp_path):
    path = write_fixture(tmp_path, load_mixed_cache())

    result = run_cli("scan", "--path", str(path), "--json", "--today", "2026-10-28")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dollars"]["usd"] is None
    assert payload["attribution"]["gap"] == 299_166
    assert payload["stale_days"] == 3


def test_cli_leaves_the_cache_untouched(tmp_path):
    path = write_fixture(tmp_path, load_mixed_cache())
    before = path.read_bytes()

    result = run_cli("scan", "--path", str(path), "--today", "2026-10-28")

    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == before


def test_cli_exits_2_when_the_cache_is_missing(tmp_path):
    result = run_cli("scan", "--path", str(tmp_path / "absent.json"))

    assert result.returncode == 2
    assert "absent.json" in result.stderr


# --- Cycle E: degenerate caches must not be invented into numbers ----------


def test_empty_cache_reports_zero_tokens_without_a_price():
    payload = quotamirror.summarize(build_cache(), today="2026-10-28")

    assert payload["tokens"]["total"] == 0
    assert payload["dollars"]["status"] == "unknown"
    assert payload["dollars"]["usd"] is None
    assert payload["attribution"]["gap"] == 0
    assert payload["busiest_day"] is None
    assert payload["as_of"] is None
    assert payload["stale_days"] is None


def test_negative_gap_is_kept_signed_so_the_direction_survives():
    cache = build_cache(
        models={"claude-opus": usage(output_tokens=100)},
        daily=[day("2026-05-01", **{"claude-opus": 900})],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["gap"] == -800
    assert attribution["gaps"][0]["gap"] == -800
    assert attribution["gap_share"] == pytest.approx(800 / 100)


def test_cli_rejects_a_json_file_that_is_not_a_usage_cache(tmp_path):
    path = write_fixture(tmp_path, {"hello": "world"}, name="other.json")

    result = run_cli("scan", "--path", str(path))

    assert result.returncode == 2
    assert "usage cache" in result.stderr


def test_cli_rejects_a_malformed_today_argument(tmp_path):
    path = write_fixture(tmp_path, load_mixed_cache())

    result = run_cli("scan", "--path", str(path), "--today", "28th of October")

    assert result.returncode == 2
    assert "today" in result.stderr.lower()


def test_cli_rejects_a_cache_with_non_object_model_usage(tmp_path):
    path = write_fixture(tmp_path, {"modelUsage": {"claude-weird": None}})

    result = run_cli("scan", "--path", str(path))

    assert result.returncode == 2
    assert "claude-weird" in result.stderr


def test_scan_reports_redact_the_current_home_in_json_and_text(monkeypatch, capsys):
    private_path = str(Path.home() / ".claude" / "stats-cache.json")
    monkeypatch.setattr(quotamirror, "load_cache", lambda _path: build_cache())

    assert quotamirror.main(["scan", "--path", private_path, "--json"]) == 0
    json_output = capsys.readouterr().out
    assert private_path not in json_output
    assert '"source": "~/.claude/stats-cache.json"' in json_output

    assert quotamirror.main(["scan", "--path", private_path]) == 0
    text_output = capsys.readouterr().out
    assert private_path not in text_output
    assert "source: ~/.claude/stats-cache.json" in text_output
