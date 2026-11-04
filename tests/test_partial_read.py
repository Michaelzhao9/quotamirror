"""A shorter read is not a vendor deletion until the same shorter read is seen twice.

The defect this exists for was measured on the tool itself (probe r89, steps
A/B/C on one cache path): record a full receipt, then read the same file while it
holds valid JSON but a missing subtree. `drift` printed `meter_field_removed`,
naming a priced field as deleted by the vendor - and then, because recording
follows reporting, it replaced the baseline with the short read. Restoring the
full file then printed ten ADDED fields that nobody had added.

Both halves are wrong in the same way: a truncated read was treated as evidence
about the vendor, and then became the record that would have refuted it. The only
thing that separates a half-written file from a real deletion is repetition, so
the rule here is: a strict subset under an unchanged version is reported as
`possible_partial_read`, the baseline is kept, and the same subset seen again is
what makes the removal real.
"""

import json

import quotamirror

SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"


def receipt():
    """A full receipt with several priced fields under one model."""
    return {
        "version": 5,
        "dailyModelTokensVersion": 5,
        "lastComputedDate": "2026-11-03",
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": 10,
                "outputTokens": 4,
                "cacheReadInputTokens": 3,
                "cacheCreationInputTokens": 2,
                "webSearchRequests": 1,
                "costUSD": 0.0,
                "contextWindow": 0,
                "maxOutputTokens": 0,
                "allowance": 0,
                "sessionCount": 7,
            }
        },
        "dailyActivity": [{"date": "2026-11-01", "messageCount": 3, "sessionCount": 1}],
        "dailyModelTokens": [
            {"date": "2026-11-01", "tokensByModel": {"claude-sonnet-4-5": 1200}}
        ],
        "totalSessions": 12,
        "totalMessages": 340,
        "hourCounts": {"9": 4},
        "longestSession": {"duration": 60, "note": SENTINEL},
    }


def without(payload, *keys):
    """The same receipt with whole subtrees missing - a valid, shorter document."""
    short = dict(payload)
    for key in keys:
        short.pop(key, None)
    return short


def read(tmp_path, payload, store_name="shapes.json"):
    """What the CLI does, in order: load the store, report, then record.

    Reporting before recording is the whole point - the shape in the store at
    report time is the baseline, and the write must not be able to destroy it.
    """
    path = tmp_path / "stats-cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store_file = tmp_path / store_name
    store = quotamirror.load_store(store_file)
    report = quotamirror.drift_report(store, path, payload)
    quotamirror.record_shape(store_file, path, payload)
    return report, store_file


def entry_of(store_file):
    stored = json.loads(store_file.read_text(encoding="utf-8"))
    versions = next(iter(stored["caches"].values()))["versions"]
    return versions["5/5"]


def test_a_shorter_same_version_read_does_not_replace_the_baseline(tmp_path):
    full = receipt()
    _, store_file = read(tmp_path, full)
    fields_baseline = len(entry_of(store_file)["shape"])

    read(tmp_path, without(full, "modelUsage"))

    entry = entry_of(store_file)
    assert len(entry["shape"]) == fields_baseline
    assert entry["pending_partial"]["fields"] < fields_baseline


def test_a_shorter_read_is_reported_as_possible_not_as_a_vendor_removal(tmp_path):
    full = receipt()
    read(tmp_path, full)

    payload, _ = read(tmp_path, without(full, "modelUsage"))
    within = payload["within_version"]

    assert within["verdict"] == "possible_partial_read"
    assert within["partial_read"]["confirmed"] is False
    # the vanished priced field is still named - as unconfirmed evidence, not a verdict
    assert within["meter_fields_removed"]
    assert within["partial_read"]["fields_now"] < within["partial_read"]["fields_baseline"]


def test_a_suspected_read_is_not_used_to_accuse_the_vendor_across_versions(tmp_path):
    """The same short read, compared with an older version's baseline: still no verdict."""
    old = receipt()
    read(tmp_path, old)

    newer = dict(receipt())
    newer["version"] = 6
    newer["dailyModelTokensVersion"] = 6
    read(tmp_path, newer)

    short = dict(without(newer, "modelUsage"))
    short["version"] = 6
    short["dailyModelTokensVersion"] = 6
    payload, store_file = read(tmp_path, short)

    assert payload["measurable"] is True  # the cross-version axis did run
    assert payload["verdict"] == "possible_partial_read"
    # and the version-5 baseline is untouched by the version-6 suspicion
    assert len(entry_of(store_file)["shape"]) == len(quotamirror.shape_of(newer))


def test_restoring_the_full_read_after_a_suspected_partial_is_unchanged(tmp_path):
    """A/B/C: full, short, full. The old behaviour made C print ten ADDED fields."""
    full = receipt()
    read(tmp_path, full)
    read(tmp_path, without(full, "modelUsage"))

    payload, _ = read(tmp_path, full)
    within = payload["within_version"]

    assert within["verdict"] == "unchanged"
    assert within["added"] == []
    assert within["removed"] == []


def test_the_same_shorter_read_twice_makes_the_removal_real(tmp_path):
    full = receipt()
    short = without(full, "modelUsage")
    read(tmp_path, full)
    read(tmp_path, short)  # suspicion only

    payload, store_file = read(tmp_path, short)  # the same shape again
    within = payload["within_version"]

    assert within["verdict"] == "meter_field_removed"
    assert within["partial_read"]["confirmed"] is True
    entry = entry_of(store_file)
    assert len(entry["shape"]) == len(quotamirror.shape_of(short))
    assert "pending_partial" not in entry


def test_a_different_shorter_read_does_not_count_as_confirmation(tmp_path):
    full = receipt()
    read(tmp_path, full)
    read(tmp_path, without(full, "modelUsage"))

    payload, store_file = read(tmp_path, without(full, "dailyModelTokens"))
    within = payload["within_version"]

    assert within["verdict"] == "possible_partial_read"
    assert len(entry_of(store_file)["shape"]) == len(quotamirror.shape_of(full))


def test_a_same_version_read_with_a_new_field_is_still_schema_changed(tmp_path):
    """Guard: the rule is about subsets. A read that grew is a change, not a truncation."""
    full = receipt()
    read(tmp_path, full)

    grown = dict(full)
    grown["newTotals"] = {"byDay": 1}
    payload, store_file = read(tmp_path, grown)

    assert payload["within_version"]["verdict"] == "schema_changed"
    assert "pending_partial" not in entry_of(store_file)


def test_the_pending_challenge_stores_no_field_names_and_no_values(tmp_path):
    """A pending read is not a shape. Keeping it small keeps the store's promise."""
    full = receipt()
    read(tmp_path, full)

    _, store_file = read(tmp_path, without(full, "modelUsage"))
    pending = entry_of(store_file)["pending_partial"]

    assert set(pending) == {"shape_digest", "fields", "at"}
    assert SENTINEL not in json.dumps(pending)


def test_render_says_the_baseline_was_kept_and_what_would_confirm_it(tmp_path):
    full = receipt()
    read(tmp_path, full)
    payload, _ = read(tmp_path, without(full, "modelUsage"))

    text = quotamirror.render_drift(quotamirror.redact_home(payload))

    assert "possible_partial_read" in text
    assert "baseline kept" in text
    assert "twice" in text or "second" in text
