"""Presence counts for list members, not only for keyed families.

The limit recorded in 0.25.0: `shape_of` collapses every `dailyActivity[]` member
into one path, so a field that one day carries and thirty-seven do not reads as
"present", never as "1 of 38". Models were counted in the previous release;
list members were not, and the printed LIMIT said so.

Counting them is the same shape of work, and it has one trap the model axis did
not: a list whose length changes on its own. A day appended that lacks the field
does not remove a carrier, and a day dropped by retention is not the vendor
dropping a field, so neither may be reported as narrowing.
"""

import json

import quotamirror


def receipt(days=3, drop_field=None, extra_day=None):
    """A receipt whose daily list looks alike, minus an optional hole."""
    activity = []
    for index in range(days):
        body = {
            "date": f"2026-10-{index + 1:02d}",
            "messageCount": 5,
            "sessionCount": 1,
            "toolCallCount": 7,
        }
        if drop_field and index == drop_field[0]:
            body.pop(drop_field[1])
        activity.append(body)
    if extra_day:
        activity.append(extra_day)
    return {
        "version": 5,
        "dailyModelTokensVersion": 5,
        "dailyActivity": activity,
        "totalSessions": 3,
    }


def recorded_store(tmp_path, payload):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    quotamirror.record_shape(store_file, path, payload)
    return path, store_file


def within_of(payload):
    return payload["within_version"]


def test_presence_counts_how_many_list_members_carry_a_path():
    presence = quotamirror.shape_presence(receipt(drop_field=(1, "toolCallCount")))
    assert presence["dailyActivity[].toolCallCount"] == [2, 3]
    assert presence["dailyActivity[].messageCount"] == [3, 3]


def test_a_field_dropped_from_one_day_is_a_narrowed_field(tmp_path):
    path, store_file = recorded_store(tmp_path, receipt())
    fewer = receipt(drop_field=(1, "toolCallCount"))
    path.write_text(json.dumps(fewer), encoding="utf-8")

    first = quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    # The list axis is ambiguous on one read for the same reason the map axis is: an
    # incrementally written array looks like a narrowed field.
    assert within_of(first)["verdict"] == "possible_narrowing"
    quotamirror.record_shape(store_file, path, fewer)

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    within = within_of(payload)

    assert within["verdict"] == "field_narrowed"
    assert within["removed"] == []
    assert within["narrowed"] == [
        {
            "path": "dailyActivity[].toolCallCount",
            "family": "list:dailyActivity",
            "was": [3, 3],
            "now": [2, 3],
        }
    ]
    assert within["removed"] == []


def test_a_list_that_grew_without_the_field_removes_no_carrier(tmp_path):
    path, store_file = recorded_store(tmp_path, receipt(days=3))
    longer = receipt(
        days=3, extra_day={"date": "2026-10-04", "messageCount": 5, "sessionCount": 1}
    )
    path.write_text(json.dumps(longer), encoding="utf-8")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, longer)

    # three carriers before, three after: a new member that lacks the field is
    # not a field that lost one
    assert within_of(payload)["narrowed"] == []
    assert within_of(payload)["verdict"] == "unchanged"


def test_a_list_that_shrank_by_retention_is_not_called_narrowing(tmp_path):
    path, store_file = recorded_store(tmp_path, receipt(days=4))
    shorter = receipt(days=3)
    path.write_text(json.dumps(shorter), encoding="utf-8")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, shorter)

    # every path lost exactly one carrier because a member left the window
    assert within_of(payload)["narrowed"] == []
    assert within_of(payload)["verdict"] == "unchanged"


def test_a_baseline_without_list_counts_leaves_that_axis_unmeasured(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    payload = receipt()
    path.write_text(json.dumps(payload), encoding="utf-8")
    store_file.write_text(
        json.dumps(
            {
                "schema": quotamirror.STORE_SCHEMA,
                "caches": {
                    quotamirror.path_identity(path): {
                        "versions": {
                            "5/5": {
                                "shape": quotamirror.shape_of(payload),
                                # presence recorded by 0.25.0: keyed families only
                                "presence": {"dailyActivity[].toolCallCount": [3, 3]},
                                "at": "2026-11-01T00:00:00Z",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = quotamirror.drift_report(quotamirror.load_store(store_file), path, payload)
    within = within_of(result)

    assert within["presence_compared"] is True
    assert within["list_presence_compared"] is False
    assert within["presence_axes_unmeasured"] == ["list"]
    assert within["verdict"] == "unchanged_shape_only"
    text = quotamirror.render_drift(result)
    assert "list" in text.lower()
    assert "unmeasured, not clean" in text


def test_the_rendered_narrowing_names_the_list(tmp_path):
    path, store_file = recorded_store(tmp_path, receipt())
    fewer = receipt(drop_field=(1, "toolCallCount"))
    path.write_text(json.dumps(fewer), encoding="utf-8")
    quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    quotamirror.record_shape(store_file, path, fewer)

    text = quotamirror.render_drift(
        quotamirror.drift_report(quotamirror.load_store(store_file), path, fewer)
    )

    assert "VERDICT: field_narrowed" in text
    assert "dailyActivity[].toolCallCount: 2 of 3 dailyActivity member(s) (was 3 of 3)" in text
