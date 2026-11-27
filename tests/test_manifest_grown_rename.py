"""A rename that also appends bytes is still one file, not a departure plus an arrival.

A chain could already pair a name that vanished with one that appeared carrying
the *same* bytes. Anything else - and a rename followed by an append is the
common case - read as one name leaving and another entering, i.e. one event
printed twice.

The manifest carries a hash of each file's first 4 KiB beside the hash of its
whole bytes. An append leaves that prefix untouched, so a vanished name and an
appeared name whose prefixes match are one file even though its size and total
hash moved. The pairing is still forced one-to-one: two candidates on either
side is a guess, and a guess is refused here as everywhere else.

A file shorter than the prefix has head == whole-file hash, so an edit to it is
indistinguishable from a delete beside a create. That limit is stated on screen
rather than hidden by a heuristic.

Every fixture here is synthetic; the names are made up.
"""

import hashlib
import json

import quotamirror

PREFIX = 4096


def _usage_line(index, tokens=10):
    return json.dumps(
        {
            "type": "assistant",
            "message": {"id": f"m{index}", "usage": {"input_tokens": tokens}},
        }
    )


def _corpus(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _big(index, filler=b"x"):
    """One transcript big enough that its first 4 KiB is a real prefix."""
    pad = "y" * (PREFIX + 500)
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"id": f"m{index}", "usage": {"input_tokens": 10}, "pad": pad},
        }
    )
    return line


# --- the manifest -----------------------------------------------------------


def test_scan_manifest_adds_a_hash_of_the_first_4_kib(tmp_path):
    big = _corpus(tmp_path, "big.jsonl", [_big(1)])
    small = _corpus(tmp_path, "small.jsonl", [_usage_line(2)])

    scan = quotamirror.scan_usage(tmp_path, manifest=True)
    facts = {row[0]: row for row in scan["file_facts"]}

    assert len(facts["big.jsonl"]) == 5
    assert len(facts["small.jsonl"]) == 5
    big_bytes = big.read_bytes()
    assert facts["big.jsonl"][4] == hashlib.sha256(big_bytes[:PREFIX]).hexdigest()[:16]
    assert facts["big.jsonl"][4] != facts["big.jsonl"][3]
    # A file shorter than the prefix has nothing but its own bytes to hash.
    small_bytes = small.read_bytes()
    assert len(small_bytes) < PREFIX
    assert facts["small.jsonl"][4] == facts["small.jsonl"][3]


def test_a_manifest_entry_without_a_head_hash_is_still_readable(tmp_path):
    _corpus(tmp_path, "one.jsonl", [_usage_line(1)])
    index = quotamirror._manifest_index([["one.jsonl", 10, 1, "0123456789abcdef"]])
    assert index["one.jsonl"]["head"] is None
    assert index["one.jsonl"]["digest"] == "0123456789abcdef"


# --- pairing ----------------------------------------------------------------


def _index(name, size, digest, head):
    return {name: {"size": size, "mtime": 0, "digest": digest, "head": head}}


def test_pair_moves_pairs_a_rename_that_appended_bytes():
    prior = _index("old.jsonl", 5000, "aaaaaaaaaaaaaaaa", "0123456789abcdef")
    now = _index("new.jsonl", 5100, "bbbbbbbbbbbbbbbb", "0123456789abcdef")

    moved, added, removed, ambiguous, grown = quotamirror._pair_moves(
        ["new.jsonl"], ["old.jsonl"], now, prior
    )

    assert len(moved) == 1
    assert moved[0]["from"] == "old.jsonl"
    assert moved[0]["to"] == "new.jsonl"
    assert moved[0]["grown"] is True
    assert moved[0]["size_from"] == 5000
    assert moved[0]["size"] == 5100
    assert added == [] and removed == []
    assert grown == 1
    assert ambiguous == 0


def test_an_exact_rename_is_not_called_grown():
    prior = _index("old.jsonl", 5000, "aaaaaaaaaaaaaaaa", "0123456789abcdef")
    now = _index("new.jsonl", 5000, "aaaaaaaaaaaaaaaa", "0123456789abcdef")

    moved, _added, _removed, _ambiguous, grown = quotamirror._pair_moves(
        ["new.jsonl"], ["old.jsonl"], now, prior
    )

    assert moved[0]["grown"] is False
    assert grown == 0


def test_a_grown_pair_needs_one_candidate_on_each_side():
    prior = _index("old.jsonl", 5000, "aaaaaaaaaaaaaaaa", "0123456789abcdef")
    now = _index("new-a.jsonl", 5100, "bbbbbbbbbbbbbbbb", "0123456789abcdef")
    now.update(_index("new-b.jsonl", 5200, "cccccccccccccccc", "0123456789abcdef"))

    moved, added, removed, ambiguous, grown = quotamirror._pair_moves(
        ["new-a.jsonl", "new-b.jsonl"], ["old.jsonl"], now, prior
    )

    assert moved == []
    assert added == ["new-a.jsonl", "new-b.jsonl"]
    assert removed == ["old.jsonl"]
    assert grown == 0
    assert ambiguous == 2


def test_a_file_shorter_than_the_prefix_is_not_paired_by_its_hash():
    # head == whole-file hash for a short file, so an edit changes both.
    prior = _index("old.jsonl", 900, "aaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa")
    now = _index("new.jsonl", 980, "bbbbbbbbbbbbbbbb", "bbbbbbbbbbbbbbbb")

    moved, added, removed, _ambiguous, grown = quotamirror._pair_moves(
        ["new.jsonl"], ["old.jsonl"], now, prior
    )

    assert moved == []
    assert added == ["new.jsonl"] and removed == ["old.jsonl"]
    assert grown == 0


# --- the chain screen -------------------------------------------------------


def _entry(name, size, mtime, digest, head):
    return [name, size, mtime, digest, head]


def _reading(entries, content):
    listing = sorted(entries)
    return {
        "files": len(listing),
        "bytes": sum(item[1] for item in listing),
        "records": 2,
        "digest": "path-" + content,
        "content_digest": content,
        "digest_basis": "name+size+mtime_ns",
        "content_basis": "name+sha256(bytes)",
        "manifest": listing,
        "manifest_total": len(listing),
        "manifest_truncated": False,
    }


def test_chain_names_pairs_a_rename_that_appended_bytes():
    before = [_entry("a.jsonl", 5000, 1, "aaaaaaaaaaaaaaaa", "1111111111111111")]
    grown = [
        _entry("a.jsonl", 5000, 1, "aaaaaaaaaaaaaaaa", "1111111111111111"),
    ]
    after = [
        _entry("renamed.jsonl", 5100, 2, "bbbbbbbbbbbbbbbb", "1111111111111111"),
    ]
    chain = quotamirror.reading_chain(
        [_reading(before, "c1"), _reading(grown, "c2"), _reading(after, "c3")],
        _reading(after, "c3"),
    )
    names = chain["names"]

    assert names["renamed_total"] == 1
    assert names["renamed_grown_total"] == 1
    assert names["renames"][0]["grown"] is True
    assert names["renames"][0]["from"] == "a.jsonl"
    assert names["renames"][0]["to"] == "renamed.jsonl"
    text = "\n".join(quotamirror._chain_lines(chain))
    assert "appended" in text
    assert "same first 4 KiB" in text


def test_the_screen_says_which_case_was_not_paired():
    limit = quotamirror._CHAIN_NAMES_LIMIT
    assert "first 4 KiB" in limit


# --- the pair screen --------------------------------------------------------


def _side(entries, content):
    side = _reading(entries, content)
    side["index"] = 2
    return side


def test_the_pair_screen_names_a_grown_rename():
    prior = _side(
        [_entry("old.jsonl", 5000, 1, "aaaaaaaaaaaaaaaa", "1111111111111111")], "c1"
    )
    current = _side(
        [_entry("new.jsonl", 5100, 2, "bbbbbbbbbbbbbbbb", "1111111111111111")], "c2"
    )
    attribution = quotamirror._name_changes(current, prior)

    assert attribution["moved_total"] == 1
    assert attribution["moved_grown_total"] == 1
    assert attribution["moved"][0]["grown"] is True
