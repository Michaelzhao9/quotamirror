"""Cycle: a receipt with no per-model entries must not report a lifetime total of zero.

A receipt can be recognisable (it carries `dailyActivity` / `dailyModelTokens`) and
still hold no `modelUsage` at all - a half-written cache, or a receipt whose priced
field was renamed by a vendor. In that state the sum over `modelUsage` is an empty
sum, not a measurement, so `scan` must say so instead of printing `TOTAL 0`.

Fixtures are synthetic; no real user data is stored in this repository.
"""

from test_scan import build_cache, day, usage

import quotamirror


def render(cache, source="/tmp/qm-fixture/stats-cache.json"):
    return quotamirror.render_text(quotamirror.summarize(cache), source=source)


def tokens_section(text):
    start = text.index("TOKENS")
    return text[start : text.index("\n\n", start)]


# --- 1. no model entries -> the total is not measured -----------------------


def test_total_is_not_measured_when_the_receipt_holds_no_model_entries():
    cache = build_cache(
        models=None,
        daily=[],
        activity=[{"date": "2026-10-01", "messageCount": 3, "sessionCount": 1, "toolCallCount": 2}],
    )
    cache.pop("modelUsage")

    payload = quotamirror.summarize(cache)

    assert payload["tokens"]["measured"] is False
    assert "modelUsage" in payload["tokens"]["reason"]
    text = render(cache)
    assert "not measured" in tokens_section(text)
    assert "TOTAL" not in tokens_section(text)


# --- 2. the day view may still hold the number -> name it -------------------


def test_scan_names_the_day_total_when_only_the_day_view_carries_tokens():
    cache = build_cache(
        models={"": usage()},  # empty model entry: present, records nothing
        daily=[day("2026-10-01", ghost=1000), day("2026-10-02", ghost=234)],
        activity=[{"date": "2026-10-01", "messageCount": 1, "sessionCount": 1, "toolCallCount": 0}],
    )
    cache.pop("modelUsage")

    payload = quotamirror.summarize(cache)
    text = render(cache)

    assert payload["tokens"]["measured"] is False
    assert "1,234" in tokens_section(text)
    assert "2 day(s)" in tokens_section(text)


# --- 3. no activity days -> no all-clear, and no bare None in the range -----


def test_scan_does_not_claim_full_coverage_over_zero_activity_days():
    text = render(build_cache(models={}, daily=[], activity=[]))

    assert "every activity day has a token record" not in text
    assert "none recorded in this receipt" in text
    assert "None" not in text


# --- 4. a receipt that does carry models keeps the measured total -----------


def test_measured_receipt_still_prints_its_total():
    cache = build_cache(models={"claude-opus": usage(input_tokens=10, output_tokens=90)})

    payload = quotamirror.summarize(cache)
    text = render(cache)

    assert payload["tokens"]["measured"] is True
    assert payload["tokens"]["reason"] is None
    assert "TOTAL" in tokens_section(text)
    assert "100" in tokens_section(text)
