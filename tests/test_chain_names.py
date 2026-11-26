"""A chain of readings can say which step a file entered or left.

The neighbouring pairs are classified by bytes, so a chain can say *that* the
tree moved between reading 2 and reading 3. A reader's next question is
*which* file moved, and the pair block answers it only when the caller keeps a
manifest on each reading. This block walks the same readings by file name and
says where each name first appeared and last appeared - or refuses, because a
name can only be followed on readings that carry one.

Every fixture here is synthetic; the names are made up.
"""

import quotamirror


def entry(name, size=10, mtime=1, digest="0123456789abcdef"):
    return [name, size, mtime, digest]


def reading(names, content="aaaa", files=None, records=2):
    """A normalised reading as `_prior_reading` hands it to the chain."""
    manifest = {name: entry(name) for name in names} if names is not None else None
    listing = sorted(manifest.values()) if manifest is not None else None
    return {
        "files": len(manifest) if manifest is not None else files,
        "bytes": 10 * (len(manifest) if manifest is not None else (files or 0)),
        "records": records,
        "digest": "path-" + content,
        "content_digest": content,
        "digest_basis": "name+size+mtime_ns",
        "content_basis": "name+sha256(bytes)",
        "manifest": listing,
        "manifest_total": len(listing) if listing is not None else 0,
        "manifest_truncated": False,
    }


def row(chain, name):
    return next(item for item in chain["names"]["files"] if item["name"] == name)


def test_a_file_that_enters_is_named_with_the_step_it_entered():
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl", "b.jsonl"]), reading(["a.jsonl", "b.jsonl", "c.jsonl"])],
        reading(["a.jsonl", "b.jsonl", "c.jsonl"]),
    )
    names = chain["names"]
    assert names["available"] is True
    assert names["verdict"] == "names_changed"
    assert names["entered"] == 1
    assert names["left"] == 0
    assert names["throughout"] == 2
    assert row(chain, "c.jsonl")["entered_at"] == 2
    assert row(chain, "c.jsonl")["steps"] == [2, 3]


def test_a_file_that_leaves_is_named_with_the_step_it_was_last_seen():
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl", "b.jsonl"]), reading(["a.jsonl", "b.jsonl"])],
        reading(["a.jsonl"]),
    )
    names = chain["names"]
    assert names["verdict"] == "names_changed"
    assert names["left"] == 1
    assert row(chain, "b.jsonl")["state"] == "left"
    assert row(chain, "b.jsonl")["left_at"] == 3


def test_a_file_that_leaves_and_returns_is_not_called_gone():
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl", "b.jsonl"]), reading(["a.jsonl"])],
        reading(["a.jsonl", "b.jsonl"]),
    )
    names = chain["names"]
    assert row(chain, "b.jsonl")["state"] == "blink"
    assert names["blink"] == 1
    assert names["left"] == 0
    assert names["entered"] == 0


def test_a_chain_of_one_set_of_names_says_names_did_not_change():
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl"]), reading(["a.jsonl"])], reading(["a.jsonl"])
    )
    names = chain["names"]
    assert names["verdict"] == "names_stable"
    assert names["throughout"] == 1
    assert names["changed"] == 0


def test_without_a_manifest_no_name_is_followed():
    chain = quotamirror.reading_chain(
        [reading(None, files=3), reading(None, files=4)], reading(None, files=4)
    )
    names = chain["names"]
    assert names["available"] is False
    assert names["verdict"] == "not_named"
    assert "manifest" in names["reason"]
    assert names["files"] == []


def test_a_truncated_manifest_refuses_to_name_the_chain():
    truncated = reading(["a.jsonl", "b.jsonl"])
    truncated["manifest_truncated"] = True
    chain = quotamirror.reading_chain([reading(["a.jsonl"]), truncated], reading(["a.jsonl"]))
    names = chain["names"]
    assert names["available"] is False
    assert names["verdict"] == "not_named"
    assert "truncated" in names["reason"]


# --- the screen -------------------------------------------------------------


def test_the_names_line_says_which_file_entered_and_which_left():
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl", "b.jsonl"]), reading(["a.jsonl", "b.jsonl", "c.jsonl"])],
        reading(["a.jsonl", "c.jsonl"]),
    )
    text = "\n".join(quotamirror._chain_lines(chain))
    assert "names" in text
    assert "c.jsonl" in text
    assert "entered on reading 2" in text
    assert "left" in text and "b.jsonl" in text


def test_the_screen_states_why_no_name_is_followed():
    chain = quotamirror.reading_chain(
        [reading(None, files=3), reading(None, files=4)], reading(None, files=4)
    )
    text = "\n".join(quotamirror._chain_lines(chain))
    assert "not named" in text
    assert "no file manifest" in text


def test_no_names_line_is_wider_than_the_screen():
    long_name = "0d6af2d3-4f5f-4283-bb21-bf2baf8be988-some-rather-long-transcript-name.jsonl"
    chain = quotamirror.reading_chain(
        [reading([long_name, "b.jsonl"]), reading(["b.jsonl"])],
        reading([long_name, "b.jsonl", "c.jsonl"]),
    )
    for line in quotamirror._chain_lines(chain):
        assert len(line) <= 80, line


def test_a_name_too_long_for_the_screen_is_shortened_and_says_so():
    long_name = "0d6af2d3-4f5f-4283-bb21-bf2baf8be988-some-rather-long-transcript-name.jsonl"
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl"]), reading(["a.jsonl"])], reading([long_name])
    )
    text = "\n".join(quotamirror._chain_lines(chain))
    assert long_name not in text
    assert "…" in text
    assert "full name" in text
    assert any(item["name"] == long_name for item in chain["names"]["files"])


def test_a_name_that_enters_on_the_last_reading_is_not_held_to_it():
    chain = quotamirror.reading_chain(
        [reading(["a.jsonl"]), reading(["a.jsonl"])], reading(["a.jsonl", "b.jsonl"])
    )
    text = "\n".join(quotamirror._chain_lines(chain))
    assert "held to reading 3" not in text
    assert "entered on reading 3, the last reading" in text


# --- content under an unchanged name ---------------------------------------
#
# A manifest entry carries a content hash, so two readings that hold the same
# name can be compared byte for byte for that file without inventing identity:
# the name is the identity. Before this, a chain could say "different bytes"
# twice and still print "7 in every reading", leaving out the two files that
# actually changed.


def screen(chain):
    """The rendered chain with wrapping collapsed to single spaces."""
    return " ".join(" ".join(quotamirror._chain_lines(chain)).split())


def reading_of(hashes, records=2):
    """A reading whose manifest carries one content hash per name."""
    listing = sorted(entry(name, digest=digest) for name, digest in hashes.items())
    content = "c" + str(abs(hash(tuple(sorted(hashes.items())))) % 100000)
    return {
        "files": len(listing),
        "bytes": 10 * len(listing),
        "records": records,
        "digest": "path-" + content,
        "content_digest": content,
        "digest_basis": "name+size+mtime_ns",
        "content_basis": "name+sha256(bytes)",
        "manifest": listing,
        "manifest_total": len(listing),
        "manifest_truncated": False,
    }


def test_a_file_edited_under_the_same_name_is_named_with_the_step_it_changed():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111", "b.jsonl": "2222222222222222"}),
            reading_of({"a.jsonl": "9999999999999999", "b.jsonl": "2222222222222222"}),
        ],
        reading_of({"a.jsonl": "9999999999999999", "b.jsonl": "2222222222222222"}),
    )
    names = chain["names"]
    assert names["available"] is True
    assert row(chain, "a.jsonl")["state"] == "throughout"
    assert row(chain, "a.jsonl")["edited_at"] == [2]
    assert row(chain, "b.jsonl")["edited_at"] == []
    assert names["edited"] == 1


def test_names_that_held_while_their_bytes_moved_are_not_called_stable():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"a.jsonl": "9999999999999999"}),
        ],
        reading_of({"a.jsonl": "9999999999999999"}),
    )
    names = chain["names"]
    assert names["changed"] == 0
    assert names["verdict"] == "names_held_content_moved"


def test_the_names_line_counts_edits_beside_entries_and_exits():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"a.jsonl": "9999999999999999"}),
        ],
        reading_of({"a.jsonl": "9999999999999999"}),
    )
    assert "1 edited in place" in screen(chain)


def test_the_edited_name_is_printed_with_the_step_its_bytes_changed():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"a.jsonl": "9999999999999999"}),
        ],
        reading_of({"a.jsonl": "9999999999999999"}),
    )
    text = screen(chain)
    assert "edited a.jsonl" in text
    assert "bytes changed on reading 2" in text


def test_a_manifest_entry_without_a_content_hash_is_not_called_edited():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"a.jsonl": ""}),
        ],
        reading_of({"a.jsonl": ""}),
    )
    names = chain["names"]
    assert row(chain, "a.jsonl")["edited_at"] == []
    assert names["edited"] == 0
    assert names["content_unmeasured"] == 1
    assert names["verdict"] == "names_stable"
    assert "1 with no content hash" in screen(chain)


def test_a_file_that_entered_and_was_then_edited_says_both():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"a.jsonl": "1111111111111111", "b.jsonl": "2222222222222222"}),
        ],
        reading_of({"a.jsonl": "1111111111111111", "b.jsonl": "3333333333333333"}),
    )
    item = row(chain, "b.jsonl")
    assert item["state"] == "entered"
    assert item["edited_at"] == [3]
    text = screen(chain)
    assert "entered on reading 2" in text
    assert "bytes changed on reading 3" in text


def test_no_names_line_is_wider_than_the_screen_when_a_long_name_is_edited():
    long_name = "0d6af2d3-4f5f-4283-bb21-bf2baf8be988-some-rather-long-transcript-name.jsonl"
    chain = quotamirror.reading_chain(
        [
            reading_of({long_name: "1111111111111111", "b.jsonl": "2222222222222222"}),
            reading_of({long_name: "9999999999999999", "b.jsonl": "2222222222222222"}),
        ],
        reading_of({long_name: "9999999999999999", "b.jsonl": "2222222222222222"}),
    )
    for line in quotamirror._chain_lines(chain):
        assert len(line) <= 80, line


# --- a rename is one event, not a departure beside an arrival ---------------
#
# The pair rows already call identical bytes under two names a move, when the
# caller kept a --manifest on both readings. The names block did not: it
# counted the same event as one name that left and another that entered, so a
# chain could print "bytes_differ" on the pair row and "1 entered, 1 left" a
# few lines later about one renamed file.


def test_a_rename_with_identical_bytes_is_named_as_one_event():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"old-a.jsonl": "1111111111111111"}),
        ],
        reading_of({"old-a.jsonl": "1111111111111111"}),
    )
    names = chain["names"]
    assert names["renamed_total"] == 1
    assert names["renames"] == [
        {"from": "a.jsonl", "to": "old-a.jsonl", "step": 2, "size": 10}
    ]
    assert row(chain, "a.jsonl")["renamed_to"] == "old-a.jsonl"
    assert row(chain, "old-a.jsonl")["renamed_from"] == "a.jsonl"
    assert "renamed a.jsonl -> old-a.jsonl on reading 2" in screen(chain)


def test_two_files_that_share_bytes_are_not_paired_into_a_rename():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a1.jsonl": "1111111111111111", "a2.jsonl": "1111111111111111"}),
            reading_of({"b1.jsonl": "1111111111111111", "b2.jsonl": "1111111111111111"}),
        ],
        reading_of({"b1.jsonl": "1111111111111111", "b2.jsonl": "1111111111111111"}),
    )
    names = chain["names"]
    assert names["renamed_total"] == 0
    assert names["renamed_ambiguous_total"] == 2
    assert row(chain, "a1.jsonl")["renamed_to"] is None
    assert "the match is not one-to-one" in screen(chain)


def test_a_rename_that_also_edits_its_bytes_is_not_paired():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111"}),
            reading_of({"old-a.jsonl": "9999999999999999"}),
        ],
        reading_of({"old-a.jsonl": "9999999999999999"}),
    )
    names = chain["names"]
    assert names["renamed_total"] == 0
    assert names["renamed_ambiguous_total"] == 0
    assert row(chain, "a.jsonl")["state"] == "left"
    assert row(chain, "old-a.jsonl")["state"] == "entered"
    assert "a rename that also edits its bytes" in names["limit"]


def test_the_rename_line_is_not_wider_than_the_screen():
    long_from = "0d6af2d3-4f5f-4283-bb21-bf2baf8be988-transcript-one.jsonl"
    long_to = "renamed-0d6af2d3-4f5f-4283-bb21-bf2baf8be988-transcript-one.jsonl"
    chain = quotamirror.reading_chain(
        [
            reading_of({long_from: "1111111111111111", "b.jsonl": "2222222222222222"}),
            reading_of({long_to: "1111111111111111", "b.jsonl": "2222222222222222"}),
        ],
        reading_of({long_to: "1111111111111111", "b.jsonl": "2222222222222222"}),
    )
    assert chain["names"]["renamed_total"] == 1
    for line in quotamirror._chain_lines(chain):
        assert len(line) <= 80, line


# --- the two rows a rename leaves behind have to point at each other --------
#
# The renamed line says the pair is one file, but the `left` and `entered` rows
# below it still read as two unrelated names: a reader who stops at those rows
# sees a departure and an arrival, which is exactly the reading the renamed
# line exists to prevent. The rows already carry renamed_to / renamed_from in
# the payload; this is only the screen catching up with it.


def test_the_rows_behind_a_rename_name_each_other():
    chain = quotamirror.reading_chain(
        [
            reading_of({"a.jsonl": "1111111111111111", "b.jsonl": "2222222222222222"}),
            reading_of({"old-a.jsonl": "1111111111111111", "b.jsonl": "2222222222222222"}),
        ],
        reading_of({"old-a.jsonl": "1111111111111111", "b.jsonl": "2222222222222222"}),
    )
    text = screen(chain)
    assert "absent on reading 2, so this file is now named old-a.jsonl" in text
    assert "entered on reading 2, held to reading 3, renamed from a.jsonl" in text


def test_the_cross_reference_survives_two_long_names():
    long_from = "0d6af2d3-4f5f-4283-bb21-bf2baf8be988-transcript-one.jsonl"
    long_to = "renamed-0d6af2d3-4f5f-4283-bb21-bf2baf8be988-transcript-one.jsonl"
    chain = quotamirror.reading_chain(
        [
            reading_of({long_from: "1111111111111111", "b.jsonl": "2222222222222222"}),
            reading_of({long_to: "1111111111111111", "b.jsonl": "2222222222222222"}),
        ],
        reading_of({long_to: "1111111111111111", "b.jsonl": "2222222222222222"}),
    )
    text = screen(chain)
    assert "so this file is now named" in text
    assert "renamed from" in text
    for line in quotamirror._chain_lines(chain):
        assert len(line) <= 80, line
