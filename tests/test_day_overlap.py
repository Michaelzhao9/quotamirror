"""The DAY OVERLAP section must not print a bare zero it did not measure.

`in both : 0` is a true statement about the day tables, but on a machine where
the two harnesses cover disjoint calendar windows it is a structural
impossibility, not a result -- and this tool's own rule (r63/r69/r71) says a
zero that could not have been anything else is a placeholder, not a count.
The same applies when one side produced no day at all.

These tests drive the payload and the rendered text; every fixture is synthetic.
"""

import json

import quotamirror


def meta(ts):
    return {"timestamp": ts, "type": "session_meta", "payload": {"session_id": "s1", "timestamp": ts}}


def event(ts, running, delta):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": running, "input_tokens": running},
                "last_token_usage": {"total_tokens": delta, "input_tokens": delta},
            },
        },
    }


def write_rollout(tmp_path, lines, name="rollout-2026-01-10T00-00-00.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def claude_cache(days):
    return {
        "modelUsage": {"claude-opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [{"date": d, "tokensByModel": {"claude-opus": 10}} for d in days],
    }


def compare(tmp_path, codex_lines, claude_days):
    write_rollout(tmp_path, codex_lines)
    payload = quotamirror.compare_harnesses(tmp_path, claude_cache(claude_days))
    return payload, quotamirror.render_compare(payload)


def test_disjoint_windows_do_not_report_a_zero_overlap(tmp_path):
    payload, text = compare(
        tmp_path,
        [meta("2026-01-10T10:00:00Z"), event("2026-01-10T10:00:05Z", 100, 100),
         event("2026-01-11T10:00:05Z", 300, 200)],
        ["2026-01-01", "2026-01-02"],
    )
    overlap = payload["overlap"]
    assert overlap["days_both"] == []
    assert overlap["coverage"] == "disjoint_windows"
    assert "do not intersect" in overlap["note"]
    assert "2026-01-10..2026-01-11" in overlap["note"]
    assert "2026-01-01..2026-01-02" in overlap["note"]
    # the count is still printed -- with the reason it cannot be anything else
    assert "in both     : 0 -- no day can be in both" in text


def test_intersecting_windows_without_a_shared_day_say_which_case_it_is(tmp_path):
    payload, text = compare(
        tmp_path,
        [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100),
         event("2026-01-04T10:00:05Z", 300, 200)],
        ["2026-01-02", "2026-01-03"],
    )
    overlap = payload["overlap"]
    assert overlap["days_both"] == []
    assert overlap["coverage"] == "no_shared_day"
    assert "intersect" in overlap["note"]
    assert "no calendar day carries usage in both" in overlap["note"]
    # a bare zero must not appear anywhere on this path either
    assert "in both     : 0 -- the two windows intersect" in text


def test_one_side_with_no_day_is_not_measured_rather_than_zero(tmp_path):
    payload, text = compare(tmp_path, [], ["2026-01-01"])
    overlap = payload["overlap"]
    assert overlap["coverage"] == "no_days"
    assert "codex produced no day" in overlap["note"]
    assert "in both     : not measured -- codex produced no day" in text


def test_a_real_shared_day_keeps_the_plain_line_and_names_nothing(tmp_path):
    payload, text = compare(
        tmp_path,
        [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100),
         event("2026-01-03T10:00:05Z", 300, 200)],
        ["2026-01-01", "2026-01-02"],
    )
    overlap = payload["overlap"]
    assert overlap["coverage"] == "shared_days"
    assert overlap["note"] == ""
    assert "in both     : 1\n" in text
    assert overlap["days_codex_only"] == ["2026-01-03"]
    assert overlap["days_claude_only"] == ["2026-01-02"]
