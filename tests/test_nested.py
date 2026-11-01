"""Tests for `nested`: a total must say what it includes and how it was counted.

Vendor reports show both error directions: descendant work can be dropped, and
forked cumulative counters can be counted twice. Both are the same defect: a
total omitted its scope and accounting basis. The fixtures below are synthetic.

A second axis is repeated writes of the same message id. The payload declares
its dedupe rule and prints the naive record sum beside the merged total.
"""

import json
import subprocess
import sys
from pathlib import Path

import quotamirror


def line(
    message_id,
    uuid,
    sidechain=False,
    input_tokens=0,
    output_tokens=0,
    cache_creation=0,
    cache_read=0,
    text="hello",
    ts=None,
):
    record = {
        "type": "assistant",
        "uuid": uuid,
        "isSidechain": sidechain,
        "sessionId": "s1",
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if ts is not None:
        record["timestamp"] = ts
    return record


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def test_every_total_declares_what_it_includes_and_how_it_was_accounted(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("msg_root", "u1", output_tokens=10),
            line("msg_side", "u2", sidechain=True, output_tokens=5),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    for scope, total in payload["totals"].items():
        assert total["includes_descendants"], scope
        assert total["accounting"] == "delta", scope
    assert payload["totals"]["all"]["includes_descendants"] == "root+sidechain"
    assert payload["totals"]["root"]["includes_descendants"] == "root"
    assert payload["totals"]["sidechain"]["includes_descendants"] == "sidechain"


def test_a_message_written_twice_in_one_file_is_counted_once(tmp_path):
    duplicate = dict(input_tokens=3, output_tokens=7, cache_read=100)
    write(
        tmp_path / "a.jsonl",
        [
            line("msg_dup", "u1", **duplicate),
            line("msg_dup", "u2", **duplicate),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["records_raw"] == 2
    assert payload["records_deduped"] == 1
    assert payload["totals"]["all"]["total_tokens"] == 110


def test_a_duplicate_with_more_content_keeps_the_larger_usage(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("msg_part", "u1", output_tokens=31, cache_read=102483),
            line("msg_part", "u2", output_tokens=1134, cache_read=102483),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["totals"]["all"]["tokens"]["output_tokens"] == 1134
    assert payload["totals"]["all"]["tokens"]["cache_read_input_tokens"] == 102483


def test_a_message_seen_in_both_scopes_counts_as_root_and_the_conflict_is_named(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [line("msg_both", "u1", output_tokens=10)],
    )
    write(
        tmp_path / "agent.jsonl",
        [line("msg_both", "u2", sidechain=True, output_tokens=10)],
    )

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["totals"]["all"]["messages"] == 1
    assert payload["totals"]["root"]["messages"] == 1
    assert payload["totals"]["sidechain"]["messages"] == 0
    assert payload["scope_conflict"] == 1


def test_records_without_a_message_id_are_not_merged(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line(None, "u1", output_tokens=10),
            line(None, "u2", output_tokens=10),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["records_deduped"] == 2
    assert payload["totals"]["all"]["total_tokens"] == 20


def test_inflation_names_both_sides_of_the_raw_sum(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("msg_x", "u1", output_tokens=50),
            line("msg_x", "u2", output_tokens=50),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["inflation"]["raw_tokens"] == 100
    assert payload["inflation"]["deduped_tokens"] == 50
    assert payload["inflation"]["pct"] == 100.0


def test_sidechain_share_carries_its_denominator(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("r1", "u1", output_tokens=30, input_tokens=10),
            line("s1", "u2", sidechain=True, output_tokens=60, input_tokens=10),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    share = payload["share"]
    assert share["available"] is True
    assert share["basis"] == "non_cache_read_tokens"
    assert share["numerator"] == 70
    assert share["denominator"] == 110
    assert share["pct"] == round(100 * 70 / 110, 1)
    assert share["sidechain_messages"] == 1
    assert share["messages"] == 2
    assert share["messages_pct"] == 50.0
    assert payload["verdict"] == "nested_confirmed"


def test_a_corpus_without_sidechain_prints_zero_over_n_messages(tmp_path):
    write(tmp_path / "a.jsonl", [line("r1", "u1", output_tokens=25)])

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["share"]["pct"] == 0.0
    assert payload["share"]["messages"] == 1
    assert payload["share"]["sidechain_messages"] == 0
    assert payload["verdict"] == "no_sidechain"


def test_a_corpus_with_no_usage_refuses_the_share(tmp_path):
    write(tmp_path / "a.jsonl", [{"type": "user", "uuid": "u1", "message": {"role": "user"}}])

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["share"]["available"] is False
    assert payload["share"]["pct"] is None
    assert payload["share"]["reason"]
    assert payload["verdict"] == "no_usage"


def test_the_cache_read_column_is_reported_apart_from_the_share_basis(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("r1", "u1", output_tokens=5, cache_read=995),
            line("s1", "u2", sidechain=True, output_tokens=5),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    cache = payload["cache_read"]
    assert cache["tokens"] == 995
    assert cache["share_of_total_pct"] == 99.0  # 995 of 1005
    # the share denominator excludes it, and says so
    assert payload["share"]["basis"] == "non_cache_read_tokens"
    assert payload["share"]["denominator"] == 10
    assert any("cache" in note.lower() for note in payload["notes"])


def test_payload_carries_no_message_text(tmp_path):
    secret = "USERPRIVATEMARKER"
    write(tmp_path / "a.jsonl", [line("r1", "u1", output_tokens=5, text=secret)])

    rendered = json.dumps(quotamirror.nested_audit(tmp_path))

    assert secret not in rendered


def test_text_rendering_states_the_stamp_and_the_denominator(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("r1", "u1", output_tokens=30),
            line("s1", "u2", sidechain=True, output_tokens=60),
        ],
    )

    text = quotamirror.render_nested(quotamirror.nested_audit(tmp_path))

    assert "includes_descendants" in text
    assert "accounting" in text
    assert "2 messages" in text
    assert "66.7%" in text


def test_cli_nested_prints_a_receipt(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [line("r1", "u1", output_tokens=30), line("s1", "u2", sidechain=True, output_tokens=60)],
    )
    proc = subprocess.run(
        [sys.executable, str(Path(quotamirror.__file__)), "nested", "--path", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "includes_descendants" in proc.stdout


def test_cli_nested_json_is_machine_readable(tmp_path):
    write(tmp_path / "a.jsonl", [line("r1", "u1", output_tokens=30)])
    proc = subprocess.run(
        [sys.executable, str(Path(quotamirror.__file__)), "nested", "--path", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["totals"]["all"]["total_tokens"] == 30


def test_cli_nested_on_a_missing_directory_exits_2(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(Path(quotamirror.__file__)), "nested", "--path", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "no transcripts" in proc.stderr


def test_a_single_message_is_not_printed_as_a_plural(tmp_path):
    write(tmp_path / "a.jsonl", [line("r1", "u1", output_tokens=30)])

    text = quotamirror.render_nested(quotamirror.nested_audit(tmp_path))

    assert "1 message " in text or "1 message\n" in text
    assert "1 messages" not in text


# A window states how it was obtained, not only which days it covers. `nested`
# measures record timestamps; `compare` reads a cache-declared day table.

WINDOW_BASES = {"record_timestamp", "declared_attribution", "none"}


def test_every_total_carries_a_window_measured_from_record_timestamps(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("m1", "u1", output_tokens=10, ts="2026-03-01T09:00:00Z"),
            line("m2", "u2", output_tokens=10, ts="2026-03-01T18:00:00Z"),
            line("m3", "u3", sidechain=True, output_tokens=10, ts="2026-03-04T11:00:00Z"),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    for scope in ("all", "root", "sidechain"):
        window = payload["totals"][scope]["window"]
        assert window["basis"] in WINDOW_BASES, scope
        assert window["basis"] == "record_timestamp", scope
    assert payload["totals"]["all"]["window"] == {
        "first": "2026-03-01",
        "last": "2026-03-04",
        "days": 2,
        "basis": "record_timestamp",
    }
    assert payload["totals"]["sidechain"]["window"]["first"] == "2026-03-04"
    assert payload["totals"]["sidechain"]["window"]["days"] == 1


def test_records_without_a_timestamp_are_counted_so_the_edges_are_not_overclaimed(tmp_path):
    """A window measured from the timestamps present must admit how many records
    carried none: those could sit outside the printed range."""
    write(
        tmp_path / "a.jsonl",
        [
            line("m1", "u1", output_tokens=10, ts="2026-03-01T09:00:00Z"),
            line("m2", "u2", output_tokens=10),
        ],
    )

    payload = quotamirror.nested_audit(tmp_path)

    assert payload["coverage"]["records_without_timestamp"] == 1
    assert payload["coverage"]["records_timed"] == 1
    assert "no timestamp" in payload["coverage"]["note"]
    assert payload["totals"]["all"]["window"]["first"] == "2026-03-01"
    assert payload["totals"]["all"]["window"]["last"] == "2026-03-01"


def test_nested_refuses_a_window_it_cannot_measure(tmp_path):
    write(tmp_path / "a.jsonl", [line("m1", "u1", output_tokens=10)])
    payload = quotamirror.nested_audit(tmp_path)
    assert payload["totals"]["all"]["window"]["first"] is None
    assert payload["totals"]["all"]["window"]["basis"] == "record_timestamp"


def cache_with_days(days):
    return {
        "modelUsage": {"m": {"inputTokens": 5}},
        "dailyModelTokens": [
            {"date": day, "tokensByModel": {"m": 10}} for day in days
        ],
        "dailyActivity": [],
    }


def test_the_text_names_the_second_window_the_other_command_prints(tmp_path):
    """The pair must be explained by one of the two commands. A reader of
    `nested` is told that `compare` prints a different window for the same
    quantity, and that the two must not be subtracted."""
    write(
        tmp_path / "a.jsonl",
        [
            line("m1", "u1", output_tokens=10, ts="2026-03-01T09:00:00Z"),
            line("m2", "u2", output_tokens=10, ts="2026-03-07T09:00:00Z"),
        ],
    )
    payload = quotamirror.nested_audit(
        tmp_path, cache=cache_with_days(["2026-03-01", "2026-03-02"])
    )

    windows = payload["windows"]
    assert windows["transcript"]["basis"] == "record_timestamp"
    assert windows["cache"]["basis"] == "declared_attribution"
    assert windows["comparability"]["same_window"] is False
    assert windows["days_transcript_only"] == ["2026-03-07"]

    text = quotamirror.render_nested(payload)
    assert "compare" in text
    assert "2026-03-01..2026-03-07" in text
    assert "2026-03-01..2026-03-02" in text
    assert "must not be subtracted" in text
    assert "2026-03-07" in text


def test_when_both_windows_agree_the_text_says_so_without_the_warning(tmp_path):
    write(tmp_path / "a.jsonl", [line("m1", "u1", output_tokens=10, ts="2026-03-01T09:00:00Z")])
    payload = quotamirror.nested_audit(tmp_path, cache=cache_with_days(["2026-03-01"]))
    text = quotamirror.render_nested(payload)
    assert payload["windows"]["comparability"]["same_window"] is True
    assert "both totals cover 2026-03-01..2026-03-01" in text
    assert "must not be subtracted" not in text


def test_without_a_cache_the_window_section_admits_it_has_only_one_side(tmp_path):
    write(tmp_path / "a.jsonl", [line("m1", "u1", output_tokens=10, ts="2026-03-01T09:00:00Z")])
    payload = quotamirror.nested_audit(tmp_path)
    text = quotamirror.render_nested(payload)
    assert payload["windows"]["cache"] is None
    assert payload["windows"]["comparability"]["same_window"] == "unknown"
    assert "not read" in text or "unreadable" in text


def test_cli_nested_reads_the_cache_when_given_one(tmp_path):
    write(tmp_path / "a.jsonl", [line("m1", "u1", output_tokens=10, ts="2026-03-01T09:00:00Z")])
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(cache_with_days(["2026-03-01", "2026-02-25"])), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(quotamirror.__file__)),
            "nested",
            "--path",
            str(tmp_path),
            "--cache",
            str(cache_path),
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["windows"]["cache"]["first"] == "2026-02-25"
    assert payload["windows"]["days_cache_only"] == ["2026-02-25"]


def test_render_names_the_day_where_repeated_writes_inflated_the_record_sum_most(tmp_path):
    write(
        tmp_path / "a.jsonl",
        [
            line("m1", "u1", output_tokens=100, ts="2026-05-01T09:00:00Z"),
            line("m1", "u1", output_tokens=100, ts="2026-05-01T09:00:01Z"),
            line("m2", "u2", output_tokens=10, ts="2026-05-02T09:00:00Z"),
        ],
    )

    text = quotamirror.render_nested(quotamirror.nested_audit(tmp_path))

    # A total that says "raw sum is 90% higher" without naming where the repeats
    # are leaves the reader unable to find them.
    assert "2026-05-01" in text
    assert "100 tokens" in text
    assert "210" in text


# --- Cycle: zero is not a measurement ---------------------------------------
# An empty directory and a measured absence both used to print "no_usage". The
# coverage sentence then claimed "the printed window bounds all records read"
# over a scan that read nothing: a vacuous claim in the product that exists to
# refuse vacuous claims. A reader has to be able to tell "there was nothing to
# read" from "I read it and it was empty".


def test_an_empty_transcript_dir_is_an_empty_store_not_a_measured_no_usage(tmp_path):
    payload = quotamirror.nested_audit(tmp_path)

    assert payload["observation"]["verdict"] == "store_empty"
    assert payload["observation"]["files"] == 0
    assert payload["observation"]["usage_records"] == 0


def test_the_coverage_note_does_not_claim_a_window_it_never_measured(tmp_path):
    payload = quotamirror.nested_audit(tmp_path)

    note = payload["coverage"]["note"]
    assert "bounds all records read" not in note
    assert "no usage record was read" in note


def test_files_with_no_usage_block_are_a_measured_absence_not_an_empty_store(tmp_path):
    write(tmp_path / "a.jsonl", [{"type": "user", "uuid": "u1", "message": {"role": "user"}}])

    payload = quotamirror.nested_audit(tmp_path)

    # One file was read and it held no usage. That is a different fact from
    # "there was nothing to read", and it is the one a reader can act on.
    assert payload["observation"]["verdict"] == "no_usage_records"
    assert payload["observation"]["files"] == 1
    assert payload["observation"]["usage_records"] == 0


def test_the_rendered_receipt_prints_which_kind_of_zero_it_is(tmp_path):
    text = quotamirror.render_nested(quotamirror.nested_audit(tmp_path))

    observation = [row for row in text.splitlines() if row.strip().startswith("observation")]

    assert len(observation) == 1
    assert "store_empty" in observation[0]
    assert "bounds all records read" not in text


def test_cli_nested_on_an_empty_directory_prints_the_observation(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(Path(quotamirror.__file__)), "nested", "--path", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "store_empty" in proc.stdout
    assert "bounds all records read" not in proc.stdout


def test_an_empty_store_refuses_to_issue_a_usage_verdict(tmp_path):
    # "no_usage" is a claim about usage. Over a directory holding no transcript
    # there is nothing to have made that claim from.
    payload = quotamirror.nested_audit(tmp_path)

    assert payload["verdict"] == "unobserved"
