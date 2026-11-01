"""Tests for the arbitration between nested's two totals and the settable floor.

The per-day cross-check can show that a cache day table agrees with a record
sum rather than a merged-message total. `nested` must carry the same arbitration
when given a cache, and say the question is unsettled when no cache is available.

The floor and tolerance that decide the verdict are settable and printed with
the values in force, so the decision can be reproduced.

All fixtures are synthetic.
"""

import json
import subprocess
import sys
from pathlib import Path

import quotamirror


def line(message_id, uuid, output_tokens=0, ts=None, sidechain=False):
    record = {
        "type": "assistant",
        "uuid": uuid,
        "isSidechain": sidechain,
        "sessionId": "s1",
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": 0,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    if ts is not None:
        record["timestamp"] = ts
    return record


def write_transcripts(root, lines):
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8"
    )
    return root


def cache_with_day_totals(day_totals):
    """A cache whose day table carries, per day, the number a vendor would print."""
    return {
        "modelUsage": {"m": {"inputTokens": 5}},
        "dailyModelTokens": [
            {"date": day, "tokensByModel": {"m": tokens}}
            for day, tokens in sorted(day_totals.items())
        ],
        "dailyActivity": [],
    }


def streamed_twice():
    """One message written twice (a streamed response) plus one single write.

    merged = 100 + 10 = 110, record sum = 200 + 10 = 210. The cache day table
    below agrees with the record sum on the duplicated day and with both numbers
    on the day that has no duplicate.
    """
    return [
        line("m1", "u1", output_tokens=100, ts="2026-05-01T09:00:00Z"),
        line("m1", "u1", output_tokens=100, ts="2026-05-01T09:00:01Z"),
        line("m2", "u2", output_tokens=10, ts="2026-05-02T09:00:00Z"),
    ]


def test_nested_names_which_of_its_two_totals_the_cache_agrees_with(tmp_path):
    write_transcripts(tmp_path, streamed_twice())
    cache = cache_with_day_totals({"2026-05-01": 200, "2026-05-02": 10})

    payload = quotamirror.nested_audit(tmp_path, cache=cache, min_tokens=1)

    arbitration = payload["inflation"]["arbitration"]
    assert arbitration["verdict"] == "record_sum_confirmed"
    assert arbitration["days_compared"] == 2
    assert arbitration["days_matching_record_sum"] == 1
    assert arbitration["cache_tokens"] == 210
    assert arbitration["merged_tokens"] == 110
    assert arbitration["naive_tokens"] == 210


def test_nested_text_does_not_call_the_deduped_total_the_bill(tmp_path):
    """The inflation sentence may print both numbers; it may not pick a winner."""
    write_transcripts(tmp_path, streamed_twice())
    cache = cache_with_day_totals({"2026-05-01": 200, "2026-05-02": 10})

    text = quotamirror.render_nested(quotamirror.nested_audit(tmp_path, cache=cache, min_tokens=1))

    assert "raw record sum" in text
    sentence = text.split("raw record sum", 1)[1].split("\n", 1)[0]
    assert "bill" not in sentence
    # ...and the arbitration is printed, so the reader is not left with a bare ratio.
    assert "record_sum_confirmed" in text


def test_without_a_cache_nested_says_which_total_is_the_bill_is_unsettled(tmp_path):
    write_transcripts(tmp_path, streamed_twice())

    payload = quotamirror.nested_audit(tmp_path)

    arbitration = payload["inflation"]["arbitration"]
    assert arbitration["verdict"] == "undecided"
    assert arbitration["available"] is False
    assert "--cache" in arbitration["reason"]
    text = quotamirror.render_nested(payload)
    assert "unsettled" in text


def test_the_floor_and_tolerance_are_printed_as_used(tmp_path):
    """A number that decides a verdict is printed with the value that was in force."""
    write_transcripts(tmp_path, streamed_twice())
    cache = cache_with_day_totals({"2026-05-01": 200, "2026-05-02": 10})

    payload = quotamirror.nested_audit(tmp_path, cache=cache, min_tokens=7, tolerance=0.5)
    text = quotamirror.render_nested(payload)

    assert payload["inflation"]["arbitration"]["min_tokens"] == 7
    assert payload["inflation"]["arbitration"]["tolerance"] == 0.5
    assert "7 tokens" in text
    assert "50%" in text


def cli_compare(tmp_path, transcripts, cache_day_totals, extra):
    write_rollout = tmp_path / "rollout-2026-05-01T00-00-00.jsonl"
    write_rollout.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                {"timestamp": "2026-05-01T10:00:00Z", "type": "session_meta", "payload": {"session_id": "s1", "timestamp": "2026-05-01T10:00:00Z"}},
                {
                    "timestamp": "2026-05-01T10:00:05Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 100, "input_tokens": 100},
                            "last_token_usage": {"total_tokens": 100, "input_tokens": 100},
                        },
                    },
                },
            ]
        ),
        encoding="utf-8",
    )
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(json.dumps(cache_with_day_totals(cache_day_totals)), encoding="utf-8")
    script = str(Path(quotamirror.__file__).resolve())
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
            "--json",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["claude_day_crosscheck"]


def test_compare_min_tokens_decides_which_days_may_vote(tmp_path):
    transcripts = write_transcripts(tmp_path / "projects", streamed_twice())
    cache_days = {"2026-05-01": 200, "2026-05-02": 10}

    permissive = cli_compare(tmp_path, transcripts, cache_days, ["--min-tokens", "1"])
    strict = cli_compare(tmp_path, transcripts, cache_days, ["--min-tokens", "1000000"])

    assert permissive["min_tokens"] == 1
    assert permissive["days_compared"] == 2
    assert permissive["verdict"] == "record_sum_confirmed"
    assert strict["min_tokens"] == 1_000_000
    assert strict["days_compared"] == 0
    assert strict["days_below_floor"] == 2
    assert strict["verdict"] == "no_overlap"


def test_compare_tolerance_decides_whether_a_day_is_a_match(tmp_path):
    """cache 105 against merged 100 / record sum 200: neither at 2%, merged at 5%."""
    transcripts = write_transcripts(
        tmp_path / "projects",
        [
            line("m1", "u1", output_tokens=100, ts="2026-05-01T09:00:00Z"),
            line("m1", "u1", output_tokens=100, ts="2026-05-01T09:00:01Z"),
        ],
    )
    cache_days = {"2026-05-01": 105}

    strict = cli_compare(tmp_path, transcripts, cache_days, ["--min-tokens", "1"])
    loose = cli_compare(
        tmp_path, transcripts, cache_days, ["--min-tokens", "1", "--tolerance", "0.05"]
    )

    assert strict["tolerance"] == 0.02
    assert strict["days"][0]["closer_to"] == "neither"
    assert strict["verdict"] == "mixed"
    assert loose["tolerance"] == 0.05
    assert loose["days"][0]["closer_to"] == "merged"
    assert loose["verdict"] == "merged_confirmed"


def raw_compare(tmp_path, extra):
    transcripts = write_transcripts(tmp_path / "projects", streamed_twice())
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(
        json.dumps(cache_with_day_totals({"2026-05-01": 200, "2026-05-02": 10})), encoding="utf-8"
    )
    root = tmp_path / "rollouts"
    root.mkdir()
    (root / "rollout-2026-05-01T00-00-00.jsonl").write_text(
        json.dumps({"timestamp": "2026-05-01T10:00:00Z", "type": "session_meta", "payload": {"session_id": "s1", "timestamp": "2026-05-01T10:00:00Z"}})
        + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            str(Path(quotamirror.__file__).resolve()),
            "compare",
            "--codex",
            str(root),
            "--path",
            str(cache_path),
            "--claude-transcripts",
            str(transcripts),
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_a_tolerance_outside_the_unit_interval_is_refused_not_clamped(tmp_path):
    """Silently clamping a deciding argument is the defect this run removes."""
    proc = raw_compare(tmp_path, ["--tolerance", "5"])

    assert proc.returncode == 2
    assert "tolerance" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_a_negative_floor_is_refused_not_clamped(tmp_path):
    proc = raw_compare(tmp_path, ["--min-tokens", "-1"])

    assert proc.returncode == 2
    assert "min_tokens" in proc.stderr
    assert "Traceback" not in proc.stderr
