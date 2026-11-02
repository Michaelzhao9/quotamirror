"""Tests for quotamirror's magnitude/unit audit (`units`).

All fixtures are synthetic. The point of the audit is that a field can be
*present* and still be unusable: 0 where a capacity belongs, or a count whose
unit has to be inferred because the key name does not carry one.
"""

import json
import subprocess
import sys
from pathlib import Path

import quotamirror


def usage(tokens=0, cost_usd=0, **extra):
    entry = {
        "inputTokens": tokens,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "webSearchRequests": 0,
        "costUSD": cost_usd,
    }
    entry.update(extra)
    return entry


def build_cache(models=None, activity=None, daily=None, total_messages=0, total_sessions=0, **extra):
    cache = {
        "version": 5,
        "lastComputedDate": "2026-10-25",
        "dailyActivity": activity if activity is not None else [],
        "dailyModelTokens": daily if daily is not None else [],
        "modelUsage": models if models is not None else {},
        "totalSessions": total_sessions,
        "totalMessages": total_messages,
    }
    cache.update(extra)
    return cache


# --- slice 1: a capacity field that is zero for every model -----------------


def test_flags_capacity_field_that_is_zero_for_every_model():
    cache = build_cache(
        models={
            "m-a": usage(tokens=1000, contextWindow=0, maxOutputTokens=0),
            "m-b": usage(tokens=2000, contextWindow=0, maxOutputTokens=0),
        },
        total_messages=1,
    )
    audit = quotamirror.audit_units(cache)

    by_field = {row["field"]: row for row in audit["zeros"]}
    assert set(by_field) == {"contextWindow", "maxOutputTokens"}
    assert by_field["contextWindow"]["verdict"] == "all_zero"
    assert by_field["contextWindow"]["models"] == 2
    assert by_field["contextWindow"]["zeros"] == 2


def test_does_not_flag_a_capacity_field_that_holds_a_value():
    cache = build_cache(
        models={
            "m-a": usage(tokens=1000, contextWindow=200000, maxOutputTokens=32000),
            "m-b": usage(tokens=2000, contextWindow=200000, maxOutputTokens=32000),
        },
    )
    audit = quotamirror.audit_units(cache)

    assert audit["zeros"] == []


def test_money_field_is_left_to_the_dollars_section():
    cache = build_cache(models={"m-a": usage(tokens=10, cost_usd=0)})
    audit = quotamirror.audit_units(cache)

    assert [row["field"] for row in audit["zeros"]] == []


# --- slice 2: a series whose unit has to be inferred -----------------------


def test_infers_series_unit_by_exact_match_against_declared_totals():
    cache = build_cache(
        models={"m-a": usage(tokens=5)},
        activity=[
            {"date": "2026-04-01", "messageCount": 4, "sessionCount": 1, "toolCallCount": 3},
            {"date": "2026-04-02", "messageCount": 6, "sessionCount": 2, "toolCallCount": 2},
        ],
        total_messages=10,
        total_sessions=3,
        hourCounts={"8": 2, "9": 1},
    )
    audit = quotamirror.audit_units(cache)

    by_name = {row["name"]: row for row in audit["series"]}
    assert by_name["dailyActivity.messageCount"]["unit"] == "messages"
    assert by_name["dailyActivity.messageCount"]["matched_total"] == "totalMessages"
    assert by_name["dailyActivity.sessionCount"]["unit"] == "sessions"
    assert by_name["hourCounts"]["unit"] == "sessions"
    assert by_name["hourCounts"]["sum"] == 3
    assert by_name["dailyActivity.toolCallCount"]["verdict"] == "no_declared_total"


# --- slice 3: a percent-named field holding a fraction ---------------------


def test_flags_percent_named_field_holding_a_fraction():
    cache = build_cache(models={"m-a": usage(tokens=10)}, usagePct=0.4, fiveHourUtilization=0.9)
    audit = quotamirror.audit_units(cache)

    percents = {row["field"]: row for row in audit["percents"]["suspect"]}
    assert set(percents) == {"usagePct", "fiveHourUtilization"}
    assert percents["usagePct"]["max"] == 0.4
    assert percents["usagePct"]["verdict"] == "suspect_fraction"


def test_percent_check_reports_how_many_fields_it_audited():
    cache = build_cache(models={"m-a": usage(tokens=10)}, usagePct=42)
    audit = quotamirror.audit_units(cache)

    assert audit["percents"]["audited"] == 1
    assert audit["percents"]["suspect"] == []


# --- slice 4: the text receipt --------------------------------------------


def test_render_units_names_the_zero_capacity_field_and_its_consequence():
    cache = build_cache(
        models={
            "m-a": usage(tokens=10, contextWindow=0),
            "m-b": usage(tokens=10, contextWindow=0),
        },
        activity=[{"date": "2026-04-01", "messageCount": 3}],
        total_messages=3,
    )
    text = quotamirror.render_units(quotamirror.audit_units(cache))

    assert "UNITS" in text
    assert "contextWindow" in text
    assert "zero" in text
    assert "divide" in text  # consequence is stated, not just the number
    assert "dailyActivity.messageCount" in text
    assert "messages" in text


def test_render_units_is_explicit_when_nothing_is_wrong():
    text = quotamirror.render_units(
        quotamirror.audit_units(build_cache(models={"m-a": usage(tokens=1, contextWindow=200000)}))
    )

    # the all-clear must name the population it audited (r71: it used to fire over
    # zero models, where 'nothing is wrong' is a claim about an unread file)
    assert "none of 1 model(s) has a field that is zero on every model" in text
    assert "no percent-named field" in text


# --- slice 5: the command -------------------------------------------------


def test_units_command_emits_json(tmp_path, capsys):
    path = tmp_path / "stats-cache.json"
    path.write_text(
        json.dumps(build_cache(models={"m-a": usage(tokens=10, contextWindow=0)})),
        encoding="utf-8",
    )
    code = quotamirror.main(["units", "--path", str(path), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["zeros"][0]["field"] == "contextWindow"


def test_units_command_rejects_a_missing_file(tmp_path, capsys):
    code = quotamirror.main(["units", "--path", str(tmp_path / "nope.json")])

    assert code == 2


def test_percent_check_does_not_mistake_a_duration_for_a_ratio():
    cache = build_cache(
        models={"m-a": usage(tokens=1)},
        longestSession={"duration": 625069131, "messageCount": 1469},
    )
    audit = quotamirror.audit_units(cache)

    assert audit["percents"]["audited"] == 0
