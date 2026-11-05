"""Per-family presence in the drift shape.

The failure: `shape_of` collapses every model id into `<model>`, so a field that
a harness drops from ONE model and keeps on the other eight produces a shape
identical to before. Measured on this machine's real receipt (9 models): strip
`costUSD` from `claude-fable-5-1` and `drift` prints

    VERDICT: unchanged
    identical to the shape recorded ...: 0 removed, 0 added, 0 retyped,
    no priced field touched.

That is a false all-clear on the exact axis this tool exists for. The fix is to
record, next to the shape, how many members of each collapsed family carried
each path - so "present" can be read as "on 8 of 9 models" instead of "present".

Families are the two collapsed key spaces (`<model>` and `<date>`). List members
were the remaining hole; they are counted now and covered by
`tests/test_list_presence.py`. What survives here is the rendered limit for lists
that roll over on their own.
"""

import json

import quotamirror


def multi(models=9, drop_field=None, drop_model=None):
    """A receipt whose model families all look alike, minus an optional hole."""
    usage = {}
    for index in range(models):
        name = f"model-{index}"
        if name == drop_model:
            continue
        body = {"inputTokens": 10, "outputTokens": 4, "costUSD": 0.0}
        if drop_field and name == drop_field[0]:
            body.pop(drop_field[1])
        usage[name] = body
    return {
        "version": 5,
        "dailyModelTokensVersion": 5,
        "modelUsage": usage,
        "totalSessions": 3,
    }


def recorded_store(tmp_path, receipt):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    quotamirror.record_shape(store_file, path, receipt)
    return path, store_file


def test_presence_counts_how_many_models_carry_a_path():
    presence = quotamirror.shape_presence(multi(drop_field=("model-3", "costUSD")))
    assert presence["modelUsage.<model>.costUSD"] == [8, 9]
    assert presence["modelUsage.<model>.inputTokens"] == [9, 9]


def test_presence_also_counts_a_date_keyed_family():
    receipt = {
        "dailyModelTokens": {
            "2026-10-01": {"tokens": 1},
            "2026-10-02": {"tokens": 2, "extra": 3},
        }
    }
    presence = quotamirror.shape_presence(receipt)
    assert presence["dailyModelTokens.<date>.tokens"] == [2, 2]
    assert presence["dailyModelTokens.<date>.extra"] == [1, 2]


def test_a_field_dropped_from_one_model_is_a_narrowed_meter_not_unchanged(tmp_path):
    path, store_file = recorded_store(tmp_path, multi())
    fewer = multi(drop_field=("model-3", "costUSD"))
    path.write_text(json.dumps(fewer), encoding="utf-8")

    first = quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    # One read cannot separate a vendor narrowing from an incrementally written
    # receipt, so the first read keeps the baseline and only reports the counts.
    assert first["within_version"]["verdict"] == "possible_narrowing"
    quotamirror.record_shape(store_file, path, fewer)

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    within = payload["within_version"]

    assert within["verdict"] == "meter_field_narrowed"
    assert within["meter_fields_narrowed"] == ["modelUsage.<model>.costUSD"]
    assert within["removed"] == []
    assert within["narrowed"] == [
        {
            "path": "modelUsage.<model>.costUSD",
            "family": "<model>",
            "was": [9, 9],
            "now": [8, 9],
        }
    ]


def test_dropping_a_whole_model_is_not_called_a_narrowed_field(tmp_path):
    path, store_file = recorded_store(tmp_path, multi())
    fewer = multi(drop_model="model-3")
    path.write_text(json.dumps(fewer), encoding="utf-8")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    within = payload["within_version"]

    # every field lost one carrier because a family member left, not because a
    # field was dropped - the shape is genuinely unchanged
    assert within["narrowed"] == []
    assert within["verdict"] == "unchanged"


def test_the_store_keeps_presence_counts_beside_the_shape(tmp_path):
    path, store_file = recorded_store(tmp_path, multi(drop_field=("model-3", "costUSD")))
    entry = quotamirror.load_store(store_file)["caches"][quotamirror.path_identity(path)][
        "versions"
    ]["5/5"]
    assert entry["presence"]["modelUsage.<model>.costUSD"] == [8, 9]
    assert set(entry) == {"shape", "presence", "presence_lists", "reads", "at"}


def test_upgrading_a_schema_2_store_keeps_its_baseline(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    digest = quotamirror.path_identity(path)
    store_file.write_text(
        json.dumps(
            {
                "schema": 2,
                "caches": {
                    digest: {
                        "versions": {
                            "4/4": {
                                "shape": {"totalSessions": "number"},
                                "at": "2026-10-01T00:00:00Z",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    quotamirror.record_shape(store_file, path, multi())

    store = quotamirror.load_store(store_file)
    assert store["schema"] == quotamirror.STORE_SCHEMA
    assert "4/4" in store["caches"][digest]["versions"]


def test_a_baseline_recorded_before_presence_cannot_claim_unchanged(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    receipt = multi(drop_field=("model-3", "costUSD"))
    path.write_text(json.dumps(receipt), encoding="utf-8")
    digest = quotamirror.path_identity(path)
    store_file.write_text(
        json.dumps(
            {
                "schema": 2,
                "caches": {
                    digest: {
                        "versions": {
                            "5/5": {
                                "shape": quotamirror.shape_of(multi()),
                                "at": "2026-10-01T00:00:00Z",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, receipt)
    within = payload["within_version"]

    assert within["presence_compared"] is False
    assert within["verdict"] == "unchanged_shape_only"
    text = quotamirror.render_drift(payload)
    assert "presence was not recorded for the baseline" in text


def test_the_rendered_drift_names_how_many_models_kept_the_field(tmp_path):
    path, store_file = recorded_store(tmp_path, multi())
    fewer = multi(drop_field=("model-3", "costUSD"))
    path.write_text(json.dumps(fewer), encoding="utf-8")
    # Named only after the same narrower counts have been read twice (see
    # tests/test_within_narrowing.py); the rendering of the confirmed read is what
    # this test is about.
    quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    quotamirror.record_shape(store_file, path, fewer)

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    )

    assert "VERDICT: meter_field_narrowed" in text
    assert "modelUsage.<model>.costUSD: 8 of 9 model(s) (was 9 of 9)" in text
    # the limit that is still true has to survive the fix: a list that rolls over
    # can lose a carrier without the vendor having dropped anything
    assert "rolls over" in text.lower()
    assert "collapsed into one path" not in text
