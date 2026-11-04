"""A baseline recorded from one read is not a witness that the vendor added fields.

r91 taught the tool that a single short read is not a vendor deletion. The mirror
case stayed open (README LIMIT): the *first* read at a brand-new version has no
same-version record to be checked against, so if that read is short it becomes
that version's baseline untested - and every later complete read then prints
`schema_changed` / ADDED (N), which is the tool asserting a vendor addition from a
record nobody ever confirmed.

A receipt that grew cannot be a truncated read of the record it grew past, so the
fuller one is the better baseline. But "grew" has two explanations - a vendor
added the field, or our earlier record was short - and one unconfirmed record
cannot separate them. That is `possible_short_baseline`, not `schema_changed`.
"""

import json

import quotamirror

SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"


def receipt(extra=None):
    payload = {
        "version": 5,
        "dailyModelTokensVersion": 5,
        "lastComputedDate": "2026-11-03",
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": 10,
                "costUSD": 0.0,
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
    payload.update(extra or {})
    return payload


def without(payload, *keys):
    short = dict(payload)
    for key in keys:
        short.pop(key, None)
    return short


def read(tmp_path, payload, store_name="shapes.json"):
    """Report first, then record - the order the CLI uses."""
    path = tmp_path / "stats-cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    store_file = tmp_path / store_name
    store = quotamirror.load_store(store_file)
    report = quotamirror.drift_report(store, path, payload)
    quotamirror.record_shape(store_file, path, payload)
    return report, store_file


def entry_of(store_file, key="5/5"):
    stored = json.loads(store_file.read_text(encoding="utf-8"))
    versions = next(iter(stored["caches"].values()))["versions"]
    return versions[key]


def test_a_fuller_read_after_a_short_first_read_is_not_a_vendor_addition(tmp_path):
    """First read at a version is short; the next read is complete."""
    short = without(receipt(), "modelUsage", "hourCounts")
    read(tmp_path, short)

    payload, _ = read(tmp_path, receipt())
    within = payload["within_version"]

    assert within["verdict"] == "possible_short_baseline"
    assert within["added"]  # named, but as unconfirmed additions
    assert within["removed"] == []


def test_the_fuller_read_becomes_the_baseline_and_counts_as_one_read(tmp_path):
    short = without(receipt(), "modelUsage", "hourCounts")
    read(tmp_path, short)

    _, store_file = read(tmp_path, receipt())
    entry = entry_of(store_file)

    assert len(entry["shape"]) == len(quotamirror.shape_of(receipt()))
    assert entry["reads"] == 1


def test_a_second_identical_read_confirms_the_baseline(tmp_path):
    short = without(receipt(), "modelUsage", "hourCounts")
    read(tmp_path, short)
    read(tmp_path, receipt())

    payload, store_file = read(tmp_path, receipt())

    assert payload["within_version"]["verdict"] == "unchanged"
    assert entry_of(store_file)["reads"] == 2


def test_growth_against_a_twice_read_baseline_is_still_schema_changed(tmp_path):
    """The r91 guard, narrowed to the case where the record was confirmed."""
    full = receipt()
    read(tmp_path, full)
    read(tmp_path, full)  # same shape twice: the baseline is confirmed

    grown = receipt(extra={"newTotals": {"byDay": 1}})
    payload, _ = read(tmp_path, grown)

    assert payload["within_version"]["verdict"] == "schema_changed"
    assert payload["within_version"]["added"]


def test_a_baseline_that_neither_gained_only_nor_lost_only_is_schema_changed(tmp_path):
    """Not a subset either way: both records moved, so neither explanation fits."""
    read(tmp_path, receipt())

    swapped = without(receipt(), "hourCounts")
    swapped["lastComputedDateAlt"] = "2026-11-03"
    payload, _ = read(tmp_path, swapped)

    assert payload["within_version"]["verdict"] == "schema_changed"


def test_a_legacy_baseline_without_a_read_count_is_not_reinterpreted(tmp_path):
    """No read count means no claim about the record - keep the old verdict."""
    path = tmp_path / "stats-cache.json"
    path.write_text(json.dumps(without(receipt(), "hourCounts")), encoding="utf-8")
    store_file = tmp_path / "shapes.json"
    legacy = without(receipt(), "hourCounts")
    store_file.write_text(
        json.dumps(
            {
                "schema": 3,
                "caches": {
                    quotamirror.path_identity(path): {
                        "versions": {
                            "5/5": {
                                "shape": quotamirror.shape_of(legacy),
                                "presence": quotamirror.shape_presence(legacy),
                                "at": "2026-11-01T00:00:00Z",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = quotamirror.load_store(store_file)
    payload = quotamirror.drift_report(store, path, receipt())

    assert payload["within_version"]["verdict"] == "schema_changed"


def test_the_cross_version_axis_does_not_call_it_a_vendor_addition(tmp_path):
    """The same read feeds both axes; a suspect baseline must silence the addition."""
    short = without(receipt(), "modelUsage", "hourCounts")
    read(tmp_path, short)  # version 5, recorded from one short read
    v6_short = dict(short)
    v6_short["version"] = 6
    v6_short["dailyModelTokensVersion"] = 6
    read(tmp_path, v6_short)  # version 6's first read is short too

    v6_full = dict(receipt())
    v6_full["version"] = 6
    v6_full["dailyModelTokensVersion"] = 6
    payload, _ = read(tmp_path, v6_full)

    assert payload["measurable"] is True  # the cross-version axis did run
    assert payload["verdict"] == "possible_short_baseline"
    assert payload["added"]


def test_a_clean_cross_version_comparison_is_not_downgraded(tmp_path):
    """A difference-free older baseline is evidence in its own right; keep it."""
    read(tmp_path, receipt())  # version 5, complete, one read
    v6_short = dict(without(receipt(), "modelUsage"))
    v6_short["version"] = 6
    v6_short["dailyModelTokensVersion"] = 6
    read(tmp_path, v6_short)

    v6_full = dict(receipt())
    v6_full["version"] = 6
    v6_full["dailyModelTokensVersion"] = 6
    payload, _ = read(tmp_path, v6_full)

    assert payload["within_version"]["verdict"] == "possible_short_baseline"
    assert payload["verdict"] == "unchanged"


def test_render_names_both_explanations_and_the_replaced_baseline(tmp_path):
    read(tmp_path, without(receipt(), "modelUsage", "hourCounts"))
    payload, _ = read(tmp_path, receipt())

    text = quotamirror.render_drift(quotamirror.redact_home(payload))

    assert "VERDICT: possible_short_baseline" in text
    assert "vendor addition" in text
    assert "One unconfirmed record cannot separate those two" in text
    assert "(unconfirmed)" in text


def test_the_short_baseline_block_stores_no_values(tmp_path):
    read(tmp_path, without(receipt(), "modelUsage", "hourCounts"))
    payload, _ = read(tmp_path, receipt())

    assert SENTINEL not in json.dumps(payload)
