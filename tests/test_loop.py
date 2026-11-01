"""Tests for `quotamirror loop`: repeat detection over session transcripts.

Every fixture is synthetic. The contract under test is the one the README
promises: the payload identifies repeated tool calls by name and hash only,
never by the text the agent passed in.
"""

import json
from pathlib import Path

from quotamirror import loop_audit, render_loop

SENTINEL = "SECRET-PROMPT-TEXT-9f3a"


def tool_use(name, inp, block_id="toolu_1", uuid="u1", sidechain=False):
    return {
        "type": "assistant",
        "sessionId": "s1",
        "uuid": uuid,
        "isSidechain": sidechain,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": block_id, "name": name, "input": inp}],
        },
    }


def write_transcript(root, name="s1.jsonl", records=()):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")
    return path


def test_counts_only_transcript_tool_use_not_request_body_copies(tmp_path):
    records = [
        tool_use("Bash", {"command": "ls"}, block_id="toolu_a"),
        {
            "type": "api-request-blob",
            "sessionId": "s1",
            "uuid": "u9",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {"command": "ls"}}
                ]
            },
        },
    ]
    write_transcript(tmp_path, records=records)

    payload = loop_audit(tmp_path)

    assert payload["calls"] == 1
    assert payload["double_count"]["skipped_tool_use"] == 1


def test_same_block_stored_in_two_files_is_counted_once(tmp_path):
    write_transcript(tmp_path, "a.jsonl", [tool_use("Read", {"file_path": "/x"}, block_id="toolu_a")])
    write_transcript(tmp_path, "b.jsonl", [tool_use("Read", {"file_path": "/x"}, block_id="toolu_a")])

    payload = loop_audit(tmp_path)

    assert payload["calls"] == 1
    assert payload["repeat_calls"] == 0


def test_payload_never_carries_tool_input_text(tmp_path):
    write_transcript(
        tmp_path,
        records=[
            tool_use("Bash", {"command": f"echo {SENTINEL}"}, block_id="toolu_a", uuid="u1"),
            tool_use("Bash", {"command": f"echo {SENTINEL}"}, block_id="toolu_b", uuid="u2"),
        ],
    )

    payload = loop_audit(tmp_path)
    rendered = render_loop(payload)

    assert SENTINEL not in json.dumps(payload)
    assert SENTINEL not in rendered
    assert payload["repeated"][0]["hash"]
    assert len(payload["repeated"][0]["hash"]) == 12


def test_block_without_id_falls_back_to_record_uuid(tmp_path):
    rec = tool_use("Grep", {"pattern": "x"}, uuid="rec-uuid-1")
    del rec["message"]["content"][0]["id"]
    write_transcript(tmp_path, "a.jsonl", [rec])
    write_transcript(tmp_path, "b.jsonl", [rec])

    payload = loop_audit(tmp_path)

    assert payload["calls"] == 1


def test_empty_directory_reports_absent_rate_not_zero(tmp_path):
    payload = loop_audit(tmp_path)

    assert payload["calls"] == 0
    assert payload["repeat_rate"] is None
    assert payload["verdict"] == "no_data"


def test_repeat_across_files_of_one_session_is_one_repeat(tmp_path):
    write_transcript(tmp_path, "a.jsonl", [tool_use("Edit", {"k": "v"}, block_id="toolu_a", uuid="u1")])
    write_transcript(tmp_path, "b.jsonl", [tool_use("Edit", {"k": "v"}, block_id="toolu_b", uuid="u2")])

    payload = loop_audit(tmp_path)

    assert payload["calls"] == 2
    assert payload["repeat_calls"] == 1
    assert payload["longest_consecutive_run"] == 1
    assert payload["consecutive_repeat_calls"] == 0


def test_three_identical_calls_in_a_row_are_a_retry_run(tmp_path):
    write_transcript(
        tmp_path,
        records=[
            tool_use("Bash", {"command": "make"}, block_id="toolu_a", uuid="u1"),
            tool_use("Bash", {"command": "make"}, block_id="toolu_b", uuid="u2"),
            tool_use("Bash", {"command": "make"}, block_id="toolu_c", uuid="u3"),
            tool_use("Read", {"file_path": "/y"}, block_id="toolu_d", uuid="u4"),
        ],
    )

    payload = loop_audit(tmp_path)

    assert payload["longest_consecutive_run"] == 3
    assert payload["consecutive_repeat_calls"] == 2
    assert payload["repeat_calls"] == 2


def test_repeated_entries_count_extra_calls_per_tool(tmp_path):
    write_transcript(
        tmp_path,
        records=[
            tool_use("Bash", {"command": "a"}, block_id="t1", uuid="u1"),
            tool_use("Bash", {"command": "a"}, block_id="t2", uuid="u2"),
            tool_use("Bash", {"command": "a"}, block_id="t3", uuid="u3"),
            tool_use("Read", {"file_path": "z"}, block_id="t4", uuid="u4"),
            tool_use("Read", {"file_path": "z"}, block_id="t5", uuid="u5"),
        ],
    )

    payload = loop_audit(tmp_path)
    top = payload["repeated"][0]

    assert top["tool"] == "Bash"
    assert top["calls"] == 3
    assert top["extra_calls"] == 2
    assert payload["by_tool"][0] == {"tool": "Bash", "extra_calls": 2}


def test_malformed_lines_are_skipped_and_counted(tmp_path):
    path = write_transcript(tmp_path, "a.jsonl", [tool_use("Read", {"p": 1}, block_id="t1")])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json}\n\n")

    payload = loop_audit(tmp_path)

    assert payload["calls"] == 1
    assert payload["skipped_lines"] == 1


def test_no_repeats_is_reported_as_no_loop_evidence(tmp_path):
    write_transcript(
        tmp_path,
        records=[
            tool_use("Read", {"p": 1}, block_id="t1", uuid="u1"),
            tool_use("Read", {"p": 2}, block_id="t2", uuid="u2"),
        ],
    )

    payload = loop_audit(tmp_path)
    rendered = render_loop(payload)

    assert payload["repeat_rate"] == 0.0
    assert payload["verdict"] == "no_repeat_evidence"
    assert "no repeated tool call" in rendered.lower()


def test_sidechain_records_are_counted_in_their_own_stream(tmp_path):
    write_transcript(
        tmp_path,
        records=[
            tool_use("Read", {"p": 1}, block_id="main-1", uuid="u1"),
            tool_use("Read", {"p": 2}, block_id="side-1", uuid="u2", sidechain=True),
        ],
    )

    payload = loop_audit(tmp_path)

    assert payload["calls"] == 2
    assert payload["sidechain_calls"] == 1
    assert payload["repeat_calls"] == 0


def test_render_states_that_only_identical_arguments_count(tmp_path):
    write_transcript(
        tmp_path,
        records=[
            tool_use("Read", {"p": 1}, block_id="t1", uuid="u1"),
            tool_use("Read", {"p": 2}, block_id="t2", uuid="u2"),
        ],
    )

    rendered = render_loop(loop_audit(tmp_path))

    assert "identical arguments" in rendered.lower()
