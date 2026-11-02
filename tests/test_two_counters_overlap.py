"""`days in both : 0` under CLAUDE, TWO COUNTERS must say which zero it is.

The DAY OVERLAP section learned this rule first: a zero that could not have
been anything else is a placeholder, not a measurement. The pair printed by
CLAUDE, TWO COUNTERS has the same shape -- a transcript-measured window beside
a cache-declared day table -- and printed the bare `0` until now.

Every fixture is synthetic.
"""

import json

import quotamirror


def usage_line(message_id, ts, input_tokens=10):
    return {
        "type": "assistant",
        "uuid": f"u-{message_id}-{ts}",
        "isSidechain": False,
        "timestamp": ts,
        "message": {
            "id": message_id,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def write_transcripts(root, days):
    root.mkdir(parents=True, exist_ok=True)
    lines = [usage_line(f"m{d}", f"{d}T10:00:00Z") for d in days]
    (root / "session.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return root


def codex_rollout(tmp_path):
    path = tmp_path / "rollout-2026-01-01T00-00-00.jsonl"
    lines = [
        {"timestamp": "2026-01-01T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": "s1", "timestamp": "2026-01-01T10:00:00Z"}},
        {"timestamp": "2026-01-01T10:00:05Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"total_tokens": 100, "input_tokens": 100},
             "last_token_usage": {"total_tokens": 100, "input_tokens": 100}}}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def claude_cache(days):
    return {
        "modelUsage": {"claude-opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [
            {"date": d, "tokensByModel": {"claude-opus": 10}} for d in days
        ],
    }


def run_compare(tmp_path, transcript_days, cache_days):
    codex_rollout(tmp_path)
    transcripts = write_transcripts(tmp_path / "transcripts", transcript_days)
    payload = quotamirror.compare_harnesses(
        tmp_path, claude_cache(cache_days), claude_transcripts=transcripts
    )
    return payload["claude_window"], quotamirror.render_compare(payload)


def test_disjoint_windows_do_not_report_a_bare_zero(tmp_path):
    pair, text = run_compare(
        tmp_path, ["2026-02-10", "2026-02-11"], ["2026-01-01", "2026-01-02"]
    )
    assert pair["days_both"] == []
    assert pair["coverage"] == "disjoint_windows"
    assert "do not intersect" in pair["note"]
    assert "2026-02-10..2026-02-11" in pair["note"]
    assert "2026-01-01..2026-01-02" in pair["note"]
    assert "days in both : 0 -- no day can be in both" in text


def test_intersecting_windows_without_a_shared_day_say_which_case_it_is(tmp_path):
    pair, text = run_compare(
        tmp_path, ["2026-01-01", "2026-01-04"], ["2026-01-02", "2026-01-03"]
    )
    assert pair["days_both"] == []
    assert pair["coverage"] == "no_shared_day"
    assert "no calendar day carries usage in both tables" in pair["note"]
    assert "days in both : 0 -- the two windows intersect" in text


def test_a_real_shared_day_keeps_the_plain_line(tmp_path):
    pair, text = run_compare(
        tmp_path, ["2026-01-01", "2026-01-05"], ["2026-01-01", "2026-01-02"]
    )
    assert pair["coverage"] == "shared_days"
    assert pair["note"] == ""
    assert "days in both : 1" in text
    assert "days in both : 1 --" not in text
    assert pair["days_transcript_only"] == ["2026-01-05"]
    assert pair["days_cache_only"] == ["2026-01-02"]
