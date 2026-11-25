"""Three readings have a verdict, not just the pair handed in last.

`quiet --compare-reading` compares this run with one saved reading. That is a
pair verdict, and the README said so: a third reading kept by the caller had no
verdict, because only the pair given is compared and a change between two
readings is not attributed when the reading in between was never compared.

The chain closes that without storing anything: the caller hands the readings
back in order (oldest first) and each neighbouring pair is classified with the
same five words a single pair already gets. The chain adds only what a chain can
say that a pair cannot:

* which neighbouring pair is the one that moved (the pair is *named* by index),
* a hole - a reading with no byte axis is stated as a hole in that position
  instead of being silently dropped,
* a return to bytes already seen, counted only where the counts agree, because
  "the corpus came back to bytes reading 1 had" and "two different trees happen
  to hold one name equally" are not the same statement,
* and the honest limit: nothing inside a reading stamps when it was taken, so
  the order is the caller's claim. The block says so instead of guessing.

Every fixture here is synthetic.
"""

import json

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


def corpus(tmp_path, lines, name="a.jsonl", tag=None):
    root = tmp_path / ("transcripts" if tag is None else f"transcripts-{tag}")
    root.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(line) + "\n" for line in lines)
    (root / name).write_text(body, encoding="utf-8")
    return root


def prior_of(root, cache=None):
    scan = quotamirror.scan_usage(root)
    payload = quotamirror.quiet_days(
        cache if cache is not None else cache_with([("2026-05-04", 600)]), scan, min_tokens=0
    )
    reading = payload["arbitration"]["reading"]
    return quotamirror._prior_reading(json.loads(json.dumps(reading)))


def block(files=1, bytes_=10, records=1, digest="path1", content="aaaa"):
    """A normalised reading, the shape `_prior_reading` hands the comparison."""
    return {
        "files": files,
        "bytes": bytes_,
        "records": records,
        "digest": digest,
        "content_digest": content,
        "digest_basis": "name+size+mtime",
        "content_basis": "name+sha256(bytes)",
        "manifest": None,
        "manifest_total": 0,
        "manifest_truncated": False,
    }


# --- the block itself -------------------------------------------------------


def test_a_chain_lists_one_pair_per_neighbour():
    chain = quotamirror.reading_chain(
        [block(content="aaaa"), block(content="aaaa")], block(content="bbbb")
    )
    assert chain["available"] is True
    assert len(chain["pairs"]) == 2
    assert [pair["from"] for pair in chain["pairs"]] == [1, 2]
    assert [pair["to"] for pair in chain["pairs"]] == [2, 3]
    assert [row["label"] for row in chain["readings"]] == ["1", "2", "this run"]


def test_a_chain_with_no_byte_moved_says_so_in_one_word():
    chain = quotamirror.reading_chain(
        [block(content="aaaa"), block(content="aaaa")], block(content="aaaa")
    )
    assert [pair["verdict"] for pair in chain["pairs"]] == ["same_reading", "same_reading"]
    assert chain["verdict"] == "no_byte_moved"
    assert chain["changed_pairs"] == 0


def test_the_pair_that_moved_is_named_by_index():
    chain = quotamirror.reading_chain(
        [block(content="aaaa"), block(content="bbbb")], block(content="bbbb")
    )
    assert chain["verdict"] == "bytes_moved"
    assert chain["changed_pairs"] == 1
    moved = [pair for pair in chain["pairs"] if pair["same_bytes"] is False]
    assert len(moved) == 1
    assert (moved[0]["from"], moved[0]["to"]) == (1, 2)


def test_a_reading_with_no_byte_axis_is_a_hole_not_a_missing_row():
    chain = quotamirror.reading_chain(
        [block(content="aaaa"), block(content=None), block(content="bbbb")], block(content="bbbb")
    )
    assert chain["verdict"] == "hole"
    assert chain["hole_pairs"] == 2
    assert len(chain["pairs"]) == 3
    assert "2" in chain["reason"]


def test_a_return_to_bytes_already_seen_is_counted():
    chain = quotamirror.reading_chain(
        [block(content="aaaa"), block(content="bbbb")], block(content="aaaa")
    )
    assert chain["verdict"] == "bytes_moved"
    assert chain["repeated_total"] == 1
    assert chain["repeated_pairs"] == [[1, 3]]


def test_a_return_whose_counts_contradict_the_bytes_is_not_counted():
    chain = quotamirror.reading_chain(
        [block(content="aaaa", files=1), block(content="bbbb", files=1)],
        block(content="aaaa", files=2),
    )
    assert chain["repeated_total"] == 0
    assert chain["contradiction_total"] == 1


def test_the_chain_states_that_the_order_is_the_caller_s_claim():
    chain = quotamirror.reading_chain([block(), block()], block())
    assert chain["order"] == "as_given"
    assert "no reading stamps" in chain["order_note"]


def test_a_chain_of_one_pair_is_not_a_chain():
    """One prior reading is the pair comparison that already existed."""
    chain = quotamirror.reading_chain([block()], block())
    assert chain["available"] is False
    assert chain["verdict"] == "not_a_chain"
    assert chain["pairs"] == []


def test_a_dropped_reading_is_named_rather_than_skipped():
    """Three readings where one is missing must not read as a two-reading chain."""
    chain = quotamirror.reading_chain([block(), None, block()], block())
    assert chain["verdict"] == "hole"
    assert "position 2" in chain["reason"]


# --- the payload ------------------------------------------------------------


def test_one_prior_keeps_the_payload_it_had_before(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]), quotamirror.scan_usage(root), min_tokens=0, prior=prior
    )
    assert "reading_chain" not in payload
    assert payload["reading_compare"]["verdict"] == "same_reading"


def test_two_priors_add_a_chain_and_keep_the_pair_verdict(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]),
        quotamirror.scan_usage(root),
        min_tokens=0,
        prior=prior,
        priors=[prior, prior],
    )
    assert payload["reading_compare"]["verdict"] == "same_reading"
    chain = payload["reading_chain"]
    assert chain["available"] is True
    assert len(chain["readings"]) == 3
    assert chain["verdict"] == "no_byte_moved"


def test_the_chain_reaches_the_text_output(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]),
        quotamirror.scan_usage(root),
        min_tokens=0,
        priors=[prior, prior],
        prior=prior,
    )
    screen = quotamirror.render_quiet(payload)
    lines = [line for line in screen.splitlines() if "chain" in line or "-> " in line]
    assert any("1 -> 2" in line for line in lines), screen
    assert any("2 -> this run" in line for line in lines), screen
    assert "no_byte_moved" in screen


# --- the command line -------------------------------------------------------


def _write_receipt(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(cache_with([("2026-05-04", 600)])), encoding="utf-8")
    return path


def _save_reading(tmp_path, reading, name):
    path = tmp_path / name
    path.write_text(json.dumps(reading), encoding="utf-8")
    return path


def test_the_flag_may_be_repeated_and_the_json_carries_the_chain(tmp_path, capsys):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    first = _save_reading(tmp_path, prior_of(root), "reading-1.json")
    second = _save_reading(tmp_path, prior_of(root), "reading-2.json")
    code = quotamirror.main(
        [
            "quiet",
            "--path",
            str(_write_receipt(tmp_path)),
            "--transcripts",
            str(root),
            "--compare-reading",
            str(first),
            "--compare-reading",
            str(second),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reading_chain"]["verdict"] == "no_byte_moved"
    assert len(payload["reading_chain"]["pairs"]) == 2


def test_a_supplied_file_that_is_not_a_reading_is_refused(tmp_path, capsys):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    good = _save_reading(tmp_path, prior_of(root), "reading-1.json")
    bad = tmp_path / "not-a-reading.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    code = quotamirror.main(
        [
            "quiet",
            "--path",
            str(_write_receipt(tmp_path)),
            "--transcripts",
            str(root),
            "--compare-reading",
            str(good),
            "--compare-reading",
            str(bad),
        ]
    )
    assert code == 2
    assert "no reading" in capsys.readouterr().err


def test_no_chain_line_is_wider_than_the_screen(tmp_path):
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]),
        quotamirror.scan_usage(root),
        min_tokens=0,
        priors=[prior, prior],
        prior=prior,
    )
    screen = quotamirror.render_quiet(payload)
    for line in screen.splitlines():
        if "chain" in line or " -> " in line or "returned" in line or "order" in line:
            assert len(line) <= 80, line


def test_the_last_pair_measures_the_run_this_command_made(tmp_path):
    """The current run's counts must reach the last pair, not print as n/a.

    A saved reading says `files`, the block this run builds says `files_read`.
    Reading only the first name left every delta against the current run
    unmeasured, which is the pair a reader is most likely to trust.
    """
    root = corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)])
    prior = prior_of(root)
    corpus(tmp_path, [usage_line("m1", "2026-05-04T10:00:00Z", 600)], tag="wider")
    wider = corpus(
        tmp_path,
        [usage_line("m2", "2026-05-05T10:00:00Z", 300)],
        name="b.jsonl",
        tag="wider",
    )
    payload = quotamirror.quiet_days(
        cache_with([("2026-05-04", 600)]),
        quotamirror.scan_usage(wider),
        min_tokens=0,
        priors=[prior, prior],
        prior=prior,
    )
    chain = payload["reading_chain"]
    last = chain["pairs"][-1]
    assert last["to"] == 3
    assert last["verdict"] == "file_set_differs"
    assert last["files_delta"] == 1
    assert last["records_delta"] == 1
    assert "files +1" in quotamirror.render_quiet(payload)
