"""A narrowing at an UNCHANGED version cannot be named from one read either.

`tests/test_cross_version_narrowing.py` closed the upgrade case: a field that
drops from SOME members of a family, first read at a brand-new version, is
`narrowing_unconfirmed`. The same-version axis was left open and the tool's own
LIMIT paragraph said so: there the guard tests whether the path SET shrank, and
a narrowing leaves it intact, so one read printed `meter_field_narrowed` as a
finding about the vendor.

The same one-read ambiguity applies at an unchanged version. A receipt written
incrementally - two of three model objects emitted - produces exactly a narrower
family with no version change. So a narrowing-only read against this version's
own last record reports `possible_narrowing`, keeps the older presence as the
baseline and lists both counts; the same narrower counts read a second time are
what become the record, and a third read is `unchanged`.

A read that also GREW is still named: a truncated copy cannot carry a path its
source did not have.
"""

import json

import quotamirror

SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"
COST = "modelUsage.<model>.costUSD"


def receipt(version=5, models=2):
    names = ["claude-sonnet-4-5", "claude-opus-5-5", "claude-haiku-4-6"][:models]
    return {
        "version": version,
        "dailyModelTokensVersion": version,
        "lastComputedDate": "2026-11-03",
        "modelUsage": {
            name: {
                "inputTokens": 10,
                "outputTokens": 4,
                "costUSD": 0.0,
                "contextWindow": 0,
                "note": SENTINEL,
            }
            for name in names
        },
        "dailyActivity": [{"date": "2026-11-01", "messageCount": 3, "sessionCount": 1}],
        "totalSessions": 12,
        "totalMessages": 340,
        "hourCounts": {"9": 4},
    }


def narrowed(payload, carriers):
    """Keep `costUSD` on `carriers` of the models, leaving every path in place."""
    short = json.loads(json.dumps(payload))
    for index, name in enumerate(short["modelUsage"]):
        if index >= carriers:
            del short["modelUsage"][name]["costUSD"]
    return short


def store_at(tmp_path, payload, at="2026-11-01T00:00:00Z"):
    path = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    quotamirror.record_shape(store, path, payload, captured_at=at)
    return path, store


def within(tmp_path, store, path, payload):
    return quotamirror.drift_report(quotamirror.load_store(store), path, payload)[
        "within_version"
    ]


# --- slice 1: the verdict --------------------------------------------------


def test_a_first_narrowing_at_an_unchanged_version_is_not_named(tmp_path):
    path, store = store_at(tmp_path, receipt())
    report = within(tmp_path, store, path, narrowed(receipt(), 1))

    assert report["verdict"] == "possible_narrowing"
    assert report["confirmed_narrowing"] is False


def test_the_same_narrower_counts_twice_at_this_version_become_the_record(tmp_path):
    path, store = store_at(tmp_path, receipt())
    suspect = narrowed(receipt(), 1)
    quotamirror.record_shape(store, path, suspect)

    report = within(tmp_path, store, path, suspect)

    assert report["verdict"] == "meter_field_narrowed"
    assert report["confirmed_narrowing"] is True
    assert report["meter_fields_narrowed"] == [COST]


def test_the_settled_narrow_record_reads_as_unchanged_afterwards(tmp_path):
    path, store = store_at(tmp_path, receipt())
    suspect = narrowed(receipt(), 1)
    for _ in range(2):
        quotamirror.record_shape(store, path, suspect)

    report = within(tmp_path, store, path, suspect)

    assert report["verdict"] == "unchanged"
    assert report["narrowed"] == []


# --- slice 2: the payload names both counts and the price ------------------


def test_the_suspect_payload_carries_both_counts_and_the_price_marker(tmp_path):
    path, store = store_at(tmp_path, receipt())
    payload = quotamirror.drift_report(
        quotamirror.load_store(store), path, narrowed(receipt(), 1)
    )

    part = payload["within_version"]["possible_narrowing"]
    assert part["rows"] == [
        {"path": COST, "family": "<model>", "was": [2, 2], "now": [1, 2]}
    ]
    assert part["priced_unconfirmed"] == [COST]
    assert part["fields_baseline"] == payload["within_version"]["fields_baseline"]
    assert part["recorded_at"] == "2026-11-01T00:00:00Z"
    assert SENTINEL not in json.dumps(payload)


# --- slice 3: the older presence stays the baseline ------------------------


def test_the_first_suspicion_keeps_the_older_presence(tmp_path):
    path, store = store_at(tmp_path, receipt())
    quotamirror.record_shape(store, path, narrowed(receipt(), 1))

    entry = json.loads(store.read_text())["caches"][quotamirror.path_identity(path)][
        "versions"
    ]["5/5"]
    assert entry["presence"][COST] == [2, 2]
    assert entry["pending_narrowed"]["rows"] == 1
    assert entry["pending_narrowed"]["presence_digest"]


def test_counts_that_keep_moving_are_never_confirmed(tmp_path):
    path, store = store_at(tmp_path, receipt(models=3))
    # The real sequence is report-then-record, so each simulated run does both.
    two = narrowed(receipt(models=3), 2)
    first = within(tmp_path, store, path, two)
    quotamirror.record_shape(store, path, two)

    report = within(tmp_path, store, path, narrowed(receipt(models=3), 1))

    assert first["verdict"] == "possible_narrowing"
    assert first["possible_narrowing"]["rows"][0]["now"] == [2, 3]
    # The counts moved again, so nothing about 2 of 3 confirms 1 of 3.
    assert report["verdict"] == "possible_narrowing"
    assert report["confirmed_narrowing"] is False
    assert report["possible_narrowing"]["rows"][0]["now"] == [1, 3]
    assert report["possible_narrowing"]["rows"][0]["was"] == [3, 3]


# --- slice 4: growth still excludes the guard ------------------------------


def test_a_read_that_also_adds_a_path_is_still_named(tmp_path):
    path, store = store_at(tmp_path, receipt())
    quotamirror.record_shape(store, path, receipt())  # two agreeing reads
    moving = narrowed(receipt(), 1)
    moving["modelUsage"]["claude-sonnet-4-5"]["newCounter"] = 1

    report = within(tmp_path, store, path, moving)

    # It added a path, so it cannot be a truncated copy of the baseline.
    assert report["verdict"] == "meter_field_narrowed"
    assert report["possible_narrowing"] is None


# --- slice 5: a known version is not reported as an upgrade ----------------

def test_a_narrowing_at_a_known_version_is_not_a_cross_version_finding(tmp_path):
    path, store = store_at(tmp_path, receipt(version=5))
    quotamirror.record_shape(store, path, receipt(version=6))

    payload = quotamirror.drift_report(
        quotamirror.load_store(store), path, narrowed(receipt(version=6), 1)
    )

    assert payload["within_version"]["verdict"] == "possible_narrowing"
    assert payload["verdict"] == "possible_narrowing"
    assert payload.get("cross_version_unconfirmed") is None


# --- slice 6: the printed sentences --------------------------------------


def test_the_printed_report_does_not_name_the_narrowing_it_refuses(tmp_path):
    path, store = store_at(tmp_path, receipt())
    text = quotamirror.render_drift(
        quotamirror.drift_report(
            quotamirror.load_store(store), path, narrowed(receipt(), 1)
        )
    )

    assert "VERDICT: possible_narrowing" in text
    assert "! " + COST + ": 1 of 2 model(s) (was 2 of 2) (unconfirmed)" in text
    assert "run drift again" in text.lower()
    # A definite NARROWED block, and the undercount sentence that follows it,
    # would name the very thing the verdict refuses.
    assert "NARROWED (" not in text
    assert "undercounts" not in text
    assert "VERDICT: meter_field_narrowed" not in text


def test_the_printed_confirming_read_says_the_counts_were_seen_twice(tmp_path):
    path, store = store_at(tmp_path, receipt())
    suspect = narrowed(receipt(), 1)
    quotamirror.record_shape(store, path, suspect)
    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, suspect)
    )

    assert "VERDICT: meter_field_narrowed" in text
    assert "twice at this version" in text
