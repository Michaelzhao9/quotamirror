"""DAY OVERLAP must say which Claude table it measured against.

`compare` prints one Claude day table by default: the cache's declared
attribution days. With `--claude-transcripts` it measures a *second* Claude
window from record timestamps -- and on a real machine those two windows can
land on opposite sides of the Codex window. The command then printed

    in both : 0 -- no day can be in both: the two windows do not intersect

while holding, in the same payload, a measured transcript table that shared
days with Codex. The zero was true of the cache table and false of the tool's
own measurement, and the sentence attributed it to "claude" without saying
which table that was.

These tests pin: when a transcript scan is available, the section names its
basis and the measured overlap is printed as its own block. Fixtures are
synthetic.
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


def setup_machine(tmp_path):
    """Codex on 01-10..01-11, cache day table on 01-01..01-02, transcripts on 01-10..01-11."""
    rollout = tmp_path / "codex" / "rollout-2026-01-10T00-00-00.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        "".join(
            json.dumps(line) + "\n"
            for line in [
                meta("2026-01-10T10:00:00Z"),
                event("2026-01-10T10:00:05Z", 100, 100),
                event("2026-01-11T10:00:05Z", 300, 200),
            ]
        ),
        encoding="utf-8",
    )
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "session-a.jsonl").write_text(
        "".join(
            json.dumps(usage_line(f"m{i}", f"2026-01-{10 + i}T09:00:00Z")) + "\n"
            for i in range(2)
        ),
        encoding="utf-8",
    )
    cache = {
        "modelUsage": {"claude-opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [
            {"date": "2026-01-01", "tokensByModel": {"claude-opus": 10}},
            {"date": "2026-01-02", "tokensByModel": {"claude-opus": 10}},
        ],
    }
    return rollout.parent, transcripts, cache


def test_a_measured_transcript_window_gets_its_own_overlap_block(tmp_path):
    codex_root, transcripts, cache = setup_machine(tmp_path)
    payload = quotamirror.compare_harnesses(
        codex_root, cache, claude_transcripts=transcripts
    )
    measured = payload["overlap_transcript"]
    assert measured["days_both"] == ["2026-01-10", "2026-01-11"]
    assert measured["days_codex_only"] == []

    text = quotamirror.render_compare(payload)
    basis_lines = [line for line in text.splitlines() if line.startswith("DAY OVERLAP")]
    assert basis_lines == [
        "DAY OVERLAP (claude side: cache day table)",
        "DAY OVERLAP (claude side: transcript record dates)",
    ]


def test_the_cache_block_names_its_own_table_inside_the_sentence(tmp_path):
    """The reason sentence must not say "claude" while another claude table is on screen."""
    codex_root, transcripts, cache = setup_machine(tmp_path)
    payload = quotamirror.compare_harnesses(
        codex_root, cache, claude_transcripts=transcripts
    )
    text = quotamirror.render_compare(payload)
    assert "claude cache table 2026-01-01..2026-01-02" in text


def test_the_transcript_block_names_its_table_too(tmp_path):
    """When Codex sits outside the measured window, the reason names that table."""
    rollout = tmp_path / "codex" / "rollout-2026-02-01T00-00-00.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "".join(
            json.dumps(line) + "\n"
            for line in [
                meta("2026-02-01T10:00:00Z"),
                event("2026-02-01T10:00:05Z", 100, 100),
            ]
        ),
        encoding="utf-8",
    )
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "session-a.jsonl").write_text(
        json.dumps(usage_line("m1", "2026-01-10T09:00:00Z")) + "\n", encoding="utf-8"
    )
    cache = {
        "modelUsage": {"claude-opus": {"inputTokens": 10, "costUSD": 0.0}},
        "dailyModelTokens": [{"date": "2026-01-10", "tokensByModel": {"claude-opus": 10}}],
    }
    payload = quotamirror.compare_harnesses(
        rollout.parent, cache, claude_transcripts=transcripts
    )
    text = quotamirror.render_compare(payload)
    assert "claude transcript 2026-01-10..2026-01-10" in text


def test_a_zero_one_sided_count_says_which_side_it_measured(tmp_path):
    """`codex only: 0` means every codex day is shared -- not every claude day."""
    codex_root, transcripts, cache = setup_machine(tmp_path)
    payload = quotamirror.compare_harnesses(
        codex_root, cache, claude_transcripts=transcripts
    )
    text = quotamirror.render_compare(payload)
    assert "codex only  : 0 -- every codex day is in both tables" in text
    assert "claude only : 0 -- every claude day is in both tables" in text
