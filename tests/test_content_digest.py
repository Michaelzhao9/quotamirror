"""The reading digest could not see an edit; a digest over the bytes can.

`reading_digest` is a sha256 over (name, size, mtime_ns). Its docstring promised
that "an equal digest means the same bytes were read". That promise is false in
two directions, and both are everyday events for a corpus an agent writes:

* a tool rewrites a transcript and restores its mtime (or the filesystem keeps
  the same mtime granularity) with the size unchanged - the digest is equal
  while the bytes differ;
* a file is touched without any change - the digest differs while the bytes are
  identical, so "the corpus changed" is printed for a corpus that did not.

The first is the dangerous one: it makes a changed corpus look like a parsing
change. These tests pin the miss on the old axis and require a digest over the
file bytes beside it. All fixtures are synthetic.
"""

import hashlib
import json
import os

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
    path = root / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return root, path


def test_scan_carries_a_digest_over_the_file_bytes(tmp_path):
    root, path = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    scan = quotamirror.scan_usage(root)
    assert len(scan["content_digest"]) == 12
    assert scan["content_basis"] == "file bytes"
    assert scan["digest_basis"] == "path+size+mtime_ns"
    expected = hashlib.sha256(
        f"{path.name}|{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}".encode()
    ).hexdigest()[:12]
    assert scan["content_digest"] == expected


def test_touching_a_file_moves_the_old_digest_and_not_the_content_digest(tmp_path):
    """Same bytes, new mtime: only the old axis should move."""
    root, path = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    before = quotamirror.scan_usage(root)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_200_000_000_000))
    after = quotamirror.scan_usage(root)
    assert after["reading_digest"] != before["reading_digest"]
    assert after["content_digest"] == before["content_digest"]


def test_an_edit_that_keeps_size_and_mtime_is_invisible_to_the_old_digest(tmp_path):
    """The defect: 1000 -> 2000 is four bytes either way, and the mtime is put back."""
    root, path = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    before = quotamirror.scan_usage(root)
    stat = path.stat()
    original = path.read_bytes()
    assert original.count(b'"input_tokens": 1000') == 1
    path.write_bytes(original.replace(b'"input_tokens": 1000', b'"input_tokens": 2000'))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    after = quotamirror.scan_usage(root)
    assert after["tokens"]["input_tokens"] == before["tokens"]["input_tokens"] + 1000
    assert after["reading_digest"] == before["reading_digest"]  # the old blind spot, pinned
    assert after["content_digest"] != before["content_digest"]


def test_content_digest_moves_when_the_corpus_gains_a_file(tmp_path):
    root, _ = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 1000)])
    before = quotamirror.scan_usage(root)
    (root / "b.jsonl").write_text(
        json.dumps(usage_line("m2", "2026-05-05T10:00:00Z", 5)) + "\n", encoding="utf-8"
    )
    after = quotamirror.scan_usage(root)
    assert after["content_digest"] != before["content_digest"]


def test_reading_block_carries_both_digests_with_their_basis(tmp_path):
    root, _ = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    scan = quotamirror.scan_usage(root)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 600)]), scan, min_tokens=0)
    reading = result["arbitration"]["reading"]
    assert reading["digest"] == scan["reading_digest"]
    assert reading["content_digest"] == scan["content_digest"]
    assert reading["digest_basis"] == "path+size+mtime_ns"
    assert reading["content_basis"] == "file bytes"
    json.dumps(result)  # the payload must stay serialisable


def test_reading_without_a_scan_names_no_content_digest():
    reading = quotamirror._reading_axis(None, [])
    assert reading["verdict"] == "no_reading"
    assert reading["content_digest"] is None


def test_quiet_text_prints_both_digests_on_their_own_lines(tmp_path):
    root, _ = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    scan = quotamirror.scan_usage(root)
    result = quotamirror.quiet_days(cache_with([("2026-05-04", 600)]), scan, min_tokens=0)
    text = quotamirror.render_quiet(result)
    assert f"digest {scan['reading_digest']} (path+size+mtime_ns)" in text
    assert f"content {scan['content_digest']} (file bytes)" in text
    digest_lines = [line for line in text.splitlines() if " (path+size+mtime_ns)" in line or " (file bytes)" in line]
    assert len(digest_lines) == 2
    assert max(len(line) for line in digest_lines) <= 80
