"""Day cross-check: `no_overlap` is three different situations, not one.

The verdict is legitimately `no_overlap` whenever no day was allowed to decide
it, but the reason was one sentence for all of them: "no day is both above the
token floor and present in both sources". On a corpus whose shared days all sit
under the floor that sentence is false (they ARE in both sources); on a
transcript scan that read no usage at all it invents a one-sided comparison
against nothing. Same family as the two overlap fixes before it: name the case.
"""

import quotamirror


def day(total_tokens, naive_tokens=None):
    naive = total_tokens if naive_tokens is None else naive_tokens
    return {
        "total_tokens": total_tokens,
        "naive_tokens": naive,
        "duplicate_tokens": naive - total_tokens,
        "records": 1,
    }


def scan(days):
    return {
        "days": days,
        "files": [],
        "records": 0,
        "messages": 0,
        "naive_tokens": 0,
        "tokens": {},
        "unattributed_tokens": 0,
    }


def test_shared_days_all_under_the_floor_are_named_below_the_floor():
    cache = {"2026-10-01": 999_999, "2026-10-02": 500_000}
    transcripts = scan(
        {
            "2026-10-01": day(1, naive_tokens=999_999),
            "2026-10-02": day(2, naive_tokens=500_000),
        }
    )

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["verdict"] == "no_overlap"  # the published decision is unchanged
    assert result["coverage"] == "all_below_floor"
    assert result["days_below_floor"] == 2
    assert "2 shared day(s) exist, but every one is under the 1,000,000-token floor" in result["reason"]
    # The old sentence claimed no day was present in both sources; two were.
    assert "present in both sources" not in result["reason"]


def test_a_shared_day_above_the_floor_keeps_the_coverage_at_shared_days():
    cache = {"2026-10-01": 999_999, "2026-10-09": 5_415_374_398}
    transcripts = scan(
        {
            "2026-10-01": day(1, naive_tokens=999_999),
            "2026-10-09": day(2_500_000_000, naive_tokens=5_415_374_398),
        }
    )

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["coverage"] == "shared_days"
    assert result["days_compared"] == 1
    assert result["days_below_floor"] == 1


def test_disjoint_windows_name_both_ranges_instead_of_claiming_no_overlap_of_days():
    cache = {"2026-05-03": 5_000_000}
    transcripts = scan({"2026-10-26": day(9_000_000)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["verdict"] == "no_overlap"
    assert result["coverage"] == "disjoint_windows"
    assert "the two windows do not intersect" in result["reason"]
    assert "cache table 2026-05-03..2026-05-03" in result["reason"]
    assert "transcript 2026-10-26..2026-10-26" in result["reason"]


def test_a_scan_that_read_no_usage_day_is_no_days_not_a_one_sided_count():
    cache = {"2026-05-03": 5_000_000}
    transcripts = scan({})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["coverage"] == "no_days"
    assert "transcript produced no day at all" in result["reason"]

    lines = []
    quotamirror._render_crosscheck(result, lines)
    text = "\n".join(lines)
    # Nothing was read on one side, so the section is not measured -- and the
    # one-sided count is not printed at all, because it would read as a comparison.
    assert "not measured -- transcript produced no day at all" in text
    assert "cache-only" not in text


def test_a_transcript_dir_that_read_no_file_is_not_reported_as_a_missing_flag(tmp_path):
    """The flag was passed and the directory was read: name the empty read, not the flag."""
    (tmp_path / "projects").mkdir()
    cache = {
        "modelUsage": {"m": {"inputTokens": 4}},
        "dailyModelTokens": [{"date": "2026-05-03", "tokensByModel": {"m": 5_000_000}}],
        "dailyActivity": [],
    }

    payload = quotamirror.compare_harnesses(
        tmp_path, cache, claude_transcripts=tmp_path / "projects"
    )
    crosscheck = payload["claude_day_crosscheck"]

    assert crosscheck["coverage"] == "no_days"
    assert "transcript produced no day at all" in crosscheck["reason"]
    assert "was not given" not in crosscheck["reason"]
    text = quotamirror.render_compare(payload)
    assert "not measured -- transcript produced no day at all" in text


def test_render_names_the_floor_when_every_shared_day_is_under_it():
    cache = {"2026-10-01": 999_999}
    transcripts = scan({"2026-10-01": day(1, naive_tokens=999_999)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)
    lines = []
    quotamirror._render_crosscheck(result, lines)
    text = "\n".join(lines)

    assert "verdict: no_overlap -- 1 shared day(s) exist, but every one is under" in text
    assert "no day is both above the token floor and present in both sources" not in text


def test_the_reason_does_not_fire_when_there_are_comparable_days():
    cache = {"2026-10-09": 5_415_374_398}
    transcripts = scan({"2026-10-09": day(2_500_000_000, naive_tokens=5_415_374_398)})

    result = quotamirror.crosscheck_days(cache, transcripts, min_tokens=1_000_000)

    assert result["coverage"] == "shared_days"
    assert "day is both above the token floor" not in (result["reason"] or "")
