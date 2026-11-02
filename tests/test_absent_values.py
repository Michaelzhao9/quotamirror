"""Tests for absence rendered as a value or as an all-clear.

Three separate commands each printed a claim about a set that was empty, or a
Python value for a field that was not in the file:

* `revise` printed the literal text ``None`` where a date belongs, because the
  field was simply absent from the receipt.
* `revise` promised that "the second reading is what makes a revision visible"
  after recording 0 keys, and then a second read printed ``net +0 tokens`` over
  0 compared keys - a sum over nothing, reported as a total.
* `units` printed ``no capacity field is zero for every model`` while auditing
  zero models: an all-clear issued over a set it never read.

The rule these three share is the one the tool already states elsewhere: a zero
and a total must both declare the population they were taken over, and an absent
field is not a value. All fixtures are synthetic.
"""

import json

import quotamirror


def entry(tokens=10, **extra):
    base = {
        "inputTokens": tokens,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "webSearchRequests": 0,
        "costUSD": 0.0,
    }
    base.update(extra)
    return base


def cache(models=None, last="2026-10-25", day="2026-10-20", tokens_by_model=None):
    receipt = {
        "version": 5,
        "dailyActivity": [{"date": day, "messageCount": 3, "sessionCount": 1}],
        "dailyModelTokens": [],
        "modelUsage": models if models is not None else {},
    }
    if last is not None:
        receipt["lastComputedDate"] = last
    if tokens_by_model:
        receipt["dailyModelTokens"].append(
            {"date": day, "tokensByModel": tokens_by_model}
        )
    return receipt


# --- slice 1: an absent field is not the string "None" ---------------------


def test_revise_does_not_print_python_none_for_an_absent_date():
    receipt = cache(models={"m-a": entry()}, last=None)

    text = quotamirror.render_revise(quotamirror.revise_report({}, "cache.json", receipt))

    assert "None" not in text
    assert "lastComputedDate: not recorded" in text


def test_revise_names_an_absent_date_in_both_readings(tmp_path):
    earlier = cache(models={"m-a": entry()}, last=None, tokens_by_model={"m-a": 10})
    later = cache(models={"m-a": entry()}, last=None, tokens_by_model={"m-a": 20})
    store_path = tmp_path / "readings.json"
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(later), encoding="utf-8")
    quotamirror.record_reading(store_path, cache_path, earlier, recorded_at="2026-11-01T00:00:00Z")

    payload = quotamirror.revise_report(quotamirror.load_readings(store_path), cache_path, later)

    text = quotamirror.render_revise(payload)
    assert payload["measurable"] is True
    assert "None" not in text
    assert text.count("not recorded") >= 2


# --- slice 2: a first reading of zero keys makes no promise ----------------

def test_first_reading_of_zero_keys_does_not_promise_a_revision():
    receipt = cache(models={})  # no modelUsage entries, no token numbers anywhere

    text = quotamirror.render_revise(quotamirror.revise_report({}, "cache.json", receipt))

    assert "recorded: 0 keys" in text
    assert "the second reading is what makes" not in text
    assert "no monotone token counter" in text


def test_comparison_of_zero_keys_does_not_total_nothing(tmp_path):
    receipt = cache(models={})
    store_path = tmp_path / "readings.json"
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps(receipt), encoding="utf-8")
    quotamirror.record_reading(store_path, cache_path, receipt, recorded_at="2026-11-01T00:00:00Z")

    payload = quotamirror.revise_report(
        quotamirror.load_readings(store_path), cache_path, receipt
    )
    text = quotamirror.render_revise(payload)

    assert payload["compared"] == 0
    assert "net" in text
    assert "net 0 tokens" not in text
    assert "nothing to sum" in text


# --- slice 3: the capacity all-clear carries its denominator ---------------


def test_capacity_all_clear_names_how_many_models_it_audited():
    receipt = cache(models={"m-a": entry(tokens=10, contextWindow=200000)})

    text = quotamirror.render_units(quotamirror.audit_units(receipt))

    assert "none of 1 model" in text
    assert "no capacity field is zero for every model" not in text


def test_capacity_over_zero_models_claims_nothing():
    receipt = cache(models={}, tokens_by_model={"m-a": 10})

    text = quotamirror.render_units(quotamirror.audit_units(receipt))

    assert "0 modelUsage entries" in text
    assert "no capacity field is zero" not in text
    assert "none of" not in text


def test_capacity_over_models_without_tokens_is_not_audited():
    receipt = cache(models={"m-a": entry(tokens=0, contextWindow=0)})

    text = quotamirror.render_units(quotamirror.audit_units(receipt))

    assert "not audited" in text
    assert "no capacity field is zero" not in text
