"""The cross-version axis has no regain policy: a field that came back reads as a difference.

`README` LIMIT, verbatim: "the cross-version axis has no regain policy at all, so a
field that the *other* version's record shows on 8 of 9 models and this one holds on
9 of 9 is reported as the ordinary difference, not as `REGAINED`."

That is the asymmetry these tests close. The same-version axis already has a regain
verdict (`_regained_rows`), and `_shape_diff` grew a `detect_regain` switch when it
was added - but `drift_report` never flips it, so the older version's record can only
ever subtract carriers, never return them.

What a cross-version regain may and may not claim:

- it may say the carrier count went up while the family size stayed the same, because
  those are the two numbers both records carry;
- it may NOT say the vendor restored the field. A record taken while the file was
  being written looks exactly like a narrowed one, and the tool cannot re-read the old
  version to find out. So the word has to be different from the same-version verdict:
  `field_regained_across_versions`, printed with "across versions" and the baseline's
  read count, so nobody reads it as a reversal witnessed inside one version;
- a family that grew is still not a regain (the existing guard): a new model arriving
  with the field raises both counts, and counting that as a returned carrier would
  invent a reversal.
"""

import json

import quotamirror

SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"


def receipt(version=5, carriers=("claude-sonnet-4-5", "claude-opus-5-5")):
    return {
        "version": version,
        "dailyModelTokensVersion": version,
        "lastComputedDate": "2026-11-03",
        "modelUsage": {
            model: {
                "inputTokens": 10,
                "outputTokens": 4,
                "costUSD": 0.0,
                "contextWindow": 0,
                "note": SENTINEL,
            }
            for model in carriers
        },
        "dailyActivity": [{"date": "2026-11-01", "messageCount": 3, "sessionCount": 1}],
        "totalSessions": 12,
        "totalMessages": 340,
        "hourCounts": {"9": 4},
    }


def without_cost(version, model_to_strip):
    """One priced field missing from one of two models: the family stays whole."""
    body = receipt(version=version)
    del body["modelUsage"][model_to_strip]["costUSD"]
    return body


def store_with(recorded, tmp_path, reads=None):
    cache = tmp_path / "stats-cache.json"
    store_path = tmp_path / "shapes.json"
    quotamirror.record_shape(store_path, cache, recorded)
    if reads is not None:
        raw = json.loads(store_path.read_text())
        versions = next(iter(raw["caches"].values()))["versions"]
        for entry in versions.values():
            entry["reads"] = reads
        store_path.write_text(json.dumps(raw))
    return quotamirror.load_store(store_path), cache


def test_a_carrier_count_that_rose_across_versions_is_named_with_its_basis(tmp_path):
    store, cache = store_with(without_cost(4, "claude-opus-5-5"), tmp_path)
    payload = quotamirror.drift_report(store, cache, receipt(version=5))

    # costUSD is a priced path, so the meter word wins - the same precedence the
    # same-version axis already applies. My first draft asserted the plain word and
    # the meter list beside it, which is a contradiction in the test, not the code.
    assert payload["verdict"] == "meter_field_regained_across_versions"
    paths = [row["path"] for row in payload["regained"]]
    assert paths == ["modelUsage.<model>.costUSD"], paths
    row = payload["regained"][0]
    assert row["was"] == [1, 2] and row["now"] == [2, 2], row
    assert payload["regain_basis"] == "cross_version"
    assert payload["meter_fields_regained"] == ["modelUsage.<model>.costUSD"]


def test_the_same_version_axis_keeps_its_own_word_and_basis(tmp_path):
    store, cache = store_with(without_cost(5, "claude-opus-5-5"), tmp_path)
    payload = quotamirror.drift_report(store, cache, receipt(version=5))

    within = payload["within_version"]
    assert within["verdict"] == "meter_field_regained", within["verdict"]
    assert within["regain_basis"] == "same_version"
    assert [row["path"] for row in within["regained"]] == ["modelUsage.<model>.costUSD"]
    # the two axes must not share a word: one is a witnessed reversal, the other is
    # a difference between two records
    assert within["verdict"] != "meter_field_regained_across_versions"


def test_a_family_that_grew_is_still_not_a_regain(tmp_path):
    store, cache = store_with(receipt(version=4), tmp_path)
    payload = quotamirror.drift_report(store, cache, receipt(version=5, carriers=(
        "claude-sonnet-4-5",
        "claude-opus-5-5",
        "claude-haiku-5",
    )))

    assert payload["regained"] == [], payload["regained"]
    assert payload["verdict"] != "field_regained_across_versions"


def test_a_non_meter_carrier_returning_does_not_borrow_the_meter_word(tmp_path):
    store, cache = store_with(without_cost(4, "claude-opus-5-5"), tmp_path)
    payload = quotamirror.drift_report(store, cache, receipt(version=5))
    plain = [row for row in payload["regained"] if not row["path"].endswith("costUSD")]
    assert plain == [], plain


def test_the_render_line_says_across_versions_and_prints_the_baseline_reads(tmp_path):
    store, cache = store_with(without_cost(4, "claude-opus-5-5"), tmp_path, reads=1)
    payload = quotamirror.drift_report(store, cache, receipt(version=5))
    text = quotamirror.render_drift(payload)

    assert "REGAINED ACROSS VERSIONS (1)" in text, text
    assert "read once" in text or "1 read" in text, text
