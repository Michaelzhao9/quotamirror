"""A reading can only be compared if a second one is in hand.

`quiet` is read-only by design, so it never stores a reading. That left the
tool with two digests and no verdict: a reader who saw `digest` move could not
learn from the tool whether the corpus had been edited or merely touched, even
though the pair of digests already answers it once another reading exists.

So the caller may hand one back in (`--compare-reading`, a `quiet --json`
payload or its reading block). This file pins the five differences the pair can
be told apart on, the refusals (a block that is not a reading, a reading from a
version with no byte axis, two blocks that contradict each other), and the rule
that no comparison prints as agreement. All fixtures are synthetic.
"""

import json

import pytest

import quotamirror


def cache_with(rows, messages=10):
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


def corpus(tmp_path, lines, name="a.jsonl"):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(line) + "\n" for line in lines)
    (root / name).write_text(body, encoding="utf-8")
    return root


def reading_of(root, cache=None):
    scan = quotamirror.scan_usage(root)
    payload = quotamirror.quiet_days(
        cache if cache is not None else cache_with([("2026-05-04", 600)]), scan, min_tokens=0
    )
    return payload["arbitration"]["reading"]


def prior_of(root, cache=None):
    """The same block the way a later run would receive it from JSON."""
    return quotamirror._prior_reading(json.loads(json.dumps(reading_of(root, cache))))


def compared(root, prior, cache=None):
    scan = quotamirror.scan_usage(root)
    payload = quotamirror.quiet_days(
        cache if cache is not None else cache_with([("2026-05-04", 600)]),
        scan,
        min_tokens=0,
        prior=prior,
    )
    return payload["reading_compare"]


def test_a_reading_compared_with_itself_is_the_same_reading(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    block = compared(root, prior_of(root))
    assert block["available"] is True
    assert block["verdict"] == "same_reading"
    assert block["content_equal"] is True
    assert block["path_equal"] is True
    assert block["files_delta"] == 0
    assert block["bytes_delta"] == 0
    assert block["records_delta"] == 0


def test_a_touched_file_is_metadata_only_not_a_changed_corpus(tmp_path):
    import os

    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    target = root / "a.jsonl"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns + 3_600_000_000_000, stat.st_mtime_ns + 3_600_000_000_000))
    block = compared(root, prior)
    assert block["verdict"] == "metadata_only"
    assert block["content_equal"] is True
    assert block["path_equal"] is False
    assert block["files_delta"] == 0
    assert block["bytes_delta"] == 0


def test_a_same_size_edit_with_the_mtime_restored_is_a_byte_difference(tmp_path):
    import os

    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    target = root / "a.jsonl"
    stat = target.stat()
    body = target.read_text(encoding="utf-8")
    target.write_text(body.replace('"input_tokens": 600', '"input_tokens": 900'), encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    block = compared(root, prior)
    assert block["verdict"] == "bytes_differ"
    assert block["content_equal"] is False
    assert block["path_equal"] is True
    assert block["files_delta"] == 0
    assert block["bytes_delta"] == 0


def test_a_removed_file_is_a_file_set_difference(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    (root / "b.jsonl").write_text(
        json.dumps(usage_line("m2", "2026-05-04T11:00:00Z", 10)) + "\n", encoding="utf-8"
    )
    prior = prior_of(root)
    (root / "b.jsonl").unlink()
    block = compared(root, prior)
    assert block["verdict"] == "file_set_differs"
    assert block["files_delta"] == -1
    assert block["content_equal"] is False


def test_a_grown_file_is_a_size_difference(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    (root / "a.jsonl").write_text(
        (root / "a.jsonl").read_text(encoding="utf-8")
        + json.dumps(usage_line("m2", "2026-05-04T11:00:00Z", 20))
        + "\n",
        encoding="utf-8",
    )
    block = compared(root, prior)
    assert block["verdict"] == "size_differs"
    assert block["files_delta"] == 0
    assert block["bytes_delta"] > 0


def test_a_prior_reading_without_a_byte_axis_is_not_comparable(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    prior["content_digest"] = None
    block = compared(root, prior)
    assert block["available"] is False
    assert block["verdict"] == "not_comparable"
    assert "content_digest" in block["reason"]


def test_no_supplied_reading_prints_as_not_measured():
    block = quotamirror.compare_readings({"available": True}, None)
    assert block["available"] is False
    assert block["verdict"] == "no_prior"
    assert "--compare-reading" in block["reason"]


def test_a_run_without_a_reading_cannot_be_compared():
    block = quotamirror.compare_readings({"available": False}, {"content_digest": "abc"})
    assert block["verdict"] == "no_current_reading"


def test_two_blocks_that_contradict_each_other_are_not_compared(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    prior["records"] = prior["records"] + 1  # same bytes, one more record: a different rule
    block = compared(root, prior)
    assert block["verdict"] == "not_comparable"
    assert "record" in block["reason"]


def test_a_document_that_is_not_a_reading_is_refused():
    with pytest.raises(ValueError):
        quotamirror._prior_reading({"hello": "world"})
    with pytest.raises(ValueError):
        quotamirror._prior_reading([1, 2, 3])


def test_a_saved_payload_is_accepted_as_a_prior_reading(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    scan = quotamirror.scan_usage(root)
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 600)]), scan, min_tokens=0)
    saved = tmp_path / "payload.json"
    saved.write_text(json.dumps(payload), encoding="utf-8")
    prior = quotamirror.read_prior_reading(saved)
    assert prior["content_digest"] == scan["content_digest"]
    assert quotamirror.compare_readings(payload["arbitration"]["reading"], prior)["verdict"] == "same_reading"


def test_the_compare_block_prints_its_numbers_without_overflowing(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    block = compared(root, prior_of(root))
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]),
        quotamirror.scan_usage(root),
        min_tokens=0,
        prior=prior_of(root),
    )
    text = quotamirror.render_quiet(payload)
    assert block["verdict"] == "same_reading"
    assert "same_reading" in text
    assert "files +0" in text
    compare_lines = [line for line in text.splitlines() if line.strip().startswith(("prior ", "change ", "compare ")) or "same_reading" in line]
    assert compare_lines
    assert max(len(line) for line in compare_lines) <= 80


def test_the_absent_comparison_is_printed_on_the_quiet_screen(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]), quotamirror.scan_usage(root), min_tokens=0
    )
    text = quotamirror.render_quiet(payload)
    assert "not measured" in text
    assert "--compare-reading" in text


def test_the_cli_refuses_a_file_that_is_not_a_reading(tmp_path, capsys):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(cache_with([("2026-05-04", 600)])), encoding="utf-8")
    base = ["quiet", "--path", str(cache_path), "--transcripts", str(root)]

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert quotamirror.main(base + ["--compare-reading", str(broken)]) == 2
    assert "not JSON" in capsys.readouterr().err

    other = tmp_path / "other.json"
    other.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert quotamirror.main(base + ["--compare-reading", str(other)]) == 2
    assert "no reading" in capsys.readouterr().err

    assert quotamirror.main(base + ["--compare-reading", str(tmp_path / "absent.json")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_the_cli_compares_against_a_saved_payload(tmp_path, capsys, monkeypatch):
    import os

    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(cache_with([("2026-05-04", 600)])), encoding="utf-8")
    saved = tmp_path / "saved.json"
    base = ["quiet", "--path", str(cache_path), "--transcripts", str(root)]

    assert quotamirror.main(base + ["--json"]) == 0
    saved.write_text(capsys.readouterr().out, encoding="utf-8")

    assert quotamirror.main(base + ["--compare-reading", str(saved)]) == 0
    assert "same_reading" in capsys.readouterr().out

    target = root / "a.jsonl"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_200_000_000_000))
    assert quotamirror.main(base + ["--compare-reading", str(saved)]) == 0
    assert "metadata_only" in capsys.readouterr().out
