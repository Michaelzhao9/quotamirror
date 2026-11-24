"""A moved digest can be counted; only a manifest can name the file.

The comparison added in 0.49.0 tells five kinds of difference apart, and
`metadata_only` says "no byte moved, only (name, size, mtime) did". That is a
verdict about a corpus with no subject: a reader who wants to know *which*
file was touched had to diff the tree themselves. A digest is a one-way
summary, so the only way to name a file is to carry the per-file facts along
with the reading - opt-in (`--manifest`), because most runs do not need it and
the payload should stay small.

This file pins the naming, the cap on how many names are printed (the count
is never capped), and every reason the tool refuses to name anything: no
manifest on either side, a manifest capped at the file limit, and a manifest
that disagrees with its own file count.

All fixtures are synthetic.
"""

import json
import os

import pytest

import quotamirror


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


def corpus(tmp_path, lines, name="a.jsonl"):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return root


def add_file(root, name, message_id, tokens=10, hour=11):
    (root / name).write_text(
        json.dumps(usage_line(message_id, f"2026-05-04T{hour:02d}:00:00Z", tokens)) + "\n",
        encoding="utf-8",
    )
    return root / name


def reading_of(root, manifest=False):
    scan = quotamirror.scan_usage(root, manifest=manifest)
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 600)]), scan, min_tokens=0)
    return payload["arbitration"]["reading"]


def prior_of(root, manifest=False):
    """The block the way a later run receives it: through JSON and back."""
    return quotamirror._prior_reading(json.loads(json.dumps(reading_of(root, manifest))))


def compared(root, prior, manifest=False):
    scan = quotamirror.scan_usage(root, manifest=manifest)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]), scan, min_tokens=0, prior=prior
    )
    return payload["reading_compare"]


def tap(target, seconds=3600):
    stat = target.stat()
    os.utime(
        target,
        ns=(stat.st_atime_ns + seconds * 10**9, stat.st_mtime_ns + seconds * 10**9),
    )


def test_a_scan_holds_no_manifest_unless_it_is_asked(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    assert "file_facts" not in quotamirror.scan_usage(root)


def test_an_asked_for_scan_names_every_file_it_read(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    add_file(root, "b.jsonl", "m2")
    scan = quotamirror.scan_usage(root, manifest=True)
    facts = scan["file_facts"]
    assert [entry[0] for entry in facts] == ["a.jsonl", "b.jsonl"]
    assert all(len(entry) == 4 for entry in facts)
    size, mtime_ns, content_hash = facts[0][1], facts[0][2], facts[0][3]
    assert size == (root / "a.jsonl").stat().st_size
    assert mtime_ns == (root / "a.jsonl").stat().st_mtime_ns
    assert len(content_hash) == 16


def test_the_reading_carries_the_manifest_with_its_own_total(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    reading = reading_of(root, manifest=True)
    assert reading["manifest_total"] == 1
    assert reading["manifest_truncated"] is False
    assert [entry[0] for entry in reading["manifest"]] == ["a.jsonl"]


def test_the_reading_holds_no_manifest_when_it_was_not_asked(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    reading = reading_of(root)
    assert reading.get("manifest") is None
    assert reading.get("manifest_total") == 0


def test_metadata_only_names_the_touched_file(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    add_file(root, "b.jsonl", "m2")
    prior = prior_of(root, manifest=True)
    tap(root / "b.jsonl")
    block = compared(root, prior, manifest=True)
    assert block["verdict"] == "metadata_only"
    assert block["attribution"]["available"] is True
    assert block["attribution"]["mtime_only"] == ["b.jsonl"]
    assert block["attribution"]["mtime_only_total"] == 1
    assert block["attribution"]["content_changed"] == []
    assert block["attribution"]["added"] == []
    assert block["attribution"]["removed"] == []


def test_bytes_differ_names_the_edited_file(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    add_file(root, "b.jsonl", "m2", tokens=20)
    prior = prior_of(root, manifest=True)
    target = root / "b.jsonl"
    stat = target.stat()
    body = target.read_text(encoding="utf-8")
    target.write_text(body.replace('"input_tokens": 20', '"input_tokens": 30'), encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    block = compared(root, prior, manifest=True)
    assert block["verdict"] == "bytes_differ"
    assert block["attribution"]["content_changed"] == ["b.jsonl"]
    assert block["attribution"]["mtime_only"] == []


def test_a_renamed_file_reads_as_bytes_differ_and_the_manifest_names_it(tmp_path):
    # Same length, same total, one name gone and one new: the verdict cannot
    # tell this from an in-place edit (that is what `bytes_differ` means), and
    # the manifest is the only thing that can.
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    add_file(root, "b.jsonl", "m2")
    prior = prior_of(root, manifest=True)
    (root / "b.jsonl").unlink()
    add_file(root, "c.jsonl", "m3")
    block = compared(root, prior, manifest=True)
    assert block["verdict"] == "bytes_differ"
    assert block["attribution"]["added"] == ["c.jsonl"]
    assert block["attribution"]["removed"] == ["b.jsonl"]
    assert block["attribution"]["content_changed"] == []


def test_a_file_appearing_beside_the_old_one_is_a_file_set_difference(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root, manifest=True)
    add_file(root, "c.jsonl", "m3")
    block = compared(root, prior, manifest=True)
    assert block["verdict"] == "file_set_differs"
    assert block["attribution"]["added"] == ["c.jsonl"]
    assert block["attribution"]["added_total"] == 1


def test_without_a_manifest_the_comparison_counts_and_does_not_name(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    tap(root / "a.jsonl")
    block = compared(root, prior)
    assert block["verdict"] == "metadata_only"
    attribution = block["attribution"]
    assert attribution["available"] is False
    assert "--manifest" in attribution["reason"]
    assert attribution["mtime_only"] == []


def test_a_prior_without_a_manifest_while_this_run_has_one_still_refuses(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    tap(root / "a.jsonl")
    block = compared(root, prior, manifest=True)
    assert block["attribution"]["available"] is False
    assert "supplied" in block["attribution"]["reason"]


def test_a_truncated_manifest_refuses_to_name_a_file(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root, manifest=True)
    prior["manifest_truncated"] = True
    tap(root / "a.jsonl")
    block = compared(root, prior, manifest=True)
    assert block["verdict"] == "metadata_only"
    assert block["attribution"]["available"] is False
    assert "cap" in block["attribution"]["reason"]


def test_a_manifest_that_contradicts_its_own_file_count_is_refused(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root, manifest=True)
    prior["manifest_total"] = prior["files"] + 3
    tap(root / "a.jsonl")
    block = compared(root, prior, manifest=True)
    assert block["attribution"]["available"] is False
    assert "file" in block["attribution"]["reason"]


def test_names_stop_at_the_cap_but_the_count_does_not(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    for index in range(12):
        add_file(root, f"f{index:02d}.jsonl", f"m{index}", tokens=100 + index)
    prior = prior_of(root, manifest=True)
    for index in range(12):
        tap(root / f"f{index:02d}.jsonl")
    block = compared(root, prior, manifest=True)
    attribution = block["attribution"]
    assert attribution["available"] is True
    assert attribution["mtime_only_total"] == 12
    assert len(attribution["mtime_only"]) == attribution["capped_at"]
    assert attribution["mtime_only"] == sorted(attribution["mtime_only"])


def test_a_malformed_manifest_is_refused_when_supplied(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    doc = json.loads(json.dumps(reading_of(root, manifest=True)))
    doc["manifest"] = [["a.jsonl", 1]]
    with pytest.raises(ValueError):
        quotamirror._prior_reading(doc)
    doc["manifest"] = "a.jsonl|1|2|abc"
    with pytest.raises(ValueError):
        quotamirror._prior_reading(doc)


def test_the_cli_flag_puts_a_manifest_in_the_payload(tmp_path, capsys):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(
        json.dumps(cache_with([("2026-05-04", 600)])), encoding="utf-8"
    )
    code = quotamirror.main(
        [
            "quiet",
            "--path",
            str(cache_path),
            "--transcripts",
            str(root),
            "--json",
            "--manifest",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry[0] for entry in payload["arbitration"]["reading"]["manifest"]] == ["a.jsonl"]


def test_the_cli_payload_has_no_manifest_without_the_flag(tmp_path, capsys):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    cache_path = tmp_path / "stats-cache.json"
    cache_path.write_text(
        json.dumps(cache_with([("2026-05-04", 600)])), encoding="utf-8"
    )
    code = quotamirror.main(
        ["quiet", "--path", str(cache_path), "--transcripts", str(root), "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["arbitration"]["reading"].get("manifest") is None
