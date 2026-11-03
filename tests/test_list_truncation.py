"""A list that stops is not a list that ended.

Measured in this tool's own output, not in a fixture: `nested --cache` printed

    38 day(s) are listed by the cache with no transcript here: 2026-05-03, ...
    (8 dates, then the line ends)

and `compare` printed `claude only : 42 (six dates, then `)`)`. Both lines name a
count and then print fewer items than the count, with nothing marking the gap -
so the printed list reads as the whole list. Two other sites marked the gap with
a bare `...` and no number, which says "something was left out" but not how much.

There is one fact here, so there is one sentence: `_sample(items, limit)` prints
the first `limit` items and, only when it truncated, `+N more`.
"""

import quotamirror

NINE_DAYS = [f"2026-05-{day:02d}" for day in range(1, 10)]


def overlap(days_claude_only):
    return {
        "coverage": "shared_days",
        "note": "",
        "days_both": ["2026-01-01"],
        "days_codex_only": [],
        "days_claude_only": days_claude_only,
    }


# --- the shared sentence ---------------------------------------------------


def test_sample_states_how_many_items_it_did_not_print():
    assert quotamirror._sample(NINE_DAYS, 6) == (
        "2026-05-01, 2026-05-02, 2026-05-03, 2026-05-04, 2026-05-05, 2026-05-06, +3 more"
    )


def test_sample_adds_nothing_when_the_list_fits():
    assert quotamirror._sample(NINE_DAYS[:6], 6) == (
        "2026-05-01, 2026-05-02, 2026-05-03, 2026-05-04, 2026-05-05, 2026-05-06"
    )
    assert quotamirror._sample([], 6) == ""


# --- site 1: compare's DAY OVERLAP block -----------------------------------


def test_overlap_block_says_how_many_days_it_left_out():
    lines = []
    quotamirror._render_overlap_block("DAY OVERLAP", overlap(NINE_DAYS), lines)
    text = "\n".join(lines)
    assert "claude only : 9 (" in text
    assert "+3 more" in text
    # the first six are printed, the seventh is not printed at all
    assert NINE_DAYS[5] in text
    assert NINE_DAYS[6] not in text


def test_overlap_block_prints_every_day_when_none_are_left_out():
    lines = []
    quotamirror._render_overlap_block("DAY OVERLAP", overlap(NINE_DAYS[:6]), lines)
    text = "\n".join(lines)
    assert "more" not in text
    assert NINE_DAYS[5] in text


# --- site 2: revise's appeared list ----------------------------------------


def test_revise_says_how_many_keys_appeared_that_it_did_not_print(tmp_path):
    cache = tmp_path / "stats-cache.json"
    models = {
        f"claude-model-{n}": {"inputTokens": 10 * n, "outputTokens": 5 * n, "costUSD": 0.0}
        for n in range(6)
    }
    receipt = {"version": 5, "dailyModelTokensVersion": 5, "modelUsage": models}
    store = {
        "schema": quotamirror.READINGS_SCHEMA,
        "caches": {
            quotamirror.path_identity(cache): {
                "readings": [
                    {"values": {}, "version_key": "5/5", "at": "2026-11-01T00:00:00Z"}
                ]
            }
        },
    }
    payload = quotamirror.revise_report(store, cache, receipt)
    assert len(payload["appeared"]) == 6
    text = quotamirror.render_revise(payload)
    assert "appeared in this reading only (6)" in text
    assert "+2 more" in text
