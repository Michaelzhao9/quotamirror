"""The day table's span, and the models it never names.

`scan` prints a signed gap between the two lifetime views in one receipt. That
gap is only readable if the per-day view is known to cover the same span as the
per-model view, and only nameable if the models holding it are printed as
`no day row` rather than as a measured zero. Both are measured here from the
receipt's own two day tables.

All fixtures are synthetic. No real user data is stored in this repository.
"""

from test_scan import build_cache, day, usage

import quotamirror


def test_a_model_the_day_table_never_names_is_not_a_measured_zero():
    cache = build_cache(
        models={
            "claude-a": usage(input_tokens=1000),
            "claude-b": usage(input_tokens=300),
        },
        daily=[day("2026-05-03", **{"claude-a": 1000})],
        activity=[{"date": "2026-05-03", "messageCount": 1}],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    row = next(g for g in attribution["gaps"] if g["model"] == "claude-b")
    assert row["presence"] == "no_day_row"
    assert row["day_rows"] == 0
    assert row["coverage"] is None
    assert row["daily"] == 0
    assert attribution["gap_models_without_day_rows"] == 1


def test_each_gap_names_how_many_day_rows_and_what_share_they_cover():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000)},
        daily=[day("2026-05-03", **{"claude-a": 100}), day("2026-05-04", **{"claude-a": 50})],
        activity=[{"date": "2026-05-03", "messageCount": 1}],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    row = attribution["gaps"][0]
    assert row["day_rows"] == 2
    assert row["presence"] == "day_rows"
    assert row["coverage"] == round(150 / 1000, 4)
    assert attribution["gap_models"] == 1


def test_day_view_is_a_floor_when_activity_starts_before_the_token_table():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000)},
        daily=[day("2026-05-03", **{"claude-a": 1000})],
        activity=[
            {"date": "2026-04-01", "messageCount": 1},
            {"date": "2026-04-02", "messageCount": 1},
            {"date": "2026-05-03", "messageCount": 1},
        ],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["day_table_first"] == "2026-05-03"
    assert attribution["activity_first"] == "2026-04-01"
    assert attribution["days_before_day_table"] == 2
    assert attribution["day_table_truncated"] is True
    assert attribution["span_verdict"] == "day_view_is_a_floor"


def test_same_span_when_both_tables_start_on_the_same_day():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000)},
        daily=[day("2026-05-03", **{"claude-a": 1000})],
        activity=[{"date": "2026-05-03", "messageCount": 1}],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["days_before_day_table"] == 0
    assert attribution["day_table_truncated"] is False
    assert attribution["span_verdict"] == "same_span"


def test_span_is_not_comparable_when_one_table_is_absent():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000)},
        daily=[day("2026-05-03", **{"claude-a": 1000})],
        activity=[],
    )

    attribution = quotamirror.summarize(cache)["attribution"]

    assert attribution["span_verdict"] == "not_comparable"
    assert attribution["day_table_truncated"] is False
    assert attribution["days_before_day_table"] == 0


def test_render_says_no_day_row_instead_of_a_zero_for_an_unnamed_model():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000), "claude-b": usage(input_tokens=300)},
        daily=[day("2026-05-03", **{"claude-a": 1000})],
        activity=[{"date": "2026-05-03", "messageCount": 1}],
    )

    text = quotamirror.render_text(quotamirror.summarize(cache), source="fixture")

    line = next(ln for ln in text.splitlines() if "claude-b" in ln and "gap" in ln)
    assert "no day row" in line
    assert "daily 0" not in line


def test_render_names_the_days_the_token_table_does_not_reach():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000)},
        daily=[day("2026-05-03", **{"claude-a": 1000})],
        activity=[
            {"date": "2026-04-01", "messageCount": 1},
            {"date": "2026-04-02", "messageCount": 1},
            {"date": "2026-05-03", "messageCount": 1},
        ],
    )

    text = quotamirror.render_text(quotamirror.summarize(cache), source="fixture")

    assert "2 day(s) of activity sit before the day table" in text
    assert "floor over the days it covers" in text


def test_render_prints_coverage_for_a_partly_covered_model():
    cache = build_cache(
        models={"claude-a": usage(input_tokens=1000)},
        daily=[day("2026-05-03", **{"claude-a": 150})],
        activity=[{"date": "2026-05-03", "messageCount": 1}],
    )

    text = quotamirror.render_text(quotamirror.summarize(cache), source="fixture")

    assert "150 of 1,000 (15.00%)" in text
