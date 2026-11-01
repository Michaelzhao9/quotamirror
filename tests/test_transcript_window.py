"""Tests for `compare --claude-transcripts`: measure Claude's window from *this* side.

`nested` can be handed a cache and print its declared day table beside the
measured transcript window. `compare`, which prints the cache day table, must
also measure the transcript window so one command can explain both windows.

The scan here is deliberately the *window* measurement, not `nested`'s full
per-scope audit: same dedupe rule (merge repeated `message.id` keeping the larger
value per bucket, so the two commands cannot disagree about the same corpus), same
day attribution, but the output is per-day totals plus a stamped window and the
calendar difference against the cache day table.

All fixtures are synthetic.
"""

import json

import quotamirror


def usage_line(message_id, ts, input_tokens=10, output_tokens=0, cache_read=0, sidechain=False):
    record = {
        "type": "assistant",
        "uuid": f"u-{message_id}-{ts}",
        "isSidechain": sidechain,
        "message": {
            "id": message_id,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if ts:
        record["timestamp"] = ts
    return record


def write_transcripts(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, lines in files.items():
        (root / name).write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return root


def write_rollout(tmp_path, lines):
    path = tmp_path / "rollout-2026-01-01T00-00-00.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


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


EMPTY_CACHE = {"modelUsage": {}, "dailyModelTokens": []}


def test_the_window_is_measured_from_record_timestamps_and_says_so(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z"),
                usage_line("m2", "2026-05-03T10:00:00Z"),
            ],
            "b.jsonl": [usage_line("m3", "2026-05-02T10:00:00Z")],
        },
    )
    window = quotamirror.transcript_window(root)

    assert window["window"] == {"first": "2026-05-01", "last": "2026-05-03", "days": 3, "basis": "record_timestamp"}
    assert window["files"] == 2


def test_a_repeated_message_id_is_merged_not_summed(tmp_path):
    """The cache-side lesson, applied here so the two commands cannot disagree."""
    root = write_transcripts(
        tmp_path / "projects",
        {"a.jsonl": [usage_line("m1", "2026-05-01T10:00:00Z", 10), usage_line("m1", "2026-05-01T10:00:01Z", 10)]},
    )
    window = quotamirror.transcript_window(root)

    assert window["records"] == 2
    assert window["messages"] == 1
    assert window["tokens"] == 10
    assert window["naive_tokens"] == 20


def test_a_record_without_a_timestamp_narrows_the_claim(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {"a.jsonl": [usage_line("m1", "2026-05-02T10:00:00Z"), usage_line("m2", None)]},
    )
    window = quotamirror.transcript_window(root)

    assert window["window"]["first"] == "2026-05-02"
    assert window["coverage"]["records_without_timestamp"] == 1
    assert "does not bound" in window["coverage"]["note"]


def test_per_day_totals_carry_their_own_tokens(tmp_path):
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m2", "2026-05-03T10:00:00Z", 5),
            ]
        },
    )
    window = quotamirror.transcript_window(root)

    assert window["day_totals"] == {"2026-05-01": 10, "2026-05-03": 5}


def test_compare_reports_both_claude_windows_and_the_days_only_one_holds(tmp_path):
    write_rollout(tmp_path, [meta("2026-05-01T10:00:00Z"), event("2026-05-01T10:00:05Z", 100, 100)])
    transcripts = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m2", "2026-05-02T10:00:00Z", 90),
            ]
        },
    )
    cache = {
        "modelUsage": {"opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [{"date": "2026-05-01", "tokensByModel": {"opus": 10}}],
    }
    payload = quotamirror.compare_harnesses(tmp_path, cache, claude_transcripts=transcripts)
    window = payload["claude_window"]

    assert window["available"] is True
    assert (window["cache"]["first"], window["cache"]["last"]) == ("2026-05-01", "2026-05-01")
    assert window["cache"]["basis"] == "declared_attribution"
    assert (window["transcript"]["first"], window["transcript"]["last"]) == ("2026-05-01", "2026-05-02")
    assert window["transcript"]["basis"] == "record_timestamp"
    assert window["days_transcript_only"] == ["2026-05-02"]
    assert window["tokens_transcript_only"] == 90
    assert window["pct_transcript_only"] == 90.0
    assert payload["stamps"]["claude.transcripts"]["window"]["basis"] == "record_timestamp"


def test_the_text_prints_the_calendar_gap_and_refuses_to_call_it_the_whole_gap(tmp_path):
    write_rollout(tmp_path, [meta("2026-05-01T10:00:00Z"), event("2026-05-01T10:00:05Z", 100, 100)])
    transcripts = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m2", "2026-05-02T10:00:00Z", 90),
            ]
        },
    )
    cache = {
        "modelUsage": {"opus": {"inputTokens": 30, "costUSD": 0.0}},
        "dailyModelTokens": [{"date": "2026-05-01", "tokensByModel": {"opus": 30}}],
    }
    text = quotamirror.render_compare(
        quotamirror.compare_harnesses(tmp_path, cache, claude_transcripts=transcripts)
    )

    assert "CLAUDE, TWO COUNTERS" in text
    assert "2026-05-02" in text
    assert "does not explain" in text


def test_when_the_shared_days_agree_the_text_says_the_calendar_is_the_whole_story(tmp_path):
    """The other direction: if the counters match on shared days, say so instead."""
    write_rollout(tmp_path, [meta("2026-05-01T10:00:00Z"), event("2026-05-01T10:00:05Z", 100, 100)])
    transcripts = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10),
                usage_line("m2", "2026-05-02T10:00:00Z", 90),
            ]
        },
    )
    cache = {
        "modelUsage": {"opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [{"date": "2026-05-01", "tokensByModel": {"opus": 10}}],
    }
    payload = quotamirror.compare_harnesses(tmp_path, cache, claude_transcripts=transcripts)
    text = quotamirror.render_compare(payload)

    assert payload["claude_window"]["residual"]["gap"] == 0
    assert "agree exactly" in text
    assert "does not explain" not in text


def test_without_the_flag_compare_says_which_side_is_unmeasured(tmp_path):
    write_rollout(tmp_path, [meta("2026-05-01T10:00:00Z"), event("2026-05-01T10:00:05Z", 100, 100)])
    cache = {
        "modelUsage": {"opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [{"date": "2026-05-01", "tokensByModel": {"opus": 10}}],
    }
    payload = quotamirror.compare_harnesses(tmp_path, cache)
    window = payload["claude_window"]

    assert window["available"] is False
    assert "--claude-transcripts" in window["reason"]
    assert "claude.transcripts" not in payload["stamps"]


def test_a_message_seen_on_two_days_is_attributed_to_the_earlier_one(tmp_path):
    """Day totals must sum to the total, or the day view is a second, different bill."""
    root = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T23:59:00Z", 7),
                usage_line("m1", "2026-05-02T00:01:00Z", 7),
                usage_line("m2", "2026-05-02T10:00:00Z", 3),
            ]
        },
    )
    window = quotamirror.transcript_window(root)

    assert window["day_totals"] == {"2026-05-01": 7, "2026-05-02": 3}
    assert sum(window["day_totals"].values()) == window["tokens"] == 10


def test_cli_compare_measures_the_transcript_window(tmp_path):
    import subprocess
    import sys

    write_rollout(tmp_path, [meta("2026-05-01T10:00:00Z"), event("2026-05-01T10:00:05Z", 100, 100)])
    transcripts = write_transcripts(
        tmp_path / "projects",
        {"a.jsonl": [usage_line("m1", "2026-05-01T10:00:00Z", 10), usage_line("m2", "2026-05-02T10:00:00Z", 5)]},
    )
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "modelUsage": {"opus": {"inputTokens": 10}},
                "dailyModelTokens": [{"date": "2026-05-01", "tokensByModel": {"opus": 10}}],
            }
        ),
        encoding="utf-8",
    )
    script = str(__import__("pathlib").Path(quotamirror.__file__).resolve())
    proc = subprocess.run(
        [
            sys.executable,
            script,
            "compare",
            "--codex",
            str(tmp_path),
            "--path",
            str(cache_path),
            "--claude-transcripts",
            str(transcripts),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "CLAUDE, TWO COUNTERS" in proc.stdout
    assert "2026-05-02" in proc.stdout


def test_cli_compare_exits_2_when_the_transcript_dir_is_missing(tmp_path):
    import subprocess
    import sys

    write_rollout(tmp_path, [meta("2026-05-01T10:00:00Z"), event("2026-05-01T10:00:05Z", 100, 100)])
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(json.dumps({"modelUsage": {}, "dailyModelTokens": []}), encoding="utf-8")
    script = str(__import__("pathlib").Path(quotamirror.__file__).resolve())
    proc = subprocess.run(
        [
            sys.executable,
            script,
            "compare",
            "--codex",
            str(tmp_path),
            "--path",
            str(cache_path),
            "--claude-transcripts",
            str(tmp_path / "nope"),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 2
    assert "no transcripts" in proc.stderr


def test_compare_and_nested_agree_on_the_same_corpus(tmp_path):
    """Two commands, one corpus: if these diverge, one of them is lying."""
    transcripts = write_transcripts(
        tmp_path / "projects",
        {
            "a.jsonl": [
                usage_line("m1", "2026-05-01T10:00:00Z", 10, cache_read=1000),
                usage_line("m1", "2026-05-01T10:00:01Z", 10, cache_read=1000),
                usage_line("m2", "2026-05-02T10:00:00Z", 20, sidechain=True),
            ]
        },
    )
    nested = quotamirror.nested_audit(transcripts)
    window = quotamirror.transcript_window(transcripts)

    assert window["tokens"] == nested["totals"]["all"]["total_tokens"]
    assert window["window"]["first"] == nested["totals"]["all"]["window"]["first"]
