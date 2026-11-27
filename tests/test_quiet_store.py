"""Tests for `quiet --store`: a reading that is kept, so the next run has a prior.

Until 0.62.0 the chain of readings existed only if the user saved every
`quiet --json` payload by hand and passed each one back with
`--compare-reading`. The lab carried that as debt ("no reading is stored; the
chain requires --manifest on every reading"): a tool whose whole comparison
axis is "what changed since" had no memory of its own.

So `--store DIR` writes this run's reading beside the earlier readings of the
same transcript corpus, keeps the newest few, and - when the user supplied no
`--compare-reading` - takes the newest one held there as the prior, saying so
on screen. `--manifest` is required, because a stored reading that cannot name
a file cannot be chained, which is the other half of the same debt.

All fixtures are synthetic.
"""

import json
import os
import stat

import quotamirror


def usage_line(message_id, ts, input_tokens=1000):
    return {
        "type": "assistant",
        "uuid": f"u-{message_id}",
        "isSidechain": False,
        "timestamp": ts,
        "message": {
            "id": message_id,
            "model": "claude-test",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }


def corpus(tmp_path, day="2026-05-04", tokens=1000):
    """A receipt and one transcript that agree on a single day."""
    root = tmp_path / "transcripts"
    root.mkdir(exist_ok=True)
    (root / "a.jsonl").write_text(
        json.dumps(usage_line("m1", f"{day}T10:00:00Z", tokens)) + "\n", encoding="utf-8"
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 5,
                "dailyActivity": [{"date": day, "messageCount": 5, "sessionCount": 1}],
                "dailyModelTokensVersion": 5,
                "dailyModelTokens": [{"date": day, "tokensByModel": {"claude-test": tokens}}],
            }
        ),
        encoding="utf-8",
    )
    return cache_path, root


def run(cache_path, root, *extra):
    return quotamirror.main(
        ["quiet", "--path", str(cache_path), "--transcripts", str(root), *extra]
    )


def stored_files(store):
    return sorted(path for path in store.iterdir() if path.suffix == ".json")


def reading_payload(files=3, records=9, digest="aaaa1111bbbb"):
    """The smallest document that is a reading, shaped like a quiet payload."""
    return {
        "arbitration": {
            "reading": {
                "available": True,
                "files_read": files,
                "bytes_read": 1200,
                "records": records,
                "reading_digest": digest,
            }
        }
    }


def test_store_without_a_manifest_is_refused_before_it_writes(tmp_path, capsys):
    cache_path, root = corpus(tmp_path)
    store = tmp_path / "readings"

    code = run(cache_path, root, "--store", str(store), "--json")

    captured = capsys.readouterr()
    assert code == 2
    assert "--manifest" in captured.err
    # a refusal that still left a file behind would be a half-done run
    assert not store.exists()


def test_a_stored_reading_is_named_and_written_with_private_permissions(tmp_path, capsys):
    cache_path, root = corpus(tmp_path)
    store = tmp_path / "readings"

    code = run(cache_path, root, "--store", str(store), "--manifest", "--json")

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["reading_store"]
    assert block["available"] is True
    assert block["kept"] == 1 and block["pruned"] == 0
    written = store / block["name"]
    assert written.is_file()
    assert block["name"].startswith(block["corpus"] + "-")
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600


def test_the_second_run_takes_the_stored_reading_as_its_prior(tmp_path, capsys):
    cache_path, root = corpus(tmp_path)
    store = tmp_path / "readings"
    first = run(cache_path, root, "--store", str(store), "--manifest", "--json")
    assert first == 0
    first_payload = json.loads(capsys.readouterr().out)

    code = run(cache_path, root, "--store", str(store), "--manifest", "--json")

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    block = payload["reading_store"]
    assert block["kept"] == 2
    assert block["prior"]["name"] == first_payload["reading_store"]["name"]
    assert block["prior"]["taken_at"].endswith("Z")
    # the same bytes read twice: the comparison must be the same reading, not a gap
    assert payload["reading_compare"]["verdict"] == "same_reading"


def test_a_stored_reading_reloads_through_the_loader_compare_reading_uses(tmp_path, capsys):
    cache_path, root = corpus(tmp_path)
    store = tmp_path / "readings"
    assert run(cache_path, root, "--store", str(store), "--manifest", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    loaded = quotamirror.read_prior_reading(store / payload["reading_store"]["name"])

    reading = payload["arbitration"]["reading"]
    assert loaded["files"] == reading["files_read"]
    assert loaded["bytes"] == reading["bytes"]
    assert loaded["records"] == reading["records_parsed"]
    assert loaded["digest"] == reading["digest"]


def test_only_the_newest_five_readings_are_kept(tmp_path):
    store = tmp_path / "readings"
    for hour in range(6):
        quotamirror.store_reading(
            store,
            "0123456789ab",
            reading_payload(files=hour + 1),
            taken_at=f"2026-05-04T0{hour}:00:00Z",
        )

    kept = stored_files(store)
    assert len(kept) == 5
    assert kept[0].name == "0123456789ab-20260504T010000Z.json"


def test_an_unreadable_stored_file_is_skipped_and_counted_not_trusted(tmp_path):
    store = tmp_path / "readings"
    store.mkdir()
    (store / "0123456789ab-20260504T000000Z.json").write_text(
        json.dumps(reading_payload(files=3)), encoding="utf-8"
    )
    (store / "0123456789ab-20260504T010000Z.json").write_text("{not json", encoding="utf-8")

    block, name, taken_at, skipped = quotamirror.load_stored_reading(store, "0123456789ab")

    assert name == "0123456789ab-20260504T000000Z.json"
    assert block["files"] == 3
    assert taken_at == "2026-05-04T00:00:00Z"
    assert skipped == 1


def test_a_symlinked_store_directory_is_refused(tmp_path, capsys):
    cache_path, root = corpus(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real)

    code = run(cache_path, root, "--store", str(link), "--manifest", "--json")

    captured = capsys.readouterr()
    assert code == 2
    assert "symlink" in captured.err
    assert list(real.iterdir()) == []


def test_a_stored_reading_carries_the_reading_not_the_verdict_it_produced(tmp_path, capsys):
    # A comparison block embeds the prior reading's manifest. Keeping it inside
    # the stored payload would double the file and make a reading a record of
    # its own verdicts - so the stored document is the reading alone.
    cache_path, root = corpus(tmp_path)
    store = tmp_path / "readings"
    assert run(cache_path, root, "--store", str(store), "--manifest", "--json") == 0
    capsys.readouterr()
    assert run(cache_path, root, "--store", str(store), "--manifest", "--json") == 0
    capsys.readouterr()

    files = stored_files(store)
    assert len(files) == 2
    newest = json.loads(files[-1].read_text(encoding="utf-8"))

    assert "reading_compare" not in newest
    assert "reading_chain" not in newest
    assert "reading_store" not in newest
    # same corpus read twice: the two files describe the same reading
    assert files[-1].stat().st_size < files[0].stat().st_size * 1.3


def test_the_screen_says_which_file_holds_this_reading_and_which_one_it_compared(
    tmp_path, capsys
):
    cache_path, root = corpus(tmp_path)
    store = tmp_path / "readings"
    assert run(cache_path, root, "--store", str(store), "--manifest") == 0
    first_text = capsys.readouterr().out
    first_name = stored_files(store)[0].name
    assert "store" in first_text
    assert first_name in first_text
    # no prior yet: the screen says so instead of comparing with nothing
    assert "no stored reading" in first_text

    assert run(cache_path, root, "--store", str(store), "--manifest") == 0
    text = capsys.readouterr().out

    assert "store" in text
    assert "stored" in text
    assert first_name in text
    assert all(len(line) <= 80 for line in text.splitlines())
