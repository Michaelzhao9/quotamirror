"""Tests for `compare --poll-share`: how much of usage bought waiting.

Polling and work consume the same metered tokens. These synthetic fixtures test
that each percentage is printed with its token and turn denominators, including
a measured zero at one granularity and a non-zero share at another.

A turn is the span closed by one `token_count` event (that event carries the
turn's own delta). Its tool calls are every `function_call` / `custom_tool_call`
response item seen since the previous `token_count`. A turn is coordination
only when it called at least one tool and EVERY call is a coordination verb.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import quotamirror

COORDINATION_VERBS = ["wait", "wait_agent", "list_agents", "write_stdin", "sleep"]


def meta(ts, session_id="s1"):
    return {"timestamp": ts, "type": "session_meta", "payload": {"session_id": session_id}}


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


def call(ts, name):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "function_call", "name": name, "arguments": "{}"},
    }


def custom_call(ts, name):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "custom_tool_call", "name": name, "input": "ls"},
    }


def write_rollout(tmp_path, lines, name="rollout-2026-01-01T00-00-00.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def share_of(tmp_path, lines):
    write_rollout(tmp_path, lines)
    return quotamirror.poll_share(tmp_path)


def test_a_wait_only_turn_is_charged_to_coordination(tmp_path):
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            custom_call("2026-01-01T10:00:02Z", "exec"),
            event("2026-01-01T10:00:05Z", 200, 200),
            call("2026-01-01T10:01:02Z", "wait"),
            event("2026-01-01T10:01:05Z", 250, 50),
        ],
    )
    assert payload["turns_total"] == 2
    assert payload["coordination_turns"] == 1
    assert payload["tokens_total"] == 250
    assert payload["coordination_tokens"] == 50
    assert payload["share_pct"] == 20.0
    assert payload["verdict"] == "coordination_found"


def test_a_turn_that_also_did_real_work_is_not_coordination(tmp_path):
    """One exec call anywhere in the turn disqualifies it: the tokens cannot be
    split between the wait and the work, so claiming them would be invented."""
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            call("2026-01-01T10:00:02Z", "wait"),
            custom_call("2026-01-01T10:00:03Z", "exec"),
            event("2026-01-01T10:00:05Z", 300, 300),
        ],
    )
    assert payload["turns_total"] == 1
    assert payload["coordination_turns"] == 0
    assert payload["coordination_tokens"] == 0
    assert payload["share_pct"] == 0.0
    assert payload["verdict"] == "no_coordination"


def test_zero_share_prints_the_denominator_it_was_measured_over(tmp_path):
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            custom_call("2026-01-01T10:00:02Z", "exec"),
            event("2026-01-01T10:00:05Z", 100, 100),
            custom_call("2026-01-01T10:01:02Z", "exec"),
            event("2026-01-01T10:01:05Z", 200, 100),
            custom_call("2026-01-01T10:02:02Z", "js"),
            event("2026-01-01T10:02:05Z", 300, 100),
        ],
    )
    text = quotamirror.render_poll_share(payload)
    assert "0.0%" in text
    assert "over 3 turns" in text
    assert "0 of 3" in text


def test_calls_after_the_last_token_event_are_reported_not_dropped(tmp_path):
    write_rollout(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            custom_call("2026-01-01T10:00:02Z", "exec"),
            event("2026-01-01T10:00:05Z", 100, 100),
            call("2026-01-01T10:01:02Z", "wait"),
            call("2026-01-01T10:01:03Z", "wait"),
        ],
    )
    payload = quotamirror.poll_share(tmp_path)
    assert payload["unattributed_calls"] == 2
    assert payload["turns_total"] == 1
    text = quotamirror.render_poll_share(payload)
    assert "2 call(s) arrived after the last token event" in text


@pytest.mark.parametrize("verb", COORDINATION_VERBS)
def test_every_coordination_verb_counts_as_coordination(tmp_path, verb):
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            call("2026-01-01T10:00:02Z", verb),
            event("2026-01-01T10:00:05Z", 40, 40),
        ],
    )
    assert payload["coordination_turns"] == 1
    assert payload["coordination_tokens"] == 40


def test_a_turn_with_no_tool_call_stays_in_the_denominator(tmp_path):
    """Text-only turns are not coordination, but their tokens are still spent, so
    dropping them would inflate the share by shrinking the denominator."""
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            call("2026-01-01T10:00:02Z", "sleep"),
            event("2026-01-01T10:00:05Z", 100, 100),
            event("2026-01-01T10:01:05Z", 300, 200),
        ],
    )
    assert payload["turns_total"] == 2
    assert payload["turns_with_calls"] == 1
    assert payload["tokens_total"] == 300
    assert payload["share_pct"] == 33.3


def test_compare_without_the_flag_is_byte_identical_to_before(tmp_path):
    write_rollout(tmp_path, [meta("2026-01-01T10:00:00Z"), event("2026-01-01T10:00:05Z", 10, 10)])
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(json.dumps({"modelUsage": {"m": {"inputTokens": 4}}, "dailyModelTokens": []}))

    def run(*extra):
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
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    without = run()
    assert "poll_share" not in without
    with_flag = run("--poll-share")
    assert "poll_share" in with_flag
    with_flag.pop("poll_share")
    assert with_flag == without


def test_json_payload_carries_both_numbers_and_the_coverage(tmp_path):
    write_rollout(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            call("2026-01-01T10:00:02Z", "wait"),
            event("2026-01-01T10:00:05Z", 30, 30),
            custom_call("2026-01-01T10:01:02Z", "exec"),
            event("2026-01-01T10:01:05Z", 130, 100),
        ],
    )
    payload = quotamirror.poll_share(tmp_path)
    for key in (
        "turns_total",
        "turns_with_calls",
        "coordination_turns",
        "tokens_total",
        "coordination_tokens",
        "share_pct",
        "tool_calls",
        "sessions",
        "verdict",
    ):
        assert key in payload, key
    assert payload["sessions"] == 1
    assert payload["tool_calls"] == {"exec": 1, "wait": 1}
    text = quotamirror.render_poll_share(payload)
    assert "23.1%" in text
    assert "over 2 turns" in text


def test_an_empty_corpus_refuses_to_report_a_percentage(tmp_path):
    payload = quotamirror.poll_share(tmp_path)
    assert payload["turns_total"] == 0
    assert payload["share_pct"] is None
    assert payload["verdict"] == "no_turns"
    text = quotamirror.render_poll_share(payload)
    assert "%" not in text.split("COORDINATION SPEND")[1].split("RULE")[0]
    assert "no turns" in text


def user_message(ts):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
    }


def test_the_same_corpus_reads_differently_at_user_turn_granularity(tmp_path):
    """Synthetic spans can read differently at request and user-turn granularity.

    Neither reading is wrong; they count different units. Both must be printed
    with their own denominators.
    """
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            user_message("2026-01-01T10:00:01Z"),
            call("2026-01-01T10:00:02Z", "sleep"),
            event("2026-01-01T10:00:05Z", 50, 50),
            custom_call("2026-01-01T10:00:06Z", "exec"),
            event("2026-01-01T10:00:09Z", 250, 200),
        ],
    )
    assert payload["coordination_turns"] == 1          # by request
    assert payload["by_user_turn"]["turns_total"] == 1
    assert payload["by_user_turn"]["coordination_turns"] == 0
    assert payload["by_user_turn"]["share_pct"] == 0.0


def test_both_granularities_are_printed_and_neither_is_called_the_answer(tmp_path):
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            user_message("2026-01-01T10:00:01Z"),
            call("2026-01-01T10:00:02Z", "wait"),
            event("2026-01-01T10:00:05Z", 40, 40),
        ],
    )
    text = quotamirror.render_poll_share(payload)
    assert "by request" in text
    assert "by user turn" in text
    assert "100.0%" in text


def test_merging_request_turns_into_user_turns_conserves_tokens(tmp_path):
    payload = share_of(
        tmp_path,
        [
            meta("2026-01-01T10:00:00Z"),
            user_message("2026-01-01T10:00:01Z"),
            custom_call("2026-01-01T10:00:02Z", "exec"),
            event("2026-01-01T10:00:05Z", 100, 100),
            custom_call("2026-01-01T10:01:02Z", "exec"),
            event("2026-01-01T10:01:05Z", 300, 200),
            user_message("2026-01-01T10:02:00Z"),
            call("2026-01-01T10:02:02Z", "sleep"),
            event("2026-01-01T10:02:05Z", 350, 50),
        ],
    )
    assert payload["tokens_total"] == 350
    assert payload["by_user_turn"]["tokens_total"] == 350
    assert payload["by_user_turn"]["turns_total"] == 2
    assert payload["by_user_turn"]["coordination_turns"] == 1
