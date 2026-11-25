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
