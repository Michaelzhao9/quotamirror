"""A first read at a new version cannot name a removal either.

The same-version rule (tests/test_partial_read.py) says a strict subset under an
unchanged version is `possible_partial_read` until the same subset is seen twice.
It does not cover the upgrade case: version A has a full shape recorded, the
harness publishes version B, and the first read at B is shorter. `drift` used to
compare that one read against A's record and print `meter_field_removed` - a
vendor deletion asserted from a single read, which is exactly what the r89 guard
forbids on the other axis.

The two explanations are not separable from one read: a vendor that dropped a
subtree and a read taken while the file was being rewritten produce the same
picture. So the first removal-only read at a new version is reported as
`removal_unconfirmed`, the paths are listed as unconfirmed, and the read is still
recorded - a second identical read at that version is what turns it into a
removal somebody can check.
"""

import json

import quotamirror

SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"


def receipt(version=5, daily_model_version=None):
    return {
        "version": version,
        "dailyModelTokensVersion": daily_model_version or version,
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
                "sessionCount": 7,
                "note": SENTINEL,
            }
        },
        "dailyActivity": [{"date": "2026-11-01", "messageCount": 3, "sessionCount": 1}],
        "totalSessions": 12,
        "totalMessages": 340,
        "hourCounts": {"9": 4},
    }


def without(payload, *keys):
    short = json.loads(json.dumps(payload))
    for key in keys:
        short.pop(key, None)
    return short


def store_at(tmp_path, payload, at="2026-11-01T00:00:00Z"):
    path = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    quotamirror.record_shape(store, path, payload, captured_at=at)
    return path, store


def removed_cost(payload):
    """The same receipt with one priced field gone from the only model."""
    short = json.loads(json.dumps(payload))
    del short["modelUsage"]["claude-sonnet-4-5"]["costUSD"]
    return short


# --- slice 1: the verdict --------------------------------------------------


def test_a_first_read_at_a_new_version_reports_a_removal_as_unconfirmed(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "removal_unconfirmed"


def test_a_second_identical_read_at_the_new_version_confirms_the_removal(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))
    quotamirror.record_shape(store, path, newer)

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "meter_field_removed"
    assert payload["meter_fields_removed"] == ["modelUsage.<model>.costUSD"]


# --- slice 2: the payload names both versions -----------------------------


def test_the_unconfirmed_payload_names_both_versions_and_the_path(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    part = payload["cross_version_unconfirmed"]
    assert part["baseline_version_key"] == "5/5"
    assert part["version_key"] == "6/6"
    assert part["fields_baseline"] == payload["fields_baseline"]
    assert part["fields_now"] == payload["fields_now"]
    assert part["fields_unconfirmed"] == ["modelUsage.<model>.costUSD"]
    assert part["priced_unconfirmed"] == ["modelUsage.<model>.costUSD"]
    assert SENTINEL not in json.dumps(part)


# --- slice 3: the guard is only for removals without additions -------------


def test_an_addition_alone_at_a_new_version_is_still_a_schema_change(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = receipt(version=6)
    newer["modelUsage"]["claude-sonnet-4-5"]["newCounter"] = 1

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "schema_changed"
    assert payload.get("cross_version_unconfirmed") is None


def test_a_read_that_both_adds_and_removes_is_not_an_unconfirmed_removal(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))
    newer["modelUsage"]["claude-sonnet-4-5"]["newCounter"] = 1

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    # The priced removal still wins the verdict, but a read that also grew cannot be
    # a truncated copy of the baseline, so the r89 guard has nothing to explain.
    assert payload["verdict"] == "meter_field_removed"
    assert payload.get("cross_version_unconfirmed") is None


def test_a_non_priced_removal_at_a_new_version_is_unconfirmed_too(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = json.loads(json.dumps(receipt(version=6)))
    newer.pop("hourCounts")

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "removal_unconfirmed"
    assert payload["cross_version_unconfirmed"]["priced_unconfirmed"] == []


def test_an_unchanged_shape_across_versions_is_still_unchanged(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))

    payload = quotamirror.drift_report(
        quotamirror.load_store(store), path, receipt(version=6)
    )

    assert payload["verdict"] == "unchanged"
    assert payload.get("cross_version_unconfirmed") is None


def test_a_same_version_shorter_read_still_reports_a_partial_read(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))

    payload = quotamirror.drift_report(
        quotamirror.load_store(store), path, removed_cost(receipt(version=5))
    )

    # One version on disk means no cross-version baseline at all; the same-version
    # axis is the one that carries the partial-read contract.
    assert payload["verdict"] == "no_baseline"
    assert payload["within_version"]["verdict"] == "possible_partial_read"
    assert payload.get("cross_version_unconfirmed") is None


# --- slice 4: the read is recorded, and the printed sentence ---------------


def test_the_unconfirmed_read_is_recorded_so_a_second_read_can_confirm(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))

    quotamirror.record_shape(store, path, newer)

    versions = quotamirror.load_store(store)["caches"][quotamirror.path_identity(path)][
        "versions"
    ]
    assert sorted(versions) == ["5/5", "6/6"]
    assert versions["6/6"]["reads"] == 1
    assert len(versions["6/6"]["shape"]) == len(quotamirror.shape_of(newer))


def test_the_printed_report_says_why_it_cannot_name_a_removal(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, newer)
    )

    assert "VERDICT: removal_unconfirmed" in text
    assert "modelUsage.<model>.costUSD" in text
    assert "unconfirmed" in text
    assert "meter_field_removed" not in text
    assert "run drift again" in text.lower()


def test_the_printed_report_after_the_second_read_names_the_removal(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = removed_cost(receipt(version=6))
    quotamirror.record_shape(store, path, newer)

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, newer)
    )

    assert "VERDICT: meter_field_removed" in text
    # The LIMIT paragraph names removal_unconfirmed as a thing the tool can say, so
    # only the verdict line is checked here, not the whole page.
    assert "VERDICT: removal_unconfirmed" not in text
