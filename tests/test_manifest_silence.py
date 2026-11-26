"""A manifest that never said how many files it read is not a whole one.

`--manifest` is the only part of a reading that can name a file, and the tool
already refuses to name anything from a capped list or from a list that
disagrees with the reading's own file count. There was a third way in: a
reading whose manifest list is present but whose `manifest_total` is absent (a
reading taken by an older release, or passed through something that dropped the
field) or is zero. Both were read as "the list is the whole reading", because a
missing total was silently replaced by the length of the list.

That is the failure this tool exists to prevent - an absent statement read as a
whole one - so the rule here is: a manifest can only be shown to be whole if the
reading states a file total and the total matches the list. The pair comparison
and the chain both answer to that rule, because they name files from the same
lists.

All fixtures are synthetic.
"""

import json

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


def corpus(tmp_path, name="a.jsonl"):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        json.dumps(usage_line("m1", "2026-05-04T10:00:00Z", 600)) + "\n", encoding="utf-8"
    )
    return root


def reading_of(root, manifest=True):
    scan = quotamirror.scan_usage(root, manifest=manifest)
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 600)]), scan, min_tokens=0)
    return payload["arbitration"]["reading"]


def prior_of(root):
    return quotamirror._prior_reading(json.loads(json.dumps(reading_of(root))))


def compared(root, prior):
    import os

    target = root / "a.jsonl"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 3600 * 10**9))
    scan = quotamirror.scan_usage(root, manifest=True)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]), scan, min_tokens=0, prior=prior
    )
    return payload["reading_compare"]


def side(manifest, total, files=1, content="aaaa"):
    return {
        "files": files,
        "bytes": 10,
        "records": 1,
        "digest": "path",
        "content_digest": content,
        "digest_basis": "name+size+mtime",
        "content_basis": "name+sha256(bytes)",
        "manifest": manifest,
        "manifest_total": total,
        "manifest_truncated": False,
    }


# --- the pair ---------------------------------------------------------------


def test_a_supplied_manifest_without_a_file_total_refuses_to_name(tmp_path):
    root = corpus(tmp_path)
    prior = prior_of(root)
    del prior["manifest_total"]
    block = compared(root, prior)
    assert block["verdict"] == "metadata_only"
    assert block["attribution"]["available"] is False
    assert "how many" in block["attribution"]["reason"]


def test_a_supplied_manifest_total_of_zero_beside_a_list_is_not_a_whole_list(tmp_path):
    root = corpus(tmp_path)
    prior = prior_of(root)
    prior["manifest_total"] = 0
    block = compared(root, prior)
    assert block["attribution"]["available"] is False
    assert "file" in block["attribution"]["reason"]


def test_a_whole_manifest_still_names_the_touched_file(tmp_path):
    root = corpus(tmp_path)
    prior = prior_of(root)
    block = compared(root, prior)
    assert block["attribution"]["available"] is True
    assert block["attribution"]["mtime_only"] == ["a.jsonl"]


# --- the chain -------------------------------------------------------------


def test_a_chain_side_whose_manifest_disagrees_with_its_total_is_not_named():
    entry = ["a.jsonl", 10, 1, "hash"]
    prior = side([entry], total=5, files=None)
    chain = quotamirror.reading_chain([prior], side([entry], total=1))
    names = chain["names"]
    assert names["available"] is False
    assert "manifest" in names["reason"] or "file(s)" in names["reason"]


def test_a_chain_side_without_a_file_total_is_not_named():
    entry = ["a.jsonl", 10, 1, "hash"]
    prior = side([entry], total=None)
    chain = quotamirror.reading_chain([prior], side([entry], total=1))
    names = chain["names"]
    assert names["available"] is False
    assert "how many" in names["reason"] or "total" in names["reason"]


def test_a_chain_with_whole_manifests_still_follows_a_name():
    entry = ["a.jsonl", 10, 1, "hash"]
    chain = quotamirror.reading_chain([side([entry], total=1)], side([entry], total=1))
    names = chain["names"]
    assert names["available"] is True
    assert names["throughout"] == 1
