"""A first read at a new version cannot name a narrowing either.

`tests/test_cross_version_removal.py` closed half of the upgrade case: a removal
(whole paths gone) seen once at a brand-new version is `removal_unconfirmed`.
The other half stayed open, and the tool's own LIMIT paragraph said so: a field
that drops from SOME members of a family - `costUSD` on 1 of 2 models - leaves
the path set intact, so the removal guard had nothing to catch, and the first
read at the new version printed `meter_field_narrowed` as a finding about the
vendor.

The evidence is the same one read. A vendor that narrowed a field and a read
taken while the file was being rewritten differ in nothing that a single
snapshot can show: an incremental writer that has emitted two of three model
entries produces exactly a narrower family. So the first narrowing-only read at
a new version reports `narrowing_unconfirmed`, lists the rows with their two
counts and does not name a narrowing; a second identical read at that version
(same shape digest) is what makes it `meter_field_narrowed`.

A read that also GREW is still named on the first read: a truncated copy cannot
carry a path its source did not have.
"""

import json

import quotamirror

SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"


def receipt(version=5):
    """Two models so one of them can lose a field while the family stays whole."""
    return {
        "version": version,
        "dailyModelTokensVersion": version,
        "lastComputedDate": "2026-11-03",
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": 10,
                "outputTokens": 4,
                "costUSD": 0.0,
                "contextWindow": 0,
                "note": SENTINEL,
            },
            "claude-opus-5-5": {
                "inputTokens": 7,
                "outputTokens": 2,
                "costUSD": 0.0,
                "contextWindow": 0,
                "note": SENTINEL,
            },
        },
        "dailyActivity": [{"date": "2026-11-01", "messageCount": 3, "sessionCount": 1}],
        "totalSessions": 12,
        "totalMessages": 340,
        "hourCounts": {"9": 4},
    }


def narrowed_cost(payload):
    """The same receipt with one priced field gone from one of the two models."""
    short = json.loads(json.dumps(payload))
    del short["modelUsage"]["claude-sonnet-4-5"]["costUSD"]
    return short


def store_at(tmp_path, payload, at="2026-11-01T00:00:00Z"):
    path = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    quotamirror.record_shape(store, path, payload, captured_at=at)
    return path, store


# --- slice 1: the verdict --------------------------------------------------


def test_a_first_read_at_a_new_version_reports_a_narrowing_as_unconfirmed(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "narrowing_unconfirmed"


def test_a_second_identical_read_at_the_new_version_confirms_the_narrowing(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))
    quotamirror.record_shape(store, path, newer)

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "meter_field_narrowed"
    assert payload["meter_fields_narrowed"] == ["modelUsage.<model>.costUSD"]


# --- slice 2: the payload names both versions and both counts -------------


def test_the_unconfirmed_payload_names_both_versions_and_the_family_counts(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    part = payload["cross_version_unconfirmed"]
    assert part["baseline_version_key"] == "5/5"
    assert part["version_key"] == "6/6"
    # Nothing is recorded at 6/6 yet - the read itself is what gets recorded.
    assert part["reads_at_current"] is None
    assert part["narrowed_unconfirmed"] == [
        {
            "path": "modelUsage.<model>.costUSD",
            "family": "<model>",
            "was": [2, 2],
            "now": [1, 2],
        }
    ]
    assert part["priced_unconfirmed"] == ["modelUsage.<model>.costUSD"]
    assert SENTINEL not in json.dumps(part)


# --- slice 3: growth still excludes the guard -----------------------------


def test_a_read_that_also_adds_a_path_at_a_new_version_names_the_narrowing(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))
    newer["modelUsage"]["claude-sonnet-4-5"]["newCounter"] = 1

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    # It added a path, so it cannot be a truncated copy of the baseline.
    assert payload["verdict"] == "meter_field_narrowed"
    assert payload.get("cross_version_unconfirmed") is None


def test_a_read_that_both_removes_and_narrows_reports_both_as_unconfirmed(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))
    newer.pop("hourCounts")

    payload = quotamirror.drift_report(quotamirror.load_store(store), path, newer)

    assert payload["verdict"] == "removal_unconfirmed"
    part = payload["cross_version_unconfirmed"]
    assert part["fields_unconfirmed"] == ["hourCounts", "hourCounts.9"]
    assert [row["path"] for row in part["narrowed_unconfirmed"]] == [
        "modelUsage.<model>.costUSD"
    ]


def test_a_narrowing_read_at_the_same_version_is_measured_but_not_named_yet(tmp_path):
    # The other half of the upgrade case: with one version on disk the same-version
    # axis is the only record. A narrowing leaves the path set intact, so that axis
    # used to name it from one read. It now keeps the older counts as the baseline
    # and reports `possible_narrowing` (see tests/test_within_narrowing.py); the same
    # narrower counts read twice are what name it.
    path, store = store_at(tmp_path, receipt(version=5))

    payload = quotamirror.drift_report(
        quotamirror.load_store(store), path, narrowed_cost(receipt(version=5))
    )

    assert payload["verdict"] == "no_baseline"
    assert payload["within_version"]["verdict"] == "possible_narrowing"
    assert payload.get("cross_version_unconfirmed") is None


# --- slice 4: the printed sentence ----------------------------------------


def test_the_printed_report_says_why_it_cannot_name_a_narrowing(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, newer)
    )

    assert "VERDICT: narrowing_unconfirmed" in text
    assert "modelUsage.<model>.costUSD" in text
    assert "1 of 2 model(s)" in text
    assert "(was 2 of 2)" in text
    assert "unconfirmed" in text
    assert "run drift again" in text.lower()
    assert "VERDICT: meter_field_narrowed" not in text
    # A definite NARROWED block would name the very thing the verdict refuses.
    assert "NARROWED (" not in text


def test_the_printed_report_after_the_second_read_names_the_narrowing(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))
    quotamirror.record_shape(store, path, newer)

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, newer)
    )

    assert "VERDICT: meter_field_narrowed" in text
    assert "VERDICT: narrowing_unconfirmed" not in text
    assert "NARROWED (1)" in text


def test_the_unconfirmed_read_is_recorded_so_a_second_read_can_confirm(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    newer = narrowed_cost(receipt(version=6))

    quotamirror.record_shape(store, path, newer)

    versions = quotamirror.load_store(store)["caches"][quotamirror.path_identity(path)][
        "versions"
    ]
    assert sorted(versions) == ["5/5", "6/6"]
    assert versions["6/6"]["reads"] == 1
    assert len(versions["6/6"]["shape"]) == len(quotamirror.shape_of(newer))
