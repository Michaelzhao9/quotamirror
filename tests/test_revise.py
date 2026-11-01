"""Tests for quotamirror's two-reading revision witness (`revise`).

The failure this exists for: a usage receipt does not only describe the present.
The same key, read twice, can carry a *smaller* number the second time - the
vendor recomputed history, or a page that had been showing a larger figure was
corrected. Three independent reporters in one window saw exactly that (a profile
page 62.87B -> 38.76B, a desktop app running lower every day for three days, a
history bucket that shrank while saying it returns "the saved numbers").

Read once, every one of those looks like "the meter is broken". Read twice, it
is one thing: the past is mutable. So the contract is not "report a number" -
it is read the same key twice, print both readings and the signed difference,
and never call a downward revision a drop in usage. Those are different facts.

All fixtures are synthetic.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import quotamirror


def receipt(tokens=1200, model=(10, 4), last="2026-10-25", day="2026-10-20", **extra):
    base = {
        "version": 5,
        "dailyModelTokensVersion": 5,
        "lastComputedDate": last,
        "dailyActivity": [{"date": day, "messageCount": 3, "sessionCount": 1}],
        "dailyModelTokens": [
            {"date": day, "tokensByModel": {"claude-sonnet-4-5": tokens}}
        ],
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": model[0],
                "outputTokens": model[1],
                "costUSD": 0.0,
            }
        },
        "totalSessions": 12,
        "totalMessages": 340,
    }
    base.update(extra)
    return base


MODEL_KEY = "model:claude-sonnet-4-5"
DAY_KEY = "day:2026-10-20:claude-sonnet-4-5"


def run_cli(args, **kwargs):
    script = str(Path(__file__).resolve().parent.parent / "quotamirror.py")
    return subprocess.run(
        [sys.executable, script, *args], capture_output=True, text=True, **kwargs
    )


# --- slice 1: what a reading is, and where it lives ------------------------


def test_reading_lists_lifetime_model_totals_and_day_cells():
    reading = quotamirror.reading_of(receipt())
    # a lifetime per-model total is the sum of its token buckets
    assert reading["values"][MODEL_KEY] == 14
    # a day cell is the value the receipt attributes to that day and model
    assert reading["values"][DAY_KEY] == 1200
    assert reading["version_key"] == "5/5"
    assert reading["last_computed"] == "2026-10-25"


def test_reading_ignores_fields_that_are_not_token_counts():
    reading = quotamirror.reading_of(receipt())
    assert all(isinstance(value, int) for value in reading["values"].values())
    # message/session counts are not in this reading: the keys must be
    # comparable across readings, and a count of messages is not a token total
    assert not [key for key in reading["values"] if "total" in key]


def test_recorded_reading_keeps_numbers_under_a_path_digest(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"

    quotamirror.record_reading(store_file, cache, receipt(), recorded_at="2026-10-31T18:00:00Z")

    raw = store_file.read_text(encoding="utf-8")
    assert str(cache) not in raw
    assert os.stat(store_file).st_mode & 0o777 == 0o600
    store = quotamirror.load_readings(store_file)
    readings = store["caches"][quotamirror.path_identity(cache)]["readings"]
    assert len(readings) == 1
    assert readings[0]["at"] == "2026-10-31T18:00:00Z"
    assert readings[0]["values"][DAY_KEY] == 1200


def test_reading_history_is_bounded_and_keeps_the_newest(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"
    for index in range(1, 8):
        quotamirror.record_reading(
            store_file, cache, receipt(tokens=1000 + index), recorded_at=f"2026-10-31T18:0{index}:00Z"
        )
    readings = quotamirror.load_readings(store_file)["caches"][
        quotamirror.path_identity(cache)
    ]["readings"]
    assert len(readings) == quotamirror.MAX_READINGS
    assert readings[-1]["values"][DAY_KEY] == 1007
    assert readings[0]["values"][DAY_KEY] == 1007 - quotamirror.MAX_READINGS + 1


def test_recording_refuses_a_symlinked_store(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "readings.json"
    link.symlink_to(target)
    try:
        quotamirror.record_reading(link, tmp_path / "stats-cache.json", receipt())
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("a symlinked store must be refused")


# --- slice 2: two readings of the same keys --------------------------------


def test_first_reading_has_no_baseline_and_says_so():
    payload = quotamirror.revise_report(
        {"schema": 1, "caches": {}}, Path("/tmp/none.json"), receipt()
    )
    assert payload["measurable"] is False
    assert payload["verdict"] == "no_baseline"
    assert payload["keys_now"] == 2
    assert payload["rows"] == []


def test_a_downward_key_is_a_revision_not_a_usage_drop(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"
    quotamirror.record_reading(store_file, cache, receipt(tokens=1200), recorded_at="2026-10-31T18:00:00Z")
    store = quotamirror.load_readings(store_file)

    payload = quotamirror.revise_report(store, cache, receipt(tokens=900))

    row = [r for r in payload["rows"] if r["key"] == DAY_KEY][0]
    assert (row["earlier"], row["now"], row["delta"]) == (1200, 900, -300)
    assert row["verdict"] == "revised_down"
    assert payload["verdict"] == "history_revised"
    assert payload["down"] == 1 and payload["up"] == 0 and payload["flat"] == 1
    assert payload["net_delta"] == -300


def test_an_identical_reading_reports_no_change(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"
    quotamirror.record_reading(store_file, cache, receipt(), recorded_at="2026-10-31T18:00:00Z")

    payload = quotamirror.revise_report(quotamirror.load_readings(store_file), cache, receipt())

    assert payload["verdict"] == "unchanged"
    assert payload["compared"] == 2
    assert payload["down"] == 0 and payload["up"] == 0
    assert {row["verdict"] for row in payload["rows"]} == {"unchanged"}


def test_a_key_missing_from_one_side_is_not_read_as_zero(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"
    quotamirror.record_reading(store_file, cache, receipt(), recorded_at="2026-10-31T18:00:00Z")
    smaller = receipt()
    smaller["modelUsage"] = {}
    smaller["dailyModelTokens"] = []

    payload = quotamirror.revise_report(quotamirror.load_readings(store_file), cache, smaller)

    assert payload["compared"] == 0
    assert sorted(payload["vanished"]) == sorted([MODEL_KEY, DAY_KEY])
    assert payload["appeared"] == []
    # a receipt that stops publishing a key has not revised it downwards
    assert payload["verdict"] == "not_comparable"
    assert payload["net_delta"] == 0


def test_a_closed_day_is_marked_because_no_new_work_can_land_on_it(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"
    earlier = receipt(tokens=1200, day="2026-10-20", last="2026-10-25")
    earlier["dailyModelTokens"].append(
        {"date": "2026-10-30", "tokensByModel": {"claude-sonnet-4-5": 10}}
    )
    quotamirror.record_reading(
        store_file, cache, earlier, recorded_at="2026-10-31T18:00:00Z"
    )
    later = receipt(tokens=1300, day="2026-10-20", last="2026-10-31")
    later["dailyModelTokens"].append(
        {"date": "2026-10-30", "tokensByModel": {"claude-sonnet-4-5": 40}}
    )

    payload = quotamirror.revise_report(quotamirror.load_readings(store_file), cache, later)

    by_key = {row["key"]: row for row in payload["rows"]}
    # the earlier receipt already declared 2026-10-20 computed, so a change there
    # can only be the entry being rewritten; 2026-10-30 was still open
    assert by_key[DAY_KEY]["closed"] is True
    assert by_key[DAY_KEY]["delta"] == 100
    assert by_key["day:2026-10-30:claude-sonnet-4-5"]["closed"] is False
    assert by_key["day:2026-10-30:claude-sonnet-4-5"]["delta"] == 30
    assert payload["closed_day_changes"] == 1


def test_closed_day_sentence_agrees_with_its_count(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store_file = tmp_path / "readings.json"

    def payload_for(days):
        earlier = receipt(day="2026-10-01", last="2026-10-25")
        for index, day in enumerate(days, start=1):
            earlier["dailyModelTokens"].append(
                {"date": day, "tokensByModel": {"claude-sonnet-4-5": 10}}
            )
        quotamirror.record_reading(store_file, cache, earlier, recorded_at="2026-10-31T18:00:00Z")
        later = receipt(day="2026-10-01", last="2026-10-31")
        for day in days:
            later["dailyModelTokens"].append(
                {"date": day, "tokensByModel": {"claude-sonnet-4-5": 20}}
            )
        return quotamirror.revise_report(quotamirror.load_readings(store_file), cache, later)

    one = quotamirror.render_revise(payload_for(["2026-10-02"]))
    assert "1 changed key sits on a day" in one
    assert "key(s)" not in one

    (store_file).unlink()
    two = quotamirror.render_revise(payload_for(["2026-10-02", "2026-10-03"]))
    assert "2 changed keys sit on days" in two


# --- slice 3: the command --------------------------------------------------


def test_cli_first_run_records_the_reading_and_says_it_is_unmeasurable(tmp_path):
    cache = tmp_path / "stats-cache.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")
    store = tmp_path / "readings.json"

    result = run_cli(["revise", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "first reading" in result.stdout
    assert quotamirror.load_readings(store)["caches"]


def test_cli_second_run_prints_both_readings_and_the_signed_difference(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "readings.json"
    cache.write_text(json.dumps(receipt(tokens=1200)), encoding="utf-8")
    assert run_cli(["revise", "--path", str(cache), "--store", str(store)], cwd=tmp_path).returncode == 0
    cache.write_text(json.dumps(receipt(tokens=900)), encoding="utf-8")

    result = run_cli(["revise", "--path", str(cache), "--store", str(store)], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "1,200" in result.stdout and "900" in result.stdout
    assert "-300" in result.stdout
    assert "history_revised" in result.stdout
    low = result.stdout.lower()
    for forbidden in ("usage drop", "usage fell", "dropped usage", "usage decreased"):
        assert forbidden not in low
    assert "revised" in low and "rewritten" in low


def test_cli_no_record_leaves_the_store_untouched(tmp_path):
    cache = tmp_path / "stats-cache.json"
    store = tmp_path / "readings.json"
    cache.write_text(json.dumps(receipt()), encoding="utf-8")

    result = run_cli(
        ["revise", "--path", str(cache), "--store", str(store), "--no-record"], cwd=tmp_path
    )

    assert result.returncode == 0, result.stderr
    assert not store.exists()
    assert "no earlier reading" in result.stdout


def test_cli_exits_2_when_the_receipt_cannot_be_read(tmp_path):
    result = run_cli(["revise", "--path", str(tmp_path / "absent.json"), "--store", str(tmp_path / "r.json")])
    assert result.returncode == 2
