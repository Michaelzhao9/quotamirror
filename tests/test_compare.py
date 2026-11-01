"""Tests for `compare`: a second harness's meter read by the same rule.

Claude Code's cache declares lifetime totals, so `units --cumulative` could test
a running total against a number the file itself wrote down. A second harness (Codex CLI rollouts) writes a different shape: every turn appends a
`token_count` event carrying BOTH a running total (`total_token_usage`) and the
delta for that turn (`last_token_usage`), plus an independent per-response
`token_usage_record`. That makes two things measurable that the Claude cache
could not measure:

  * the running total is monotone by construction, so summing the column is a
    provable double count -- the factor is sum/max, printed with both sides;
  * two writers record the same usage, so they can disagree, and the gap has a
    direction.

Day totals are built from the DELTA column only. A reset (a series that goes
backwards) is named, never silently summed. No dollars anywhere: these files
carry no price field, so the answer is `unknown`, not $0.00.

All fixtures are synthetic.
"""

import json
import subprocess
import sys

import pytest
from pathlib import Path

import quotamirror


def meta(ts, model="gpt-5.1-codex", session_id="s1"):
    return {"timestamp": ts, "type": "session_meta", "payload": {"session_id": session_id, "timestamp": ts}}


def turn(ts, model="gpt-5.1-codex"):
    return {"timestamp": ts, "type": "turn_context", "payload": {"model": model, "turn_id": "t1"}}


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


def record(ts, tokens):
    return {"timestamp": ts, "type": "token_usage_record", "payload": {"usage": {"input_tokens": tokens}}}


def write_rollout(tmp_path, lines, name="rollout-2026-01-01T00-00-00.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def audit_of(tmp_path, lines):
    return quotamirror.audit_codex_session(quotamirror.read_codex_session(write_rollout(tmp_path, lines)))


def test_monotone_running_total_with_matching_deltas_is_confirmed(tmp_path):
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            turn("2026-01-01T10:00:01Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 250, 150),
        ],
    )
    assert session["verdict"] == "cumulative_confirmed"
    assert session["delta_total"] == 250
    assert session["running_total"] == 250
    assert session["model"] == "gpt-5.1-codex"


def test_summing_the_running_total_column_is_named_with_its_factor(tmp_path):
    """350 is what a naive sum prints; 250 is what was spent. The factor is 1.4."""
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 250, 150),
        ],
    )
    assert session["naive_sum"] == 350
    assert session["naive_factor"] == 1.4


def test_a_series_that_goes_backwards_is_named_not_summed(tmp_path):
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 500, 500),
            event("2026-01-01T10:05:05Z", 120, 120),
        ],
    )
    assert session["verdict"] == "counter_reset"
    assert session["resets"] == [{"event_index": 1, "previous": 500, "current": 120}]


def test_deltas_short_of_the_running_total_are_named_with_direction(tmp_path):
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 250, 40),
        ],
    )
    assert session["verdict"] == "cumulative_mismatch"
    assert session["delta_total"] == 140
    assert session["running_total"] == 250
    assert session["gap"] == 110  # the deltas are short by 110, not over
    assert "deltas are short" in session["gap_note"]


def test_the_second_writer_disagreeing_gets_a_signed_gap(tmp_path):
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 250, 150),
            record("2026-01-01T10:00:05Z", 100),
            record("2026-01-01T10:01:05Z", 130),
        ],
    )
    writers = session["two_writers"]
    assert writers["verdict"] == "two_writers_disagree"
    assert writers["records_total"] == 230
    assert writers["gap"] == -20  # the per-response records are 20 short of the running total


def test_day_totals_split_the_delta_column_not_the_running_total(tmp_path):
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-02T09:00:05Z", 900, 800),
        ],
    )
    assert session["day_totals"] == {"2026-01-01": 100, "2026-01-02": 800}


def test_days_present_in_only_one_harness_are_named(tmp_path):
    codex_lines = [
        meta("2026-01-01T10:00:00Z"),
        event("2026-01-01T10:00:05Z", 100, 100),
        event("2026-01-03T10:00:05Z", 400, 300),
    ]
    write_rollout(tmp_path, codex_lines)
    claude = {
        "modelUsage": {"claude-opus": {"inputTokens": 50, "costUSD": 0.0}},
        "dailyModelTokens": [
            {"date": "2026-01-01", "tokensByModel": {"claude-opus": 50}},
            {"date": "2026-01-02", "tokensByModel": {"claude-opus": 25}},
        ],
    }
    payload = quotamirror.compare_harnesses(tmp_path, claude)
    assert payload["overlap"]["days_both"] == ["2026-01-01"]
    assert payload["overlap"]["days_codex_only"] == ["2026-01-03"]
    assert payload["overlap"]["days_claude_only"] == ["2026-01-02"]
    assert payload["codex"]["tokens"] == 400
    assert payload["claude"]["tokens"] == 75


def test_malformed_lines_are_counted_not_fatal(tmp_path):
    path = tmp_path / "rollout-broken.jsonl"
    path.write_text(
        json.dumps(meta("2026-01-01T10:00:00Z"))
        + '\n{"token_count": {"info": truncated\n'
        + json.dumps(event("2026-01-01T10:00:05Z", 10, 10))
        + "\n",
        encoding="utf-8",
    )
    session = quotamirror.read_codex_session(path)
    assert session["skipped_lines"] == 1
    assert len(session["events"]) == 1


def test_a_session_with_no_token_events_says_so_instead_of_zero(tmp_path):
    session = audit_of(tmp_path, [meta("2026-01-01T10:00:00Z"), record("2026-01-01T10:00:05Z", 10)])
    assert session["verdict"] == "no_token_events"
    assert session["delta_total"] is None


def test_compare_output_has_no_dollar_figure(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    claude = {"modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.0}}, "dailyModelTokens": []}
    payload = quotamirror.compare_harnesses(tmp_path, claude)
    text = quotamirror.render_compare(payload)
    assert "$" not in text
    assert payload["codex"]["dollars"]["status"] == "unknown"
    assert payload["claude"]["dollars"]["status"] == "unknown"


def test_cli_compare_runs_against_both_sources(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(json.dumps({"modelUsage": {"m": {"inputTokens": 4}},"dailyModelTokens": []}))
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(quotamirror.__file__)),
            "compare",
            "--codex",
            str(tmp_path),
            "--path",
            str(cache_path),
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["codex"]["tokens"] == 10
    # The cache declares 4 tokens per model but has no day rows for them: the day
    # total is what can be compared, and the difference is named, not hidden.
    assert payload["claude"]["tokens"] == 0
    assert payload["claude"]["lifetime_tokens"] == 4
    assert payload["claude"]["declared_vs_daily"]["gap"] == 4
    assert "no day" in payload["claude"]["declared_vs_daily"]["note"]


def test_the_delta_column_can_overstate_where_the_records_agree(tmp_path):
    """Real machine finding: two sessions had sum(delta) above max(running) while the
    per-response records matched the running total exactly. The delta column is a
    column, not a truth; the third witness is what shows it."""
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 250, 220),
            record("2026-01-01T10:00:05Z", 100),
            record("2026-01-01T10:01:05Z", 150),
        ],
    )
    witnesses = session["witnesses"]
    assert witnesses["delta_total"] == 320
    assert witnesses["running_total"] == 250
    assert witnesses["records_total"] == 250
    assert witnesses["agreement"] == "running_total+records"
    assert "deltas overstate" in witnesses["note"]


def test_all_three_witnesses_agreeing_is_stated_not_assumed(tmp_path):
    session = audit_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            record("2026-01-01T10:00:05Z", 100),
        ],
    )
    assert session["witnesses"]["agreement"] == "all_three"
    assert session["witnesses"]["note"] == "three independent numbers, one total"


def test_compare_aggregates_each_witness_separately(tmp_path):
    write_rollout(
        tmp_path,
        [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100), record("2026-01-01T10:00:05Z", 100)],
        name="rollout-a.jsonl",
    )
    write_rollout(
        tmp_path,
        [meta("2026-01-02T10:00:00Z"), event("2026-01-02T10:00:05Z", 250, 220)],
        name="rollout-b.jsonl",
    )
    payload = quotamirror.compare_harnesses(tmp_path, {"modelUsage": {}, "dailyModelTokens": []})
    witnesses = payload["codex"]["witnesses"]
    assert witnesses["delta_total"] == 320
    assert witnesses["running_total"] == 350
    assert witnesses["records_total"] == 100
    assert witnesses["sessions_without_records"] == 1
    text = quotamirror.render_compare(payload)
    assert "delta column" in text


def test_the_text_names_which_witness_disagrees_per_session(tmp_path):
    write_rollout(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 250, 220),
            record("2026-01-01T10:00:05Z", 100),
            record("2026-01-01T10:01:05Z", 150),
        ],
    )
    text = quotamirror.render_compare(
        quotamirror.compare_harnesses(tmp_path, {"modelUsage": {}, "dailyModelTokens": []})
    )
    assert "deltas overstate the running total by 70" in text


# Contract: every total states whether it includes descendants and whether it
# is a delta or a cumulative counter. Unstamped totals cannot be compared.
# ---------------------------------------------------------------------------

STAMP_KEYS = {"total_tokens", "includes_descendants", "accounting", "window", "basis", "scope_source"}
SCOPES = {"root", "sidechain", "root+sidechain", "unknown"}


def thread_meta(ts, source="user", parent=None, fork=None, ordinal=None, nickname=None):
    payload = {"session_id": "s-" + ts, "timestamp": ts, "thread_source": source}
    if parent:
        payload["parent_thread_id"] = parent
    if fork:
        payload["forked_from_id"] = fork
    if ordinal is not None:
        payload["subagent_history_start_ordinal"] = ordinal
    if nickname:
        payload["agent_nickname"] = nickname
    return {"timestamp": ts, "type": "session_meta", "payload": payload}


def empty_cache(**extra):
    cache = {"modelUsage": {}, "dailyModelTokens": [], "dailyActivity": []}
    cache.update(extra)
    return cache


def test_every_total_compare_prints_carries_both_stamps(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    payload = quotamirror.compare_harnesses(tmp_path, empty_cache())
    stamps = payload["stamps"]
    assert {"codex.delta", "codex.running", "codex.records", "claude.daily", "claude.declared"} <= set(stamps)
    for name, stamp in stamps.items():
        assert STAMP_KEYS <= set(stamp), (name, sorted(set(stamp)))
        assert stamp["includes_descendants"] in SCOPES, name
        assert stamp["accounting"] in {"delta", "cumulative"}, name
        assert {"first", "last", "days"} <= set(stamp["window"]), name


def test_the_claude_cache_total_is_scope_unknown_and_says_why(tmp_path):
    """No field in the file names a scope, so the honest stamp is unknown -- and
    the count of fields scanned is printed, not a shrug."""
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    payload = quotamirror.compare_harnesses(tmp_path, empty_cache(totalSessions=3, modelUsage={"m": {"inputTokens": 4}}))
    for name in ("claude.daily", "claude.declared"):
        stamp = payload["stamps"][name]
        assert stamp["includes_descendants"] == "unknown"
        assert stamp["scope_fields"] == []
        assert "no field" in stamp["scope_reason"]


def test_a_scope_named_field_is_named_without_upgrading_the_stamp(tmp_path):
    """A field called subagentTokens is a claim, not an arithmetic this tool checked."""
    cache = empty_cache(subagentTokens=5, modelUsage={"m": {"inputTokens": 4}})
    payload = quotamirror.compare_harnesses(tmp_path, cache)
    stamp = payload["stamps"]["claude.declared"]
    assert stamp["includes_descendants"] == "unknown"
    assert "subagentTokens" in stamp["scope_fields"]
    assert "scope field(s)" in stamp["scope_reason"]


def test_codex_subagent_threads_change_the_scope_stamp(tmp_path):
    """Codex rollouts DO name their scope: thread_source + parent_thread_id.

    A corpus with a delegated thread is root+sidechain; a corpus of root threads
    only is root. Neither is guessed from a field name -- both are read.
    """
    write_rollout(
        tmp_path,
        [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100)],
        name="rollout-root.jsonl",
    )
    write_rollout(
        tmp_path,
        [
            thread_meta("2026-01-01T11:00:00Z", source="subagent", parent="p1", nickname="Noether"),
            event("2026-01-01T11:00:05Z", 40, 40),
        ],
        name="rollout-sub.jsonl",
    )
    payload = quotamirror.compare_harnesses(tmp_path, empty_cache())
    stamp = payload["stamps"]["codex.delta"]
    assert stamp["includes_descendants"] == "root+sidechain"
    assert stamp["subagent_sessions"] == 1
    assert stamp["total_tokens"] == 140
    only_root = quotamirror.compare_harnesses(
        tmp_path, empty_cache(), max_sessions=2
    )
    assert only_root["stamps"]["codex.delta"]["includes_descendants"] == "root+sidechain"


def test_a_corpus_of_root_threads_only_is_stamped_root(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    write_rollout(sub, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100)], name="r.jsonl")
    payload = quotamirror.compare_harnesses(sub, empty_cache())
    assert payload["stamps"]["codex.delta"]["includes_descendants"] == "root"


def test_the_running_total_witness_is_stamped_cumulative(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100)], name="r.jsonl")
    payload = quotamirror.compare_harnesses(tmp_path, empty_cache())
    stamp = payload["stamps"]["codex.running"]
    assert stamp["accounting"] == "cumulative"
    assert stamp["total_tokens"] == 100
    assert payload["stamps"]["codex.delta"]["accounting"] == "delta"


def test_a_fork_inheriting_a_running_total_is_named_not_summed(tmp_path):
    """A forked thread may start its counter where its parent left off.

    Summing that column across the family counts the inherited prefix again.
    This synthetic fixture verifies the prefix is named rather than summed.
    """
    write_rollout(
        tmp_path,
        [
            thread_meta("2026-01-01T09:00:00Z", fork="parent-1", ordinal=12),
            event("2026-01-01T09:00:05Z", 5_000_000, 1_000),
        ],
        name="rollout-fork.jsonl",
    )
    payload = quotamirror.compare_harnesses(tmp_path, empty_cache())
    stamp = payload["stamps"]["codex.running"]
    assert stamp["fork_sessions"] == 1
    assert stamp["inherited_threads"] == 1
    assert stamp["inherited_prefix_tokens"] == 5_000_000
    assert "inherited" in stamp["witness_note"]


def test_the_text_prints_the_stamps_on_every_total_line(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100)], name="r.jsonl")
    payload = quotamirror.compare_harnesses(
        tmp_path, empty_cache(dailyModelTokens=[{"date": "2026-01-01", "tokensByModel": {"m": 40}}])
    )
    text = quotamirror.render_compare(payload)
    assert text.count("includes_descendants=") >= 4
    assert "accounting=cumulative" in text
    assert "includes_descendants=unknown" in text


def test_the_text_says_when_the_two_totals_do_not_cover_the_same_days(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-05T10:00:00Z"), event("2026-01-05T10:00:05Z", 100, 100)], name="r.jsonl")
    payload = quotamirror.compare_harnesses(
        tmp_path, empty_cache(dailyModelTokens=[{"date": "2025-12-01", "tokensByModel": {"m": 40}}])
    )
    text = quotamirror.render_compare(payload)
    assert payload["comparability"]["same_window"] is False
    assert "windows differ" in text and "must not be subtracted" in text


def test_the_text_says_when_the_two_totals_do_cover_the_same_days(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 100, 100)], name="r.jsonl")
    payload = quotamirror.compare_harnesses(
        tmp_path, empty_cache(dailyModelTokens=[{"date": "2026-01-01", "tokensByModel": {"m": 40}}])
    )
    text = quotamirror.render_compare(payload)
    assert payload["comparability"]["same_window"] is True
    assert "both totals cover 2026-01-01..2026-01-01" in text


# A window states how it was obtained (measured from records or declared by a
# day table), and the output names the other command that measures the pair.

BASIS = {"record_timestamp", "declared_attribution", "none"}


def test_every_window_says_how_it_was_obtained(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    payload = quotamirror.compare_harnesses(
        tmp_path, empty_cache(dailyModelTokens=[{"date": "2026-01-01", "tokensByModel": {"m": 10}}])
    )
    stamps = payload["stamps"]
    for name, stamp in stamps.items():
        assert stamp["window"]["basis"] in BASIS, name
    assert stamps["codex.delta"]["window"]["basis"] == "record_timestamp"
    assert stamps["claude.daily"]["window"]["basis"] == "declared_attribution"
    assert stamps["claude.declared"]["window"]["basis"] == "none"


def test_the_text_prints_the_window_basis_next_to_every_total(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    payload = quotamirror.compare_harnesses(
        tmp_path, empty_cache(dailyModelTokens=[{"date": "2026-01-01", "tokensByModel": {"m": 10}}])
    )
    text = quotamirror.render_compare(payload)
    assert "basis=record_timestamp" in text
    assert "basis=declared_attribution" in text


def test_the_comparability_section_names_the_command_that_measures_a_second_window(tmp_path):
    """One machine, one quantity, two windows: whichever command the reader ran
    must point at the other one instead of leaving two unexplained totals."""
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    payload = quotamirror.compare_harnesses(
        tmp_path, empty_cache(dailyModelTokens=[{"date": "2026-01-05", "tokensByModel": {"m": 10}}])
    )
    text = quotamirror.render_compare(payload)
    assert "nested" in text
    assert "second window" in text
    assert payload["comparability"]["basis_note"]


def test_a_window_basis_that_is_not_measured_or_declared_is_refused():
    with pytest.raises(ValueError):
        quotamirror._window(["2026-01-01"], "cursor_mtime")
