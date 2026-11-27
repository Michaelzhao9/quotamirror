"""A rename and an edit are different sentences, and the manifest can tell them.

0.50.0 taught the comparison to name the files behind a moved digest, in four
lists: touched, edited, added, removed. That left one case reading wrong - a
file renamed in place arrives as one `added` plus one `removed`, which is the
same shape an add-beside-a-delete takes. The manifest already carries each
file's size and content hash, so bytes that disappear under one name and
reappear under another can be paired, and a rename can be named as a move.

The pairing is refused unless it is one-to-one: two files with identical
content on either side make the pairing a guess, and a guessed move would be a
fabricated subject. Such names stay in `added`/`removed` and are counted as
ambiguous.

All fixtures are synthetic.
"""

import json
import os

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


def corpus(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.jsonl").write_text(
        json.dumps(usage_line("m1", "2026-05-04T10:00:00Z", 600)) + "\n", encoding="utf-8"
    )
    return root


def add_file(root, name, message_id, tokens=10, hour=11):
    (root / name).write_text(
        json.dumps(usage_line(message_id, f"2026-05-04T{hour:02d}:00:00Z", tokens)) + "\n",
        encoding="utf-8",
    )
    return root / name


def reading_of(root, manifest=True):
    scan = quotamirror.scan_usage(root, manifest=manifest)
    payload = quotamirror.quiet_days(cache_with([("2026-05-04", 600)]), scan, min_tokens=0)
    return payload["arbitration"]["reading"]


def prior_of(root, manifest=True):
    return quotamirror._prior_reading(json.loads(json.dumps(reading_of(root, manifest))))


def compared(root, prior, manifest=True):
    scan = quotamirror.scan_usage(root, manifest=manifest)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]), scan, min_tokens=0, prior=prior
    )
    return payload["reading_compare"]


def rendered(root, prior):
    """The lines a reader sees, as the CLI builds them."""
    block = compared(root, prior)
    return "\n".join(quotamirror._compare_lines(block))


def test_a_renamed_file_is_named_as_a_move_not_as_added_and_removed(tmp_path):
    root = corpus(tmp_path)
    before = add_file(root, "b.jsonl", "m2")
    prior = prior_of(root)
    size = before.stat().st_size
    before.rename(root / "c.jsonl")
    block = compared(root, prior)
    assert block["verdict"] == "bytes_differ"
    attribution = block["attribution"]
    assert attribution["available"] is True
    assert attribution["moved_total"] == 1
    assert attribution["moved"] == [
        {"from": "b.jsonl", "to": "c.jsonl", "size": size, "size_from": size, "grown": False}
    ]
    assert attribution["added"] == []
    assert attribution["removed"] == []
    assert attribution["moved_ambiguous_total"] == 0


def test_a_move_and_a_same_length_edit_are_named_as_two_different_things(tmp_path):
    root = corpus(tmp_path)
    moved_from = add_file(root, "b.jsonl", "m2")
    prior = prior_of(root)
    moved_from.rename(root / "c.jsonl")
    target = root / "a.jsonl"
    stat = target.stat()
    body = target.read_text(encoding="utf-8")
    target.write_text(body.replace('"input_tokens": 600', '"input_tokens": 700'), encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    attribution = compared(root, prior)["attribution"]
    assert attribution["moved_total"] == 1
    assert attribution["content_changed"] == ["a.jsonl"]
    assert attribution["added"] == []
    assert attribution["removed"] == []


def test_two_identical_files_are_never_paired_and_the_guess_is_counted(tmp_path):
    # Same bytes under two names, both renamed: which new name is which old
    # one is unknowable, so nothing may be called a move.
    root = corpus(tmp_path)
    add_file(root, "b.jsonl", "m2", tokens=10, hour=11)
    add_file(root, "d.jsonl", "m2", tokens=10, hour=11)
    prior = prior_of(root)
    (root / "b.jsonl").rename(root / "c.jsonl")
    (root / "d.jsonl").rename(root / "e.jsonl")
    attribution = compared(root, prior)["attribution"]
    assert attribution["moved"] == []
    assert attribution["moved_total"] == 0
    assert attribution["moved_ambiguous_total"] == 2
    assert attribution["added"] == ["c.jsonl", "e.jsonl"]
    assert attribution["removed"] == ["b.jsonl", "d.jsonl"]


def test_a_new_file_whose_bytes_match_no_missing_file_is_still_added(tmp_path):
    root = corpus(tmp_path)
    prior = prior_of(root)
    add_file(root, "c.jsonl", "m3")
    attribution = compared(root, prior)["attribution"]
    assert attribution["moved_total"] == 0
    assert attribution["added"] == ["c.jsonl"]
    assert attribution["added_total"] == 1


def test_every_changed_name_is_reported_once_across_the_three_lists(tmp_path):
    # A move must not also be counted as a file that appeared: the three lists
    # partition the names, so no name is lost and none is printed twice.
    root = corpus(tmp_path)
    before = add_file(root, "b.jsonl", "m2")
    add_file(root, "f.jsonl", "m4", tokens=40, hour=12)
    prior = prior_of(root)
    before.rename(root / "c.jsonl")
    (root / "f.jsonl").unlink()
    add_file(root, "g.jsonl", "m5", tokens=50, hour=13)
    attribution = compared(root, prior)["attribution"]
    names = [row["to"] for row in attribution["moved"]]
    names += [row["from"] for row in attribution["moved"]]
    names += attribution["added"] + attribution["removed"]
    assert len(names) == len(set(names))
    assert sorted(attribution["added"]) == ["g.jsonl"]
    assert sorted(attribution["removed"]) == ["f.jsonl"]
    assert attribution["moved_total"] == 1


def test_the_text_names_a_move_with_an_arrow(tmp_path):
    root = corpus(tmp_path)
    add_file(root, "b.jsonl", "m2")
    prior = prior_of(root)
    (root / "b.jsonl").rename(root / "c.jsonl")
    screen = rendered(root, prior)
    assert "moved 1: b.jsonl -> c.jsonl" in screen
    assert "added 1" not in screen
    assert "removed 1" not in screen


def test_the_text_counts_an_ambiguous_pair_instead_of_naming_a_move(tmp_path):
    root = corpus(tmp_path)
    add_file(root, "b.jsonl", "m2", tokens=10, hour=11)
    add_file(root, "d.jsonl", "m2", tokens=10, hour=11)
    prior = prior_of(root)
    (root / "b.jsonl").rename(root / "c.jsonl")
    (root / "d.jsonl").rename(root / "e.jsonl")
    screen = rendered(root, prior)
    assert "->" not in screen
    assert "2 file(s) share content with a missing name" in screen
    assert "added 2" in screen and "removed 2" in screen
