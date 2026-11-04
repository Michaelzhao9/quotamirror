"""Tests for quotamirror's receipt-schema drift sampler (`drift`).

The failure this exists for: a harness silently deletes a field from the usage
receipt it publishes, and every meter built on top of it keeps printing a number
of zero. opencode 2.2.0 is the reported case - one PR removed a same-typed
allowance field, and the consequence was "no spend at all" for a paid session.

All fixtures are synthetic. No fixture value is ever stored: the store keeps the
*shape* (paths and type names), never a value, which is the only reason a
snapshot of somebody's receipt can sit on disk at all.
"""

import json
import os

import quotamirror
from pathlib import Path


SENTINEL = "SENTINEL-VALUE-DO-NOT-STORE"


def receipt(**extra):
    base = {
        "version": 5,
        "dailyModelTokensVersion": 5,
        "lastComputedDate": "2026-10-25",
        "dailyActivity": [{"date": "2026-10-01", "messageCount": 3, "sessionCount": 1}],
        "dailyModelTokens": [
            {"date": "2026-10-01", "tokensByModel": {"claude-sonnet-4-5": 1200}}
        ],
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": 10,
                "outputTokens": 4,
                "costUSD": 0.0,
                "allowance": 0,
            }
        },
        "totalSessions": 12,
        "totalMessages": 340,
        "hourCounts": {"9": 4},
        "longestSession": {"duration": 60},
    }
    base.update(extra)
    return base


def test_shape_lists_paths_and_types_without_any_value():
    shape = quotamirror.shape_of(receipt())
    # a scalar leaf keeps its path and its type name, never its value
    assert shape["modelUsage.<model>.inputTokens"] == "number"
    assert shape["modelUsage.<model>.costUSD"] == "number"
    assert shape["totalMessages"] == "number"
    assert shape["lastComputedDate"] == "str"
    # containers are named, and list items are reachable
    assert shape["modelUsage"] == "object"
    assert shape["dailyActivity"] == "list"
    assert shape["dailyActivity[].messageCount"] == "number"
    # date-shaped keys are collapsed so the shape does not grow one entry per day
    assert shape["dailyModelTokens[].date"] == "str"
    assert shape["dailyModelTokens[].tokensByModel.<model>"] == "number"
    # nothing in the shape is a value
    assert SENTINEL not in json.dumps(shape)


def test_shape_collapses_date_keys_and_keeps_other_keys():
    shape = quotamirror.shape_of({"dailyModelTokens": {"2026-10-01": 5, "2026-10-02": 6}})
    assert shape["dailyModelTokens.<date>"] == "number"
    assert [p for p in shape if p.startswith("dailyModelTokens.")] == [
        "dailyModelTokens.<date>"
    ]


def test_shape_treats_int_and_float_as_one_number_type():
    # a price that parses as int 0 in one build and float 0.0 in the next is not
    # a schema change; only a change of kind (number -> null) is
    a = quotamirror.shape_of({"costUSD": 0})
    b = quotamirror.shape_of({"costUSD": 0.0})
    assert a == b == {"costUSD": "number"}


def test_shape_marks_null_distinctly_from_number():
    shape = quotamirror.shape_of({"costUSD": None})
    assert shape["costUSD"] == "null"


def test_shape_drops_values_even_when_they_are_nested_strings():
    payload = {"modelUsage": {"claude-sonnet-4-5": {"costUSD": 1.5}}, "note": SENTINEL}
    shape = quotamirror.shape_of(payload)
    assert SENTINEL not in json.dumps(shape)
    assert shape["note"] == "str"


# --- slice 2: the snapshot store -------------------------------------------


def test_missing_store_reads_as_empty(tmp_path):
    store = quotamirror.load_store(tmp_path / "shapes.json")
    assert store["schema"] == quotamirror.STORE_SCHEMA
    assert store["caches"] == {}


def test_record_keeps_only_normalized_shape_under_a_path_digest(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"

    quotamirror.record_shape(store_file, path, receipt(note=SENTINEL))

    raw = store_file.read_text(encoding="utf-8")
    assert str(path) not in raw
    assert "claude-sonnet-4-5" not in raw
    assert "captured_at" not in raw
    assert "captures" not in raw
    store = quotamirror.load_store(store_file)
    assert list(store["caches"]) == [quotamirror.path_identity(path)]
    entry = store["caches"][quotamirror.path_identity(path)]["versions"]["5/5"]
    assert entry["shape"] == quotamirror.shape_of(receipt(note=SENTINEL))
    assert set(entry) == {"shape", "presence", "presence_lists", "reads", "at"}
    assert "modelUsage.<model>.inputTokens" in entry["shape"]


def test_record_keeps_the_shape_and_no_value(tmp_path):
    path = tmp_path / "stats-cache.json"
    path.write_text(json.dumps(receipt(note=SENTINEL)), encoding="utf-8")
    store_file = tmp_path / "shapes.json"

    quotamirror.record_shape(store_file, path, receipt(note=SENTINEL), captured_at="2026-10-29T00:00:00Z")

    raw = store_file.read_text(encoding="utf-8")
    assert SENTINEL not in raw
    store = quotamirror.load_store(store_file)
    entry = store["caches"][quotamirror.path_identity(path)]["versions"]["5/5"]
    assert entry["shape"] == quotamirror.shape_of(receipt(note=SENTINEL))
    assert set(entry) == {"shape", "presence", "presence_lists", "reads", "at"}


def test_recording_the_same_version_twice_updates_one_entry(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    quotamirror.record_shape(store_file, path, receipt(), captured_at="2026-10-29T00:00:00Z")
    quotamirror.record_shape(store_file, path, receipt(), captured_at="2026-10-29T06:00:00Z")

    versions = quotamirror.load_store(store_file)["caches"][quotamirror.path_identity(path)]["versions"]
    assert list(versions) == ["5/5"]
    assert versions["5/5"]["at"] == "2026-10-29T06:00:00Z"


def test_store_file_is_private(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    quotamirror.record_shape(store_file, path, receipt(), captured_at="2026-10-29T00:00:00Z")
    assert (store_file.stat().st_mode & 0o777) == 0o600


def test_existing_store_is_replaced_atomically(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    quotamirror.record_shape(store_file, path, receipt())
    old_inode = store_file.stat().st_ino

    quotamirror.record_shape(store_file, path, receipt(version=6, dailyModelTokensVersion=6))

    assert store_file.stat().st_ino != old_inode
    assert json.loads(store_file.read_text(encoding="utf-8"))["schema"] == quotamirror.STORE_SCHEMA


def test_existing_store_is_corrected_to_private_mode(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    store_file.write_text(json.dumps({"schema": 1, "caches": {}}), encoding="utf-8")
    os.chmod(store_file, 0o644)

    quotamirror.record_shape(store_file, path, receipt())

    assert (store_file.stat().st_mode & 0o777) == 0o600


def test_schema_less_store_cannot_carry_legacy_private_keys_forward(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    store_file.write_text(
        json.dumps({"caches": {str(path): {"versions": {}}}}), encoding="utf-8"
    )

    quotamirror.record_shape(store_file, path, receipt())

    raw = store_file.read_text(encoding="utf-8")
    assert str(path) not in raw
    assert list(json.loads(raw)["caches"]) == [quotamirror.path_identity(path)]


def test_record_refuses_a_symlink_store_without_touching_its_target(tmp_path):
    path = tmp_path / "stats-cache.json"
    target = tmp_path / "target.json"
    target.write_text("private target", encoding="utf-8")
    store_file = tmp_path / "shapes.json"
    store_file.symlink_to(target)

    try:
        quotamirror.record_shape(store_file, path, receipt())
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("symlink store was accepted")

    assert target.read_text(encoding="utf-8") == "private target"


def test_two_version_fields_that_disagree_are_reported_without_stored_state(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    quotamirror.record_shape(
        store_file, path, receipt(version=5, dailyModelTokensVersion=4),
        captured_at="2026-10-29T00:00:00Z",
    )
    store = quotamirror.load_store(store_file)
    entry = store["caches"][quotamirror.path_identity(path)]["versions"]["5/4"]
    assert set(entry) == {"shape", "presence", "presence_lists", "reads", "at"}
    assert quotamirror.drift_report(store, path, receipt(version=5, dailyModelTokensVersion=4))["version_fields_disagree"] is True


# --- slice 3: the drift verdict --------------------------------------------


def _record(store_file, path, payload, at):
    quotamirror.record_shape(store_file, path, payload, captured_at=at)


def test_one_version_only_is_reported_as_unmeasurable(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-29T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, receipt())

    assert payload["verdict"] == "no_baseline"
    assert payload["measurable"] is False
    assert payload["baseline_version_key"] is None
    assert payload["versions_recorded"] == ["5/5"]


def test_a_newer_version_with_the_same_shape_is_unchanged(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-29T00:00:00Z")
    newer = receipt(version=6, dailyModelTokensVersion=6)
    _record(store_file, path, newer, "2026-10-30T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, newer)

    assert payload["verdict"] == "unchanged"
    assert payload["measurable"] is True
    assert payload["baseline_version_key"] == "5/5"
    assert payload["removed"] == []


def test_a_removed_non_meter_field_is_only_a_schema_change(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-29T00:00:00Z")
    newer = receipt(version=6, dailyModelTokensVersion=6)
    del newer["hourCounts"]
    del newer["longestSession"]
    _record(store_file, path, newer, "2026-10-30T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, newer)

    assert payload["verdict"] == "schema_changed"
    assert "hourCounts" in payload["removed"]
    assert payload["meter_fields_removed"] == []


def test_a_removed_meter_field_is_named_as_one(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-29T00:00:00Z")
    newer = receipt(version=6, dailyModelTokensVersion=6)
    del newer["modelUsage"]["claude-sonnet-4-5"]["costUSD"]
    _record(store_file, path, newer, "2026-10-30T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, newer)

    assert payload["verdict"] == "meter_field_removed"
    assert payload["meter_fields_removed"] == ["modelUsage.<model>.costUSD"]


def test_a_meter_field_that_turns_into_null_is_altered_not_unchanged(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-29T00:00:00Z")
    newer = receipt(version=6, dailyModelTokensVersion=6)
    newer["modelUsage"]["claude-sonnet-4-5"]["costUSD"] = None
    _record(store_file, path, newer, "2026-10-30T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, newer)

    assert payload["verdict"] == "meter_field_altered"
    assert payload["type_changed"] == [
        {
            "path": "modelUsage.<model>.costUSD",
            "was": "number",
            "now": "null",
        }
    ]


def test_baseline_is_the_most_recently_captured_other_version(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-01T00:00:00Z")
    v6 = receipt(version=6, dailyModelTokensVersion=6)
    _record(store_file, path, v6, "2026-10-02T00:00:00Z")
    v7 = receipt(version=7, dailyModelTokensVersion=7)
    del v7["hourCounts"]
    _record(store_file, path, v7, "2026-10-03T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, v7)

    assert payload["baseline_version_key"] == "6/6"
    assert payload["verdict"] == "schema_changed"


def test_baseline_order_survives_a_version_rollback_without_timestamps(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(version=7, dailyModelTokensVersion=7), "ignored")
    _record(store_file, path, receipt(version=5, dailyModelTokensVersion=5), "ignored")

    current = receipt(version=6, dailyModelTokensVersion=6)
    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, current)

    assert payload["baseline_version_key"] == "5/5"


def test_an_added_field_alone_still_counts_as_a_schema_change(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, path, receipt(), "2026-10-29T00:00:00Z")
    newer = receipt(version=6, dailyModelTokensVersion=6, cacheHealth={"broken": True})
    _record(store_file, path, newer, "2026-10-30T00:00:00Z")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, newer)

    assert payload["verdict"] == "schema_changed"
    assert payload["added"] == ["cacheHealth", "cacheHealth.broken"]
    assert payload["removed"] == []


def test_a_different_receipt_path_never_borrows_another_baseline(tmp_path):
    a = tmp_path / "a" / "stats-cache.json"
    b = tmp_path / "b" / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, a, receipt(), "2026-10-01T00:00:00Z")
    _record(store_file, b, receipt(version=9, dailyModelTokensVersion=9), "2026-10-02T00:00:00Z")

    payload = quotamirror.drift_report(
        quotamirror.load_store(store_file), b, receipt(version=10, dailyModelTokensVersion=10)
    )

    # b's own last version is 9/9, captured later than a's 5/5: the baseline must
    # come from b's own history, never from the newer-looking snapshot of another file
    assert payload["baseline_version_key"] == "9/9"
    assert payload["source"] == str(b)


def test_a_receipt_path_with_no_history_at_all_has_no_baseline(tmp_path):
    a = tmp_path / "a" / "stats-cache.json"
    b = tmp_path / "b" / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    _record(store_file, a, receipt(), "2026-10-01T00:00:00Z")

    payload = quotamirror.drift_report(
        quotamirror.load_store(store_file), b, receipt(version=10, dailyModelTokensVersion=10)
    )

    assert payload["verdict"] == "no_baseline"
    assert payload["measurable"] is False



# --- slice 4: the command --------------------------------------------------

import subprocess
import sys


def run_cli(args, **kwargs):
    script = str(Path(__file__).resolve().parent.parent / "quotamirror.py")
    return subprocess.run(
        [sys.executable, script, *args],
        capture_output=True, text=True, **kwargs
    )


def test_first_run_says_drift_is_unmeasurable_and_records_the_shape(tmp_path):
    cache = tmp_path / "stats-cache.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    store = tmp_path / "shapes.json"

    result = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "no_baseline" in result.stdout
    assert "unmeasurable" in result.stdout
    assert "unchanged" not in result.stdout.lower()
    assert store.exists()
    assert json.loads(store.read_text())["caches"][quotamirror.path_identity(cache)]["versions"]["5/5"]


def test_second_version_names_the_removed_meter_field(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    assert run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path).returncode == 0

    newer = receipt(version=6, dailyModelTokensVersion=6)
    del newer["modelUsage"]["claude-sonnet-4-5"]["costUSD"]
    cache.write_text(json.dumps(newer), encoding="utf-8")
    result = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "meter_field_removed" in result.stdout
    assert "modelUsage.<model>.costUSD" in result.stdout
    assert "baseline: 5/5" in result.stdout


def test_json_payload_carries_the_verdict_and_no_value(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    cache.write_text(json.dumps(receipt(note=SENTINEL)), encoding="utf-8")
    run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    result = run_cli(
        ["drift", "--path", str(cache), "--store", str(store), "--json"], cwd=tmp_path
    )

    payload = json.loads(result.stdout)
    assert payload["verdict"] == "no_baseline"
    assert payload["version_key"] == "5/5"
    assert SENTINEL not in result.stdout
    assert SENTINEL not in store.read_text()


def test_the_source_cache_is_never_written(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)

    run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before


def test_missing_cache_exits_two(tmp_path):
    result = run_cli(
        ["drift", "--path", str(tmp_path / "nope.json"), "--store", str(tmp_path / "s.json")],
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_a_store_that_is_not_a_store_exits_two(tmp_path):
    cache = tmp_path / "stats-cache.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    store = tmp_path / "shapes.json"
    store.write_text("[1, 2, 3]", encoding="utf-8")

    result = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.returncode == 2
    assert "not a quotamirror shape store" in result.stderr


def test_a_removed_meter_field_is_listed_once_not_twice(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)
    newer = receipt(version=6, dailyModelTokensVersion=6)
    del newer["modelUsage"]["claude-sonnet-4-5"]["costUSD"]
    cache.write_text(json.dumps(newer), encoding="utf-8")

    result = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.stdout.count("modelUsage.<model>.costUSD") == 1
    assert "! modelUsage.<model>.costUSD" in result.stdout



# --- one version can move on its own -----------------------------------------
#
# The across-version sampler compares the current receipt with a shape recorded at
# a *different* version, so a harness that deletes a field without bumping
# `version` is invisible. The store has always kept one shape per version and the
# command rewrote that shape before anyone could diff against it, so the second
# axis is a read-before-write: whatever sits in the store is the baseline.


def _within(payload):
    return payload["within_version"]


def test_the_recorded_shape_carries_when_it_was_seen(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"

    quotamirror.record_shape(store_file, path, receipt(), captured_at="2026-11-01T00:00:00Z")

    entry = quotamirror.load_store(store_file)["caches"][quotamirror.path_identity(path)]["versions"]["5/5"]
    assert entry == {
        "shape": quotamirror.shape_of(receipt()),
        "presence": quotamirror.shape_presence(receipt()),
        # schema 4: presence also counts list members, and says so
        "presence_lists": True,
        # schema 6: one read is a record nobody has agreed with yet
        "reads": 1,
        "at": "2026-11-01T00:00:00Z",
    }


def test_a_within_version_change_is_visible_against_the_store_written_before_it(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    before = receipt()
    after = receipt()
    del after["modelUsage"]["claude-sonnet-4-5"]["costUSD"]

    quotamirror.record_shape(store_file, path, before, captured_at="2026-11-01T00:00:00Z")
    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, after)

    within = _within(payload)
    assert within["measurable"] is True
    # 0.27.0: a single smaller read is evidence, not a verdict - it is also what a
    # mid-write read looks like. The removal is named once the same shape is read
    # twice (see tests/test_partial_read.py), so the first read stops here.
    assert within["verdict"] == "possible_partial_read"
    assert within["meter_fields_removed"] == ["modelUsage.<model>.costUSD"]
    assert within["partial_read"]["confirmed"] is False
    assert within["baseline_at"] == "2026-11-01T00:00:00Z"
    # the across-version axis is still unmeasured, and says so separately
    assert payload["verdict"] == "no_baseline"


def test_an_unchanged_shape_names_the_time_it_was_recorded(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"

    quotamirror.record_shape(store_file, path, receipt(), captured_at="2026-11-01T00:00:00Z")
    payload = quotamirror.drift_report(
        quotamirror.load_store(store_file), path, receipt()
    )

    within = _within(payload)
    assert within["measurable"] is True
    assert within["verdict"] == "unchanged"
    assert within["same_as_recorded_at"] == "2026-11-01T00:00:00Z"


def test_a_store_written_before_timestamps_existed_is_still_comparable(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"
    legacy = {
        "schema": quotamirror.STORE_SCHEMA,
        "caches": {
            quotamirror.path_identity(path): {
                "versions": {"5/5": {"shape": quotamirror.shape_of(receipt())}}
            }
        },
    }
    store_file.write_text(json.dumps(legacy), encoding="utf-8")

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, receipt())

    within = _within(payload)
    assert within["measurable"] is True
    # the shape axis is comparable, but this entry predates presence counts, so
    # "unchanged" would be an all-clear over an axis nobody recorded
    assert within["verdict"] == "unchanged_shape_only"
    assert within["presence_compared"] is False
    assert within["baseline_at"] is None


def test_a_version_with_no_recorded_shape_is_not_measured(tmp_path):
    path = tmp_path / "stats-cache.json"
    store_file = tmp_path / "shapes.json"

    payload = quotamirror.drift_report(quotamirror.load_store(store_file), path, receipt())

    within = _within(payload)
    assert within["measurable"] is False
    assert within["verdict"] == "not_measured"
    assert within["reason"] == "no_shape_recorded_at_this_version"
    assert within["removed"] == [] and within["meter_fields_removed"] == []


def test_cli_second_run_names_a_meter_field_removed_without_a_version_change(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    assert run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path).returncode == 0

    after = receipt()
    del after["modelUsage"]["claude-sonnet-4-5"]["costUSD"]
    cache.write_text(json.dumps(after), encoding="utf-8")
    first = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)
    assert "possible_partial_read" in first.stdout

    # two agreeing reads are what a half-written file cannot produce
    result = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "SINCE THE LAST SHAPE AT THIS VERSION (5/5)" in result.stdout
    assert "meter_field_removed" in result.stdout
    assert "WITHOUT a version change" in result.stdout
    assert result.stdout.count("modelUsage.<model>.costUSD") == 1


def test_cli_first_run_does_not_print_within_version_counts(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "shapes.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")

    result = run_cli(["drift", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert "SINCE THE LAST SHAPE AT THIS VERSION (5/5)" in result.stdout
    assert "not measured" in result.stdout
    assert "removed 0" not in result.stdout
