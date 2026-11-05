"""A carrier the record had lost, and this read has again.

r100 closed the same-version narrowing guard: a pure narrowing is
`possible_narrowing` until the same counts are read twice, and only then is it
`meter_field_narrowed`, with the narrow counts installed as the new baseline.
What that left behind is the mirror case. Once the narrow counts ARE the record,
a read in which the carrier is back compares as nothing at all:
`_narrowed_rows` only looks for counts that fell, so the diff is empty, the
verdict is `unchanged`, and the tool prints "no priced field lost a carrier" -
about the very field it had itself recorded as lost on 8 of 9 models, now
present on 9 of 9. The store still holds `narrowing_confirmed_at`.

So a presence gain at an unchanged version gets its own verdict. It is not a
claim that the vendor restored anything: a stale copy of the older bytes, and a
record that was wrong, look identical from here. It is a claim about the record -
this read contradicts the sentence the tool printed last time.
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


def confirmed_narrow_store(tmp_path, carriers=1, models=2):
    """A store whose record at this version is the narrow counts (read twice)."""
    path, store = store_at(tmp_path, receipt(models=models))
    suspect = narrowed(receipt(models=models), carriers)
    quotamirror.record_shape(store, path, suspect, captured_at="2026-11-02T00:00:00Z")
    quotamirror.record_shape(store, path, suspect, captured_at="2026-11-02T00:05:00Z")
    return path, store


def within(store, path, payload):
    return quotamirror.drift_report(quotamirror.load_store(store), path, payload)[
        "within_version"
    ]


# --- slice 1: the verdict ---------------------------------------------------


def test_a_carrier_the_record_had_lost_is_named_when_it_returns(tmp_path):
    path, store = confirmed_narrow_store(tmp_path)

    report = within(store, path, receipt())

    assert report["verdict"] == "meter_field_regained"
    assert report["meter_fields_regained"] == [COST]
    assert report["regained"] == [
        {"path": COST, "family": "<model>", "was": [1, 2], "now": [2, 2]}
    ]
    assert report["narrowed"] == []


def test_the_reversal_names_the_narrowing_it_contradicts(tmp_path):
    path, store = confirmed_narrow_store(tmp_path)

    report = within(store, path, receipt())

    assert report["reversed_narrowing_at"] == "2026-11-02T00:05:00Z"
    assert report["verdict"] == "meter_field_regained"


# --- slice 2: a bigger family is not a returning carrier --------------------


def test_a_new_family_member_is_not_a_returning_carrier(tmp_path):
    path, store = confirmed_narrow_store(tmp_path, carriers=1, models=2)

    # Three models now, all carrying the field: 1 of 2 -> 3 of 3. The family grew,
    # so the field came in with a new member rather than back onto an old one.
    report = within(store, path, receipt(models=3))

    assert report["regained"] == []
    assert report["verdict"] == "unchanged"


# --- slice 3: price, and the printed sentences -----------------------------


def test_the_clear_sentence_is_not_printed_on_a_reversal(tmp_path):
    path, store = confirmed_narrow_store(tmp_path)

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, receipt())
    )

    assert "VERDICT: meter_field_regained" in text
    assert "REGAINED (1)" in text
    assert "! modelUsage.<model>.costUSD: 2 of 2 model(s) (was 1 of 2)" in text
    # The old all-clear speaks for a record this read contradicts.
    assert "no priced field lost a carrier" not in text
    assert SENTINEL not in text


def test_the_verdict_does_not_claim_the_vendor_restored_anything(tmp_path):
    path, store = confirmed_narrow_store(tmp_path)

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, receipt())
    )

    assert "restored by the vendor" not in text
    assert "stale" in text


# --- slice 4: the fuller read is what the next run compares against --------


def test_the_full_shape_becomes_the_record_so_the_next_read_is_unchanged(tmp_path):
    path, store = confirmed_narrow_store(tmp_path)
    quotamirror.record_shape(store, path, receipt())

    report = within(store, path, receipt())

    assert report["verdict"] == "unchanged"
    assert report["regained"] == []
    assert report["same_as_recorded_at"] is not None


# --- slice 5: a regain is not allowed to hide a removal --------------------


def test_a_read_that_also_loses_a_field_keeps_the_removal_verdict(tmp_path):
    path, store = confirmed_narrow_store(tmp_path, carriers=2, models=3)
    fuller = receipt(models=3)
    # Remove the whole path, or the diff is a narrowing rather than a removal.
    for model in fuller["modelUsage"].values():
        del model["note"]

    report = within(store, path, fuller)

    assert report["regained"] == [
        {"path": COST, "family": "<model>", "was": [2, 3], "now": [3, 3]}
    ]
    assert report["removed"] == ["modelUsage.<model>.note"]
    # A shorter shape at this version is the partial-read guard, which outranks
    # every definite verdict - and must not swallow the measured regain.
    assert report["verdict"] == "possible_partial_read"

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store), path, fuller)
    )
    assert "possible_partial_read" in text
    assert "REGAINED (1)" in text
    assert "! modelUsage.<model>.costUSD: 3 of 3 model(s) (was 2 of 3)" in text
    assert "Contradicts the narrowing confirmed at 2026-11-02T00:05:00Z." in text
