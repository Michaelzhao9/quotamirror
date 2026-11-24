"""Tests for the meter check's reading inventory: which corpus did the verdict read?

A witness or replay verdict is a statement about a *reading* - this set of files,
this many records, at this moment. Two runs can then print two different
witness splits with no way to tell "the corpus changed" from "the tool read a
different corpus". That gap was caught in the r306 build: one day's record sum
fell by 1.2B tokens between two runs of the same code over the same window, and
neither payload carried an inventory, so the cause was undecidable.

The reading block therefore states the reading itself: files actually opened,
files the enumeration could not open, records parsed, bytes read, a digest over
(name, size, mtime) of the corpus, and - per witnessed day - how many files and
records stand behind that day's verdict.

All fixtures are synthetic.
"""

import json

import quotamirror


def cache_with(rows, messages=10):
    """rows: [(date, tokens)]"""
    return {
        "version": 5,
        "dailyActivity": [
            {"date": date, "messageCount": messages, "sessionCount": 1, "toolCallCount": 0}
            for date, _ in rows
        ],
        "dailyModelTokensVersion": 5,
        "dailyModelTokens": [
            {"date": date, "tokensByModel": {"claude-test": tokens}} for date, tokens in rows
        ],
    }


def usage_line(message_id, ts, input_tokens, model="claude-test"):
    return {
        "type": "assistant",
        "uuid": f"u-{message_id}",
        "isSidechain": False,
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def write_file(root, name, lines):
    path = root / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def scan_of(tmp_path, lines):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    write_file(root, "a.jsonl", lines)
    return quotamirror.scan_usage(root)


def test_scan_states_how_many_files_it_actually_read(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    first = write_file(root, "a.jsonl", [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    second = write_file(root, "b.jsonl", [usage_line("m2", "2026-05-05T10:00:00Z", 2000)])
    scan = quotamirror.scan_usage(root)
    assert scan["files_read"] == 2
    assert scan["files_skipped"] == 0
    assert scan["bytes_read"] == first.stat().st_size + second.stat().st_size
    assert len(scan["reading_digest"]) == 12


def test_reading_digest_is_stable_for_an_unchanged_corpus(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    again = quotamirror.scan_usage(scan["root"])
    assert again["reading_digest"] == scan["reading_digest"]


def test_reading_digest_changes_when_the_corpus_gains_a_record(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    root = scan["root"]
    write_file(
        __import__("pathlib").Path(root), "b.jsonl", [usage_line("m2", "2026-05-05T10:00:00Z", 5)]
    )
    after = quotamirror.scan_usage(root)
    assert after["reading_digest"] != scan["reading_digest"]


def test_reading_block_names_the_files_behind_a_witness_day(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    write_file(root, "a.jsonl", [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    write_file(root, "b.jsonl", [usage_line("m2", "2026-05-04T11:00:00Z", 400)])
    scan = quotamirror.scan_usage(root)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    reading = result["arbitration"]["reading"]
    assert reading["available"] is True
    assert reading["files_read"] == 2
    assert reading["records_parsed"] == 2
    assert reading["messages_parsed"] == 2
    assert reading["digest"] == scan["reading_digest"]
    assert reading["per_day"]["2026-05-04"] == {"files": 2, "records": 2}


def test_per_day_keeps_a_file_under_a_day_the_day_table_attributes_elsewhere(tmp_path):
    """A message streamed across midnight is billed on its first day, but the
    file holding the second write still belongs to the second day's inventory."""
    root = tmp_path / "transcripts"
    root.mkdir()
    write_file(
        root,
        "a.jsonl",
        [
            usage_line("m1", "2026-05-04T23:50:00Z", 500),
            usage_line("m1", "2026-05-05T00:10:00Z", 500),
        ],
    )
    scan = quotamirror.scan_usage(root)
    cache = cache_with([("2026-05-04", 500), ("2026-05-05", 100)])
    result = quotamirror.quiet_days(cache, scan, min_tokens=0)
    reading = result["arbitration"]["reading"]
    assert reading["records_parsed"] == 2
    assert reading["per_day"]["2026-05-04"] == {"files": 1, "records": 2}
    assert reading["per_day"]["2026-05-05"] == {"files": 1, "records": 0}


def test_reading_block_is_unavailable_when_no_scan_was_taken(tmp_path):
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), None, min_tokens=0)
    reading = result["arbitration"]["reading"]
    assert reading["available"] is False
    assert reading["per_day"] == {}
    assert "no transcript" in reading["reason"]


def test_quiet_text_prints_the_reading_fingerprint(tmp_path):
    scan = scan_of(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), scan, min_tokens=0)
    text = quotamirror.render_quiet(result)
    assert "reading" in text
    assert scan["reading_digest"] in text


def test_text_says_the_reading_was_not_identified_when_no_scan_was_taken(tmp_path):
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 1000)]), None, min_tokens=0)
    text = quotamirror.render_quiet(result)
    assert "reading not identified" in text
    assert "no transcript scan" in text
