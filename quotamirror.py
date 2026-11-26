"""quotamirror: a receipt for agent usage meters that cannot answer in dollars."""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from datetime import date, datetime, timedelta, timezone

TOKEN_BUCKETS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
)


def _tokens_for(usage):
    return sum(int(usage.get(b) or 0) for b in TOKEN_BUCKETS)


def summarize(cache, today=None):
    models = cache.get("modelUsage") or {}

    by_model = {}
    for name, usage in models.items():
        by_model[name] = {"tokens": _tokens_for(usage)}
    total = sum(m["tokens"] for m in by_model.values())
    for entry in by_model.values():
        entry["share"] = (entry["tokens"] / total) if total else 0.0

    priced = {}
    unpriced = []
    for name, usage in models.items():
        cost = float(usage.get("costUSD") or 0)
        if cost > 0:
            priced[name] = cost
        elif by_model[name]["tokens"] > 0:
            unpriced.append(name)

    if priced:
        dollars = {
            "status": "vendor_reported",
            "usd": round(sum(priced.values()), 6),
            "models_with_zero_unpriced": len(unpriced),
            "reason": (
                f"{len(unpriced)} of {len(by_model)} models carry costUSD = 0.00 "
                "while their token counts are non-zero; the sum covers only priced models"
            ),
        }
    else:
        models_seen = len(by_model)
        dollars = {
            "status": "unknown",
            "usd": None,
            "models_with_zero_unpriced": len(unpriced),
            "reason": (
                "this receipt carries no modelUsage entries, so no model carries a "
                "price and 0.00 is an absent meter, not a free one"
                if not models_seen
                else (
                    f"{len(unpriced)} of {models_seen} models record costUSD = 0.00 while "
                    f"{total:,} tokens were seen; 0.00 is the meter's placeholder, not a price"
                )
            ),
        }

    _, daily_per_day = _daily_totals(cache)
    day_series = [{"date": name, "tokens": daily_per_day[name]} for name in sorted(daily_per_day)]
    busiest_day = max(day_series, key=lambda d: d["tokens"]) if day_series else None

    # An empty `modelUsage` makes the lifetime total an empty sum, not a measurement.
    # Report the absence instead, and name the day view when that view does hold tokens.
    measured = bool(by_model)
    day_view_total = sum(daily_per_day.values())

    return {
        "tokens": {
            "total": total,
            "by_model": by_model,
            "measured": measured,
            "reason": None
            if measured
            else (
                "this receipt carries no modelUsage entries, so the file holds no "
                "lifetime total to report"
            ),
            "day_view_total": day_view_total,
            "day_view_days": len(daily_per_day),
        },
        "dollars": dollars,
        "models_total": len(by_model),
        "attribution": _attribution(cache, by_model),
        "coverage": _coverage(cache, daily_per_day),
        "days": day_series,
        "busiest_day": busiest_day,
        "as_of": cache.get("lastComputedDate"),
        "stale_days": _stale_days(cache.get("lastComputedDate"), today),
    }


def _stale_days(as_of, today):
    if not as_of or not today:
        return None
    return (date.fromisoformat(str(today)) - date.fromisoformat(str(as_of))).days


def _coverage(cache, daily_per_day):
    activity_days = sorted(
        row.get("date")
        for row in cache.get("dailyActivity") or []
        if row.get("date")
    )
    token_days = sorted(daily_per_day)
    return {
        "activity_days": len(activity_days),
        "token_days": len(token_days),
        "activity_without_tokens": sorted(set(activity_days) - set(token_days)),
        "tokens_without_activity": sorted(set(token_days) - set(activity_days)),
        "activity_first": activity_days[0] if activity_days else None,
        "activity_last": activity_days[-1] if activity_days else None,
        "tokens_first": token_days[0] if token_days else None,
        "tokens_last": token_days[-1] if token_days else None,
    }


def _daily_totals(cache):
    per_model = {}
    per_day = {}
    for row in cache.get("dailyModelTokens") or []:
        date = row.get("date")
        tokens_by_model = row.get("tokensByModel") or {}
        day_total = 0
        for model, tokens in tokens_by_model.items():
            value = int(tokens or 0)
            per_model[model] = per_model.get(model, 0) + value
            day_total += value
        if date:
            per_day[date] = per_day.get(date, 0) + day_total
    return per_model, per_day


CAPACITY_FIELDS = ("contextWindow", "maxOutputTokens")


def _any_tokens(models):
    return any(_tokens_for(entry) > 0 for entry in models.values() if isinstance(entry, dict))


def _declared_totals(cache):
    return {
        "totalMessages": cache.get("totalMessages"),
        "totalSessions": cache.get("totalSessions"),
    }


def _classify_series(name, total, declared):
    matches = [
        key
        for key, value in declared.items()
        if value is not None and total != 0 and int(value) == total
    ]
    if matches:
        return {
            "name": name,
            "sum": total,
            "verdict": "unit_inferred",
            "unit": "messages" if matches[0] == "totalMessages" else "sessions",
            "matched_total": matches[0],
        }
    return {
        "name": name,
        "sum": total,
        "verdict": "no_declared_total",
        "unit": None,
        "matched_total": None,
    }


def _series(cache):
    declared = _declared_totals(cache)
    columns = {}
    for row in cache.get("dailyActivity") or []:
        for key, value in row.items():
            if key == "date" or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                columns[key] = columns.get(key, 0) + int(value)
    series = [
        _classify_series(f"dailyActivity.{name}", total, declared)
        for name, total in columns.items()
    ]
    hours = cache.get("hourCounts") or {}
    if hours:
        series.append(
            _classify_series("hourCounts", sum(int(v or 0) for v in hours.values()), declared)
        )
    return series


PERCENT_WORDS = {
    "pct",
    "percent",
    "percentage",
    "utilization",
    "utilisation",
    "ratio",
    "share",
}

WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _words(name):
    words = []
    for part in re.split(r"[^A-Za-z0-9]+", str(name)):
        words.extend(match.group(0).lower() for match in WORD_RE.finditer(part))
    return words


def _is_percent_name(name):
    return any(word in PERCENT_WORDS for word in _words(name))


def _walk_numbers(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_numbers(value, name)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_numbers(value, prefix)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield prefix, node


def _percent_audit(cache):
    seen = {}
    for name, value in _walk_numbers(cache):
        leaf = name.rsplit(".", 1)[-1]
        if not _is_percent_name(leaf):
            continue
        seen[name] = max(seen.get(name, value), value)
    suspect = [
        {"field": name, "max": value, "verdict": "suspect_fraction"}
        for name, value in sorted(seen.items())
        if 0 < value <= 1
    ]
    return {"audited": len(seen), "suspect": suspect}


def audit_units(cache):
    models = cache.get("modelUsage") or {}
    zeros = []
    if _any_tokens(models):
        for field in CAPACITY_FIELDS:
            present = [
                int(entry.get(field) or 0)
                for entry in models.values()
                if isinstance(entry, dict) and field in entry
            ]
            if present and all(value == 0 for value in present):
                zeros.append(
                    {
                        "field": field,
                        "models": len(models),
                        "present": len(present),
                        "zeros": len(present),
                        "verdict": "all_zero",
                    }
                )
    audit = {
        "zeros": zeros,
        "series": _series(cache),
        "percents": _percent_audit(cache),
        "capacity": {
            "models": len([e for e in models.values() if isinstance(e, dict)]),
            "audited": bool(_any_tokens(models)),
        },
    }
    return audit


def _capacity_absence(payload):
    """The sentence for the case where no zero field was found.

    It must name the population it was taken over: 'nothing is wrong' over zero
    models is not an all-clear, it is a claim about a file nobody read.
    """
    capacity = payload.get("capacity") or {}
    models = capacity.get("models", 0)
    if models == 0:
        return (
            "0 modelUsage entries in this receipt - no capacity field can be audited, "
            "and none is claimed to be absent"
        )
    if not capacity.get("audited", True):
        return (
            f"{models} model(s) carry no tokens, so a zero capacity cannot be told apart "
            "from an unreported one - not audited"
        )
    return f"none of {models} model(s) has a field that is zero on every model"


def render_units(payload):
    lines = ["UNITS - magnitude audit of the meter's own fields", ""]
    lines.append("CAPACITY (a field that is present where zero is not a possible value)")
    zeros = payload.get("zeros") or []
    if not zeros:
        lines.append(f"  {_capacity_absence(payload)}")
    for row in zeros:
        lines.append(
            f"  {row['field']:<24} {row['zeros']} of {row['present']} present values are zero "
            f"across {row['models']} models"
        )
        lines.append(
            "    zero everywhere: any consumer that divides tokens by this field either "
            "crashes on divide-by-zero or reports 0% forever"
        )
    lines.append("")

    lines.append("SERIES (what each count is counting, inferred by exact arithmetic)")
    series = payload.get("series") or []
    if not series:
        lines.append("  no aggregate series in this file")
    for row in series:
        if row["verdict"] == "unit_inferred":
            lines.append(
                f"  {row['name']:<28} sums to {_fmt(row['sum'])} = {row['unit']} "
                f"(matches {row['matched_total']})"
            )
        else:
            lines.append(
                f"  {row['name']:<28} sums to {_fmt(row['sum'])} - no declared total matches, "
                "unit unknown"
            )
    lines.append("")

    lines.append("PERCENTS (a fraction printed as a percent is off by 100x)")
    percents = payload.get("percents") or {"audited": 0, "suspect": []}
    if not percents["suspect"]:
        lines.append(
            f"  no percent-named field holds a fraction in (0, 1] "
            f"({percents['audited']} audited)"
        )
    for row in percents["suspect"]:
        lines.append(
            f"  {row['field']:<24} max {row['max']} - suspect_fraction: "
            "if the reader multiplies by 100 this is a 100x overstatement"
        )
    return "\n".join(lines) + "\n"


CUMULATIVE_RATIO_FLOOR = 1.25
CUMULATIVE_GROWTH_FLOOR = 1.5
DATE_WORDS = ("date", "day")


def _date_series(cache):
    """Every date-keyed list in the receipt, flattened to {name: [values in date order]}.

    Numeric columns stay one series each; a column holding a map of numbers
    (a per-model breakdown of one day) collapses to a single series, because
    the question this audit asks is about the *day total*, not a model split.
    """
    series = {}
    for key, rows in cache.items():
        if not isinstance(rows, list) or not rows:
            continue
        if not all(isinstance(row, dict) for row in rows):
            continue
        columns = set()
        for row in rows:
            columns.update(row)
        date_key = next(
            (name for name in sorted(columns) if any(w in str(name).lower() for w in DATE_WORDS)),
            None,
        )
        if date_key is None:
            continue
        ordered = sorted(rows, key=lambda row: str(row.get(date_key)))
        for column in sorted(columns):
            if column == date_key:
                continue
            values = [row.get(column) for row in ordered]
            if all(isinstance(v, bool) for v in values) or all(v is None for v in values):
                continue
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                series[f"{key}.{column}"] = [int(v) for v in values]
            elif all(isinstance(v, dict) for v in values):
                series[f"{key}.{column}"] = [
                    sum(
                        int(x)
                        for x in v.values()
                        if isinstance(x, (int, float)) and not isinstance(x, bool)
                    )
                    for v in values
                ]
    return series


def _series_shape(name, values, declared):
    total = sum(values)
    last = values[-1] if values else 0
    row = {
        "name": name,
        "points": len(values),
        "sum": total,
        "first": values[0] if values else 0,
        "last": last,
        "max": max(values) if values else 0,
        "monotone": False,
        "last_matches": None,
        "sum_over_last": None,
        "verdict": "insufficient",
    }
    if len(values) < 3:
        return row
    row["monotone"] = all(b >= a for a, b in zip(values, values[1:]))
    if not row["monotone"]:
        row["verdict"] = "incremental"
        return row
    matched = next(
        (
            key
            for key, value in declared.items()
            if value is not None and last > 0 and int(value) == last and total != last
        ),
        None,
    )
    if matched:
        row["verdict"] = "cumulative_snapshot"
        row["last_matches"] = matched
        row["sum_over_last"] = total / last
        return row
    grew = last > 0 and (values[0] == 0 or last >= values[0] * CUMULATIVE_GROWTH_FLOOR)
    if grew and total >= last * CUMULATIVE_RATIO_FLOOR:
        row["verdict"] = "cumulative_suspect"
        row["sum_over_last"] = total / last
        return row
    row["verdict"] = "incremental"
    return row


def _cumulative_crosschecks(cache, totals):
    declared = _declared_totals(cache)
    coarse = {
        "dailyActivity.messageCount": ("totalMessages", declared.get("totalMessages")),
        "dailyActivity.sessionCount": ("totalSessions", declared.get("totalSessions")),
    }
    models = cache.get("modelUsage") or {}
    if models:
        coarse["dailyModelTokens.tokensByModel"] = (
            "modelUsage",
            sum(_tokens_for(entry) for entry in models.values() if isinstance(entry, dict)),
        )
    rows = []
    for name, total in totals.items():
        coarse_name, coarse_total = coarse.get(name, (None, None))
        row = {
            "fine": name,
            "fine_total": total,
            "coarse": coarse_name,
            "coarse_total": coarse_total,
            "delta": None,
            "factor": None,
            "verdict": "no_coarse_total",
        }
        if coarse_total is not None:
            row["delta"] = total - int(coarse_total)
            row["factor"] = (total / coarse_total) if coarse_total else None
            if total > coarse_total:
                row["verdict"] = "double_counted"
            elif total < coarse_total:
                row["verdict"] = "missing_granularity"
            else:
                row["verdict"] = "reconciled"
        rows.append(row)
    return rows


def audit_cumulative(cache):
    """Three exact tests for one arithmetic failure: a running total summed as a delta."""
    declared = _declared_totals(cache)
    series = _date_series(cache)
    shapes = [_series_shape(name, values, declared) for name, values in series.items()]
    totals = {name: sum(values) for name, values in series.items()}
    return {
        "series": shapes,
        "crosschecks": _cumulative_crosschecks(cache, totals),
    }


def _ratio(value):
    """A ratio near 1 needs four decimals: "1.00x" hides a 0.35% gap."""
    return f"{value:.4f}x" if 0.99 <= value <= 1.01 else f"{value:.2f}x"


def render_cumulative(payload):
    lines = ["CUMULATIVE (a running total summed as if it were a daily increment)", ""]
    lines.append(
        "  denominator: every ratio below compares a fine-grained sum with the same quantity"
    )
    lines.append(
        "  declared at a coarser level in this same file; no ratio is printed without the pair"
    )
    lines.append(
        "  it compares. Token quantities are counted input + output, where input side ="
    )
    lines.append("  inputTokens + cacheReadInputTokens + cacheCreationInputTokens.")
    lines.append("")

    shapes = payload.get("series") or []
    lines.append("SHAPE (is any date series a running total read as a daily increment?)")
    flagged = [row for row in shapes if row["verdict"].startswith("cumulative")]
    if not shapes:
        lines.append("  no date-keyed series in this receipt")
    elif not flagged:
        lines.append("  no series in this receipt has the cumulative shape")
    for row in flagged:
        lines.append(
            f"  {row['name']}  {row['points']} points, never decreases, "
            f"last {_fmt(row['last'])}, sum {_fmt(row['sum'])}"
        )
        if row["verdict"] == "cumulative_snapshot":
            lines.append(
                f"    cumulative_snapshot: the last value equals {row['last_matches']} while the "
                f"sum does not, so this is one counter read on every date and re-summed -"
            )
            lines.append(
                f"    summing it counts every earlier date again, {row['sum_over_last']:.2f}x the "
                "counter's own final value."
            )
        else:
            lines.append(
                "    cumulative_suspect: never decreasing and the sum is "
                f"{row['sum_over_last']:.2f}x the last value. If this is one running counter, the "
                "sum is an overstatement and the last value is the total;"
            )
            lines.append(
                "    nothing in this file declares the total, so the shape is all that is proven."
            )
    lines.append("")

    lines.append("CROSS-CHECK (fine-grained sum vs the same quantity declared once)")
    rows = payload.get("crosschecks") or []
    if not rows:
        lines.append("  nothing to cross-check")
    for row in rows:
        if row["verdict"] == "no_coarse_total":
            lines.append(
                f"  {row['fine']}  sums to {_fmt(row['fine_total'])} - no coarser declaration of "
                "the same quantity, so no verdict"
            )
            continue
        lines.append(
            f"  {row['fine']}  {_fmt(row['fine_total'])} of {row['coarse']} "
            f"{_fmt(int(row['coarse_total']))}  delta {row['delta']:+,}  {_ratio(row['factor'])}"
        )
        if row["verdict"] == "double_counted":
            lines.append(
                f"    double_counted: the fine-grained sum exceeds the coarse total by "
                f"{row['delta']:+,} ({_ratio(row['factor'])}). A cumulative counter summed over its "
                "own dates produces exactly this; the coarse total is the one to print."
            )
        elif row["verdict"] == "missing_granularity":
            lines.append(
                f"    short by {abs(row['delta']):,} ({_ratio(row['factor'])} of the coarse total). "
                "The fine-grained series is missing records; this is NOT double counting, and a "
                "reader who calls the gap inflation is wrong in the other direction."
            )
        else:
            lines.append("    reconciled: the fine-grained sum and the coarse total agree exactly.")
    return "\n".join(lines) + "\n"


def _attribution(cache, by_model):
    daily_per_model, _ = _daily_totals(cache)
    rows_naming = {}
    day_dates = []
    for row in cache.get("dailyModelTokens") or []:
        if not isinstance(row, dict):
            continue
        if _day_of(row.get("date")) is not None:
            day_dates.append(str(row.get("date"))[:10])
        for model, tokens in (row.get("tokensByModel") or {}).items():
            if not isinstance(model, str) or not model:
                continue
            try:
                int(tokens or 0)
            except (TypeError, ValueError):
                continue
            rows_naming[model] = rows_naming.get(model, 0) + 1
    activity_dates = sorted(day.isoformat() for day in _receipt_day_table(cache))
    day_dates.sort()
    day_table_first = day_dates[0] if day_dates else None
    day_table_last = day_dates[-1] if day_dates else None
    activity_first = activity_dates[0] if activity_dates else None
    activity_last = activity_dates[-1] if activity_dates else None
    # A day row for every day of use is what makes the per-day view a second
    # lifetime total. When the table starts after the receipt's first activity
    # row, the per-day figure is a floor over its own span, and the gap against
    # the per-model view is an artefact of that span - not evidence that the two
    # views disagree. The span is read from the receipt's own two tables, never
    # assumed.
    if day_table_first is None or activity_first is None:
        span_verdict = "not_comparable"
        days_before = 0
    else:
        days_before = sum(1 for day in activity_dates if day < day_table_first)
        span_verdict = "day_view_is_a_floor" if days_before else "same_span"

    model_total = sum(m["tokens"] for m in by_model.values())
    daily_total = sum(daily_per_model.values())

    gaps = []
    reconciled = 0
    for model in sorted(set(by_model) | set(daily_per_model)):
        seen_by_day = daily_per_model.get(model, 0)
        seen_by_model = by_model.get(model, {}).get("tokens", 0)
        if seen_by_day == seen_by_model:
            reconciled += 1
            continue
        presence = "day_rows" if rows_naming.get(model) else "no_day_row"
        gaps.append(
            {
                "model": model,
                "daily": seen_by_day,
                "model_total": seen_by_model,
                "gap": seen_by_model - seen_by_day,
                "day_rows": rows_naming.get(model, 0),
                # `no_day_row` is the absence of a row, which is not the same
                # claim as a row that measured zero: only the second one is a
                # measurement, and only one of them can be printed as a number.
                "presence": presence,
                "coverage": (
                    round(seen_by_day / seen_by_model, 4)
                    if seen_by_model and presence == "day_rows"
                    else None
                ),
            }
        )
    gaps.sort(key=lambda g: abs(g["gap"]), reverse=True)

    gap = model_total - daily_total
    return {
        "daily_total": daily_total,
        "model_total": model_total,
        "gap": gap,
        "gap_share": (abs(gap) / model_total) if model_total else 0.0,
        "reconciled_models": reconciled,
        "models_compared": len(set(by_model) | set(daily_per_model)),
        "gap_models": len(gaps),
        "gap_models_without_day_rows": sum(
            1 for g in gaps if g["presence"] == "no_day_row"
        ),
        "gaps": gaps,
        "day_table_first": day_table_first,
        "day_table_last": day_table_last,
        "activity_first": activity_first,
        "activity_last": activity_last,
        "days_before_day_table": days_before,
        "day_table_truncated": span_verdict == "day_view_is_a_floor",
        "span_verdict": span_verdict,
    }

def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


def redact_home(value: Any, home=None) -> Any:
    """Replace the current home prefix in report strings, including nested JSON."""
    home_text = str(Path.home() if home is None else home).rstrip(os.sep)
    if isinstance(value, str):
        if value == home_text:
            return "~"
        return value.replace(home_text + os.sep, "~" + os.sep)
    if isinstance(value, list):
        return [redact_home(item, home_text) for item in value]
    if isinstance(value, dict):
        return {
            redact_home(key, home_text) if isinstance(key, str) else key: redact_home(item, home_text)
            for key, item in value.items()
        }
    return value


def render_text(payload, source="(unknown)"):
    source = redact_home(source)
    lines = []
    lines.append("quotamirror scan - usage meter receipt")
    lines.append(f"source: {source}")

    as_of = payload.get("as_of")
    stale = payload.get("stale_days")
    if as_of and stale is not None:
        lines.append(
            f"as of: {as_of} ({stale} days before today) - totals are a snapshot, not live use"
        )
    elif as_of:
        lines.append(f"as of: {as_of} (staleness unknown: pass --today to measure)")
    else:
        lines.append("as of: unknown - the cache carries no lastComputedDate")
    lines.append("")

    tokens = payload["tokens"]
    lines.append("TOKENS (lifetime, all four buckets)")
    if tokens["measured"]:
        ranked = sorted(tokens["by_model"].items(), key=lambda kv: kv[1]["tokens"], reverse=True)
        for model, entry in ranked:
            lines.append(f"  {model:<26}{_fmt(entry['tokens']):>16}  {entry['share'] * 100:5.1f}%")
        lines.append(f"  {'TOTAL':<26}{_fmt(tokens['total']):>16}")
    else:
        lines.append(f"  not measured - {tokens['reason']}")
        if tokens["day_view_days"]:
            lines.append(
                f"  the day view in the same file sums to {_fmt(tokens['day_view_total'])} "
                f"over {tokens['day_view_days']} day(s) - that number is a lower bound "
                "on the days it covers, not the lifetime total"
            )
    busiest = payload.get("busiest_day")
    if busiest:
        lines.append(f"  busiest day {busiest['date']} with {_fmt(busiest['tokens'])} tokens")
    lines.append("")

    dollars = payload["dollars"]
    label = "unknown" if dollars["status"] == "unknown" else "vendor-reported"
    lines.append(f"DOLLARS  {label}")
    if dollars["status"] == "unknown":
        lines.append(f"  {dollars['reason']}")
    else:
        lines.append(f"  {dollars['usd']} as recorded by the meter; {dollars['reason']}")
    lines.append("")

    attribution = payload["attribution"]
    lines.append("ATTRIBUTION (the two lifetime views in the same file)")
    if attribution["models_compared"]:
        lines.append(
            f"  per-day view {_fmt(attribution['daily_total'])} - "
            f"per-model view {_fmt(attribution['model_total'])} - "
            f"gap {_fmt(attribution['gap'])} ({attribution['gap_share'] * 100:.2f}%)"
        )
        lines.append(
            f"  {attribution['reconciled_models']} of {attribution['models_compared']} "
            "models reconcile"
        )
        if attribution.get("span_verdict") == "day_view_is_a_floor":
            lines.append(
                f"  {attribution['days_before_day_table']} day(s) of activity sit before "
                f"the day table ({attribution['day_table_first']}); the per-day view is a "
                "floor over the days it covers, not a second lifetime total"
            )
        elif attribution.get("span_verdict") == "same_span":
            lines.append(
                f"  day table and activity rows both start {attribution['day_table_first']} "
                "- the two views cover the same span"
            )
        else:
            lines.append(
                "  span not comparable - the receipt holds no activity row, so there is "
                "no first day to compare the day table against"
            )
    else:
        lines.append(
            "  not comparable - the receipt carries no modelUsage entries, so there is "
            "only one view here; nothing can be said about a gap"
        )
    for gap in attribution["gaps"]:
        if gap.get("presence") == "no_day_row":
            lines.append(
                f"    {gap['model']:<26} no day row  "
                f"model {_fmt(gap['model_total']):>14}  gap {_fmt(gap['gap']):>14}"
            )
        else:
            coverage = gap.get("coverage")
            share = f" ({coverage * 100:.2f}%)" if coverage is not None else ""
            lines.append(
                f"    {gap['model']:<26} "
                f"{_fmt(gap['daily'])} of {_fmt(gap['model_total'])}{share}  "
                f"gap {_fmt(gap['gap']):>14}"
            )
    lines.append("")

    coverage = payload["coverage"]
    lines.append("COVERAGE")
    if coverage["activity_days"] or coverage["token_days"]:
        lines.append(
            f"  activity {coverage['activity_days']} days "
            f"({_range(coverage['activity_first'], coverage['activity_last'])}) - "
            f"token records {coverage['token_days']} days "
            f"({_range(coverage['tokens_first'], coverage['tokens_last'])})"
        )
        missing = coverage["activity_without_tokens"]
        if missing:
            lines.append(
                f"  {len(missing)} day(s) with messages but no token record: "
                f"{', '.join(missing)}"
            )
        elif coverage["activity_days"]:
            lines.append("  every activity day has a token record")
    else:
        lines.append("  none recorded in this receipt - there is no day to reconcile")
    return "\n".join(lines) + "\n"


def _sample(items, limit):
    """The first `limit` items, and how many were left out.

    Every list this tool prints has a count on the same line. A list that simply
    stops therefore reads as the whole list of a count it contradicts: the real
    corpus printed "38 day(s) ... here:" followed by 8 dates and nothing else.
    One sentence, so it cannot drift into three conventions (a bare "...", or no
    marker at all, both of which survived here until this helper existed).
    """
    items = list(items)
    shown = ", ".join(str(item) for item in items[:limit])
    rest = len(items) - limit
    return f"{shown}, +{rest} more" if rest > 0 else shown


def _range(first, last):
    """A date range, or 'none' - never the word None in a reader-facing line."""
    if first is None and last is None:
        return "none"
    return f"{first} .. {last}"


KNOWN_KEYS = ("dailyActivity", "dailyModelTokens", "modelUsage")


TRANSCRIPT_TYPES = ("assistant",)
COPY_TYPES = ("api-request-blob", "api-request", "api-request-shape")


def _canonical(value):
    """Order-independent rendering of a tool's arguments, for hashing only."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _digest(tool, value):
    payload = f"{tool}\x00{_canonical(value)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _transcript_files(root, max_files=None):
    root = Path(root)
    if root.is_file():
        return [root]
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, str(p)))
    if max_files is not None:
        files = files[-max_files:]
    return files


def _blocks(record):
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    return content if isinstance(content, list) else []


def loop_audit(root, max_files=None):
    """Count repeated tool calls per session. Arguments are hashed, never kept."""
    files = _transcript_files(root, max_files=max_files)
    streams = {}
    ordered = {}
    seen = set()
    calls = 0
    sidechain_calls = 0
    skipped_lines = 0
    skipped_copies = 0
    tool_of = {}

    for path in files:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            skipped_lines += 1
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped_lines += 1
                    continue
                kind = record.get("type")
                if kind in COPY_TYPES:
                    skipped_copies += sum(
                        1 for b in _blocks(record) if isinstance(b, dict) and b.get("type") == "tool_use"
                    )
                    continue
                if kind not in TRANSCRIPT_TYPES:
                    continue
                session = record.get("sessionId") or path.stem
                sidechain = bool(record.get("isSidechain"))
                stream_key = (session, sidechain)
                file_stream = (str(path), session, sidechain)
                for block in _blocks(record):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    block_id = block.get("id") or record.get("uuid") or f"{path.name}:{calls}"
                    dedupe_key = (stream_key, block_id)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    tool = block.get("name") or "(unnamed)"
                    digest = _digest(tool, block.get("input") or {})
                    tool_of[digest] = tool
                    streams.setdefault(stream_key, []).append(digest)
                    ordered.setdefault(file_stream, []).append(digest)
                    calls += 1
                    if sidechain:
                        sidechain_calls += 1

    repeat_calls = 0
    consecutive = 0
    longest_run = 0
    extra_by_digest = {}
    distinct_calls = 0
    for seq in streams.values():
        counts = {}
        for digest in seq:
            counts[digest] = counts.get(digest, 0) + 1
        distinct_calls += len(counts)
        for digest, n in counts.items():
            if n > 1:
                repeat_calls += n - 1
                extra_by_digest[digest] = extra_by_digest.get(digest, 0) + (n - 1)

    # Consecutive means adjacent in one continuous transcript. A resumed session
    # spans files, and a gap of hours is not a back-to-back retry.
    for seq in ordered.values():
        run = 1
        for i in range(1, len(seq)):
            if seq[i] == seq[i - 1]:
                consecutive += 1
                run += 1
            else:
                run = 1
            longest_run = max(longest_run, run)
        longest_run = max(longest_run, min(1, len(seq)))

    repeated = [
        {
            "tool": tool_of.get(digest, "(unnamed)"),
            "hash": digest,
            "calls": extra + 1,
            "extra_calls": extra,
        }
        for digest, extra in sorted(extra_by_digest.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    by_tool = {}
    for row in repeated:
        by_tool[row["tool"]] = by_tool.get(row["tool"], 0) + row["extra_calls"]
    by_tool_rows = [
        {"tool": tool, "extra_calls": n}
        for tool, n in sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    if calls == 0:
        verdict = "no_data"
    elif repeat_calls == 0:
        verdict = "no_repeat_evidence"
    else:
        verdict = "repeat_observed"

    return {
        "root": str(root),
        "files": len(files),
        "sessions": len({stream[0] for stream in streams}),
        "calls": calls,
        "sidechain_calls": sidechain_calls,
        "distinct_calls": distinct_calls,
        "repeat_calls": repeat_calls,
        "repeat_rate": (repeat_calls / calls) if calls else None,
        "longest_consecutive_run": longest_run,
        "consecutive_repeat_calls": consecutive,
        "repeated": repeated[:5],
        "by_tool": by_tool_rows[:10],
        "double_count": {
            "skipped_tool_use": skipped_copies,
            "reason": "same call recorded inside request-body copies; counting every record overstates calls",
        },
        "skipped_lines": skipped_lines,
        "verdict": verdict,
        "redaction": "arguments are hashed (sha256, 12 hex) and never stored or printed",
    }


def render_loop(payload):
    payload = redact_home(payload)
    lines = [
        "LOOP - repeated tool calls in session transcripts",
        "  arguments are hashed, never printed: this report cannot leak prompt text",
        "",
    ]
    if payload["calls"] == 0:
        lines.append(f"  no transcript tool calls found under {payload['root']}")
        lines.append("  nothing measured: an absent rate is not a zero rate")
        return "\n".join(lines) + "\n"

    lines.append(
        f"  {_fmt(payload['calls'])} tool calls over {payload['sessions']} sessions "
        f"({payload['files']} files), {_fmt(payload['distinct_calls'])} distinct"
    )
    rate = payload["repeat_rate"] * 100
    lines.append(f"  repeats: {_fmt(payload['repeat_calls'])} ({rate:.1f}% of calls)")
    if payload["verdict"] == "no_repeat_evidence":
        lines.append("  verdict: no repeated tool call in this sample")
    else:
        lines.append(
            f"  longest identical run: {payload['longest_consecutive_run']} "
            f"({_fmt(payload['consecutive_repeat_calls'])} back-to-back repeats)"
        )
        for row in payload["repeated"]:
            lines.append(
                f"    {row['tool']:<28} {row['calls']}x same arguments  hash {row['hash']}"
            )
    lines.append("")
    lines.append("DOUBLE COUNT")
    skipped = payload["double_count"]["skipped_tool_use"]
    if skipped:
        lines.append(
            f"  {_fmt(skipped)} tool_use blocks skipped: they are copies inside request bodies. "
            "Summing every record would overstate calls by that much."
        )
    else:
        lines.append("  no request-body copies in this sample")
    lines.append("")
    lines.append(
        "LIMIT: only identical arguments count. A retry that adds one flag is a different "
        "call here, so this is a floor, not a loop detector."
    )
    lines.append("")
    lines.append(f"  {_fmt(payload['sidechain_calls'])} of those calls are subagent (sidechain) calls")
    return "\n".join(lines) + "\n"


_DATE_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MODEL_KEYED_FIELDS = {"modelUsage", "tokensByModel"}


def _type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        # a price that is 0 in one build and 0.0 in the next is not a schema change
        return "number"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _shape_walk(node, prefix=""):
    """Walk a receipt into (shape, presence).

    `shape` maps every path to a type name. `presence` maps a path whose key was
    collapsed into `<model>` or `<date>` to [carried, family_size]: how many
    members of that family carry it. A priced field that one of nine models kept
    is [1, 9], and that is precisely the fact a bare "present" was hiding.
    """
    shape, presence = {}, {}
    if isinstance(node, dict):
        parent = prefix.rstrip(".").rsplit(".", 1)[-1].rstrip("[]")
        groups = {}
        for key, value in node.items():
            if parent in _MODEL_KEYED_FIELDS:
                segment = "<model>"
            else:
                segment = "<date>" if _DATE_KEY.match(str(key)) else str(key)
            groups.setdefault(segment, []).append(value)

        for segment, members in groups.items():
            child = f"{prefix}{segment}"
            member_shapes = []
            owned_deeper = set()
            for value in members:
                member_shape, member_presence = _shape_walk(value, f"{child}.")
                member_shapes.append(member_shape)
                owned_deeper.update(member_presence)
                shape.update(member_shape)
                for path, counts in member_presence.items():
                    total = presence.setdefault(path, [0, 0])
                    total[0] += counts[0]
                    total[1] += counts[1]
            shape[child] = _type_name(members[-1])
            if segment in ("<model>", "<date>") and len(members) > 1:
                every_path = {path for member in member_shapes for path in member}
                for path in every_path - owned_deeper:
                    carried = sum(1 for member in member_shapes if path in member)
                    presence[path] = [carried, len(members)]
    elif isinstance(node, list):
        member_shapes = []
        owned_deeper = set()
        for item in node:
            member_shape, member_presence = _shape_walk(item, prefix=f"{prefix[:-1]}[].")
            member_shapes.append(member_shape)
            owned_deeper.update(member_presence)
            shape.update(member_shape)
            for path, counts in member_presence.items():
                total = presence.setdefault(path, [0, 0])
                total[0] += counts[0]
                total[1] += counts[1]
        if len(node) > 1:
            # A field that one day of a 38-day list carries and 37 do not is the
            # same blind spot the model family had: present, but on 1 of 38.
            every_path = {path for member in member_shapes for path in member}
            for path in every_path - owned_deeper:
                carried = sum(1 for member in member_shapes if path in member)
                presence[path] = [carried, len(node)]
    return shape, presence


def shape_of(receipt, prefix=""):
    """Map every path in a receipt to its type name. Values are never kept."""
    return _shape_walk(receipt, prefix)[0]


def shape_presence(receipt):
    """path -> [carried, family_size] inside the collapsed model/date families."""
    return _shape_walk(receipt, "")[1]


def shape_digest(shape):
    """A short identity for a field set, so a suspicion can be re-recognised.

    Not reversible and not a shape: a pending read must not add a second copy of
    the receipt's field names to the store just to be remembered.
    """
    payload = json.dumps(sorted(shape.items()), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _shape_is_strict_subset(base, current):
    """True when `current` holds fewer fields and none of the shared ones changed.

    That is what a read of a half-written receipt looks like. It is also what a
    vendor deleting a subtree looks like - one read cannot tell them apart, which
    is why this only raises a suspicion and never a verdict.
    """
    if not base or not current:
        return False
    if not set(current) < set(base):
        return False
    return all(base[path] == current[path] for path in current)


STORE_SCHEMA = 6



def path_identity(path):
    """Return a stable, non-reversible identity without storing the local path."""
    canonical = str(Path(path).expanduser().resolve(strict=False))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _version_of(receipt):
    version = receipt.get("version")
    daily = receipt.get("dailyModelTokensVersion")
    key = f"{version if version is not None else '?'}/{daily if daily is not None else '?'}"
    return key, (version is not None and daily is not None and version != daily)


def load_store(store_path):
    try:
        with open(store_path, "r", encoding="utf-8") as handle:
            store = json.load(handle)
    except FileNotFoundError:
        return {"schema": STORE_SCHEMA, "caches": {}}
    if not isinstance(store, dict) or not isinstance(store.get("caches"), dict):
        raise ValueError(f"{store_path} is not a quotamirror shape store")
    return store


def _utc_stamp():
    """One timestamp format for everything this tool writes to disk."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate_store(store):
    """Upgrade a shape store in place. Entries that predate presence are kept.

    Wiping a baseline to gain a field would delete the only copy of the shape a
    future version has to be compared against; a baseline without presence is
    still a valid shape baseline, it just cannot answer the newer question.
    """
    if store.get("schema") not in (2, 3, 4, 5, STORE_SCHEMA):
        return {"schema": STORE_SCHEMA, "caches": {}}
    store["schema"] = STORE_SCHEMA
    return store


def record_shape(store_path, cache_path, receipt, captured_at=None):
    """Persist a receipt's shape and per-family presence under its declared version.

    Values never reach disk - only paths, type names, and how many models (or
    days) carried each path, which is a count, not a value.
    """
    if store_path.is_symlink():
        raise ValueError(f"refusing symlink shape store: {store_path}")
    store = _migrate_store(load_store(store_path))
    key, _ = _version_of(receipt)
    versions = store["caches"].setdefault(path_identity(cache_path), {}).setdefault("versions", {})
    shape = shape_of(receipt)
    presence = shape_presence(receipt)
    stamp = captured_at or _utc_stamp()
    previous = versions.get(key) or {}
    digest = shape_digest(shape)
    previous_digest = shape_digest(previous["shape"]) if previous.get("shape") else None
    previous_reads = previous.get("reads")
    challenged = previous.get("pending_partial") or {}
    confirmed = challenged.get("shape_digest") == digest
    previous_shape = previous.get("shape") or {}
    # A read that keeps every path and type but carries fewer members of a family.
    # Nothing on the shape axis can see it - which is why it needs its own challenge.
    narrowed_now = _narrowed_rows(
        previous_shape, shape, previous.get("presence"), presence
    )
    narrowing_only = (
        bool(narrowed_now)
        and set(previous_shape) == set(shape)
        and all(previous_shape[p] == shape[p] for p in shape)
    )

    if not confirmed and _shape_is_strict_subset(previous.get("shape") or {}, shape):
        # Shorter than the record at this same version. A vendor that dropped a
        # field would have changed the version it declares; a read taken while the
        # file was being written looks exactly like this. So the baseline stays and
        # only the challenge is written - a second identical read is what turns it
        # into a removal somebody can check.
        previous["pending_partial"] = {
            "shape_digest": digest,
            "fields": len(shape),
            "at": stamp,
        }
        versions.pop(key, None)
        versions[key] = previous
    elif narrowing_only:
        # Every path and type survived; only the number of family members carrying
        # them fell. A receipt being written incrementally looks exactly like this
        # (two of three model objects emitted), so the older presence stays the
        # baseline and only the challenge is written. The same narrower counts read
        # twice are what become the record.
        pending_narrowing = previous.get("pending_narrowed") or {}
        narrowing_digest = _narrowing_digest(narrowed_now)
        if pending_narrowing.get("presence_digest") == narrowing_digest:
            versions.pop(key, None)
            versions[key] = {
                "shape": shape,
                "presence": presence,
                "presence_lists": True,
                "reads": 2,
                "at": stamp,
                "narrowing_confirmed_at": stamp,
            }
        else:
            previous["pending_narrowed"] = {
                "presence_digest": narrowing_digest,
                "rows": len(narrowed_now),
                "at": stamp,
            }
            versions.pop(key, None)
            versions[key] = previous
    else:
        # How many reads have produced this exact shape at this version. One read is
        # a record nobody has agreed with yet, which is why a later fuller read has
        # to be reported as an ambiguity rather than as a vendor addition.
        if confirmed:
            reads = 2  # the challenge read and this one
        elif previous_digest == digest:
            reads = (previous_reads if isinstance(previous_reads, int) else 1) + 1
        else:
            reads = 1
        versions.pop(key, None)
        versions[key] = {
            "shape": shape,
            "presence": presence,
            # schema 4: presence also counts list members. A schema-3 baseline has
            # counts for keyed families only, and has to say so rather than imply
            # the list axis was measured.
            "presence_lists": True,
            "reads": reads,
            "at": stamp,
        }

    store_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{store_path.name}.", dir=store_path.parent)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(store, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        if store_path.is_symlink():
            raise ValueError(f"refusing symlink shape store: {store_path}")
        os.replace(temporary, store_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return store, key


METER_WORDS = (
    "cost",
    "usd",
    "price",
    "dollar",
    "spend",
    "allowance",
    "credit",
    "quota",
    "bill",
    "charge",
)


def _is_meter_path(path):
    leaf = path.rsplit(".", 1)[-1].rstrip("[]").lower()
    return any(word in leaf for word in METER_WORDS)


def _list_name(path):
    """The list a collapsed path lives in, or None when it holds no list member."""
    if "[]" not in path:
        return None
    head = path.rsplit("[]", 1)[0]
    return head.split(".")[-1] or None


def _family_of(path):
    """Which family a narrowed count is about - the innermost one in the path."""
    marks = []
    if "<model>" in path:
        marks.append((path.rfind("<model>"), "<model>"))
    if "<date>" in path:
        marks.append((path.rfind("<date>"), "<date>"))
    if "[]" in path:
        marks.append((path.rfind("[]"), f"list:{_list_name(path)}"))
    if not marks:
        return "<date>"
    return max(marks)[1]


def _family_noun(segment):
    if segment == "<model>":
        return "model(s)"
    if segment.startswith("list:"):
        return f"{segment[len('list:'):]} member(s)"
    return "day(s)"


def _narrowed_rows(base, current, base_presence, current_presence):
    """Fields still present, but on fewer family members than the baseline had.

    A family that shrank on its own - a model left the receipt entirely - is not
    a narrowing: every field loses exactly one carrier at once, and calling that
    "9 fields narrowed" would drown the one case this is for. So a row only
    counts when the family is still at least as large as the old carrier count.
    """
    if not base_presence or not current_presence:
        return []
    rows = []
    for path in sorted(set(base) & set(current)):
        was = base_presence.get(path)
        now = current_presence.get(path)
        if not was or not now:
            continue
        if now[0] < was[0] and now[1] >= was[0]:
            rows.append(
                {
                    "path": path,
                    "family": _family_of(path),
                    "was": list(was),
                    "now": list(now),
                }
            )
    return rows


def _regained_rows(base, current, base_presence, current_presence):
    """Fields on MORE family members than the record had.

    The mirror of a narrowing, and the only way a narrowing is ever undone: the
    record said a field had lost a carrier, and this read has it back. A family
    that grew on its own is not this - a new model arriving with the field raises
    both counts at once, and calling that "one carrier returned" would invent a
    reversal. So a row only counts while the family size is the same on both
    sides; a changed size is reported by the shape and by nothing here.
    """
    if not base_presence or not current_presence:
        return []
    rows = []
    for path in sorted(set(base) & set(current)):
        was = base_presence.get(path)
        now = current_presence.get(path)
        if not was or not now:
            continue
        if now[0] > was[0] and now[1] == was[1]:
            rows.append(
                {
                    "path": path,
                    "family": _family_of(path),
                    "was": list(was),
                    "now": list(now),
                }
            )
    return rows


def _narrowing_digest(rows):
    """Identity of one narrowing reading: which paths, which two counts.

    A narrowing leaves the path set - and so the shape digest - intact, so the
    shape axis cannot tell one narrowing reading from another. The counts are the
    only thing that changed, which is exactly what has to be recognised twice.
    """
    joined = "|".join(
        f"{row['path']}:{row['was'][0]}/{row['was'][1]}->{row['now'][0]}/{row['now'][1]}"
        for row in sorted(rows, key=lambda row: row["path"])
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _shape_diff(
    base,
    current,
    base_presence=None,
    current_presence=None,
    presence_compared=False,
    list_presence_compared=None,
    detect_regain=False,
    regain_basis="same_version",
):
    """What changed between two shapes. Used by both drift axes."""
    if list_presence_compared is None:
        list_presence_compared = presence_compared
    removed = sorted(set(base) - set(current))
    added = sorted(set(current) - set(base))
    type_changed = [
        {"path": path, "was": base[path], "now": current[path]}
        for path in sorted(set(base) & set(current))
        if base[path] != current[path]
    ]
    meter_removed = [path for path in removed if _is_meter_path(path)]
    meter_altered = [row["path"] for row in type_changed if _is_meter_path(row["path"])]
    narrowed = _narrowed_rows(base, current, base_presence, current_presence)
    meter_narrowed = [row["path"] for row in narrowed if _is_meter_path(row["path"])]
    # Only the same-version axis asks for this: a regain is a statement about the
    # record this run has at THIS version, and the cross-version axis has no
    # policy for it yet (README, LIMIT).
    regained = (
        _regained_rows(base, current, base_presence, current_presence)
        if detect_regain
        else []
    )
    meter_regained = [row["path"] for row in regained if _is_meter_path(row["path"])]
    if meter_removed:
        verdict = "meter_field_removed"
    elif meter_altered:
        verdict = "meter_field_altered"
    elif meter_narrowed:
        verdict = "meter_field_narrowed"
    elif narrowed:
        verdict = "field_narrowed"
    elif meter_regained:
        verdict = (
            "meter_field_regained_across_versions"
            if regain_basis == "cross_version"
            else "meter_field_regained"
        )
    elif regained:
        verdict = (
            "field_regained_across_versions"
            if regain_basis == "cross_version"
            else "field_regained"
        )
    elif removed or added or type_changed:
        verdict = "schema_changed"
    elif not (presence_compared and list_presence_compared):
        # the shape axis is clean, but at least one presence axis was never
        # recorded for this baseline - an all-clear here would be a claim about
        # a measurement nobody made
        verdict = "unchanged_shape_only"
    else:
        verdict = "unchanged"
    unmeasured = [
        name
        for name, compared in (("map", presence_compared), ("list", list_presence_compared))
        if not compared
    ]
    return {
        "verdict": verdict,
        "removed": removed,
        "added": added,
        "type_changed": type_changed,
        "meter_fields_removed": meter_removed,
        "meter_fields_altered": meter_altered,
        "narrowed": narrowed,
        "meter_fields_narrowed": meter_narrowed,
        "regained": regained,
        "meter_fields_regained": meter_regained,
        "regain_basis": regain_basis,
        "presence_compared": presence_compared,
        "list_presence_compared": list_presence_compared,
        "presence_axes_unmeasured": unmeasured,
    }


WITHIN_REASONS = {
    "no_shape_recorded_at_this_version": (
        "no shape is recorded at this version - either nothing has been recorded for"
        " this file, or --no-record left the store untouched"
    ),
}


def within_version_report(versions, current, current_key, current_presence=None):
    """This version against the last shape recorded at the same version.

    Fed the store as it is *before* this run records, so `entry["shape"]` is the
    previous shape at this version rather than the one just written.
    """
    entry = versions.get(current_key) or {}
    payload = {
        "measurable": False,
        "reason": "no_shape_recorded_at_this_version",
        "reason_text": WITHIN_REASONS["no_shape_recorded_at_this_version"],
        "baseline_at": None,
        "fields_baseline": 0,
        "verdict": "not_measured",
        "removed": [],
        "added": [],
        "type_changed": [],
        "meter_fields_removed": [],
        "meter_fields_altered": [],
        "narrowed": [],
        "meter_fields_narrowed": [],
        "regained": [],
        "meter_fields_regained": [],
        "presence_compared": False,
        "list_presence_compared": False,
        "presence_axes_unmeasured": [],
        "same_as_recorded_at": None,
        "partial_read": None,
        "short_baseline": None,
        "baseline_reads": None,
        "confirmed_removal": False,
        "possible_narrowing": None,
        "confirmed_narrowing": False,
        "reversed_narrowing_at": None,
    }
    base = entry.get("shape")
    if not base:
        return payload
    base_presence = entry.get("presence")
    diff = _shape_diff(
        base,
        current,
        base_presence,
        current_presence,
        presence_compared=base_presence is not None and current_presence is not None,
        list_presence_compared=bool(entry.get("presence_lists"))
        and current_presence is not None,
        detect_regain=True,
    )
    digest = shape_digest(current)
    challenged = entry.get("pending_partial") or {}
    confirmed = challenged.get("shape_digest") == digest
    suspected = not confirmed and _shape_is_strict_subset(base, current)
    reads = entry.get("reads")
    # Grew past the record, changed nothing the record already had, and the record
    # came from a single read. A vendor addition and a short first read are the same
    # picture here, so neither name is allowed.
    short_baseline = bool(
        reads == 1 and diff["added"] and not diff["removed"] and not diff["type_changed"]
    )
    payload.update(diff)
    payload.update(
        {
            "measurable": True,
            "reason": None,
            "reason_text": None,
            "baseline_at": entry.get("at"),
            "fields_baseline": len(base),
            "baseline_reads": reads,
            "same_as_recorded_at": entry.get("at")
            if diff["verdict"] == "unchanged"
            else None,
        }
    )
    if diff["regained"]:
        # A record can be narrowed without having been confirmed twice (an older
        # baseline from before r100), so this stamp is optional rather than
        # required; when it exists it names the sentence this read contradicts.
        payload["reversed_narrowing_at"] = entry.get("narrowing_confirmed_at")
    if suspected or confirmed:
        payload["partial_read"] = {
            "confirmed": confirmed,
            "shape_digest": digest,
            "fields_baseline": len(base),
            "fields_now": len(current),
            "fields_unconfirmed": diff["removed"],
            "priced_unconfirmed": diff["meter_fields_removed"]
            + [
                row["path"]
                for row in diff["narrowed"]
                if _is_meter_path(row["path"])
                and row["path"] not in diff["meter_fields_removed"]
            ],
            "recorded_at": entry.get("at"),
        }
    if suspected:
        # The shape axis saw fields disappear, but this read is shorter than the
        # record at the same version, which is what a half-written file looks like.
        # Refusing to name a verdict is the only honest report: the removal is not
        # checkable until the same smaller shape is seen again.
        payload["verdict"] = "possible_partial_read"
    if confirmed:
        payload["confirmed_removal"] = True
    if short_baseline:
        # The mirror case: one unconfirmed record, then a read that only has more.
        # Recording the fuller shape is right - a truncation cannot grow past the
        # record it truncated. Naming the difference a vendor addition is not.
        payload["verdict"] = "possible_short_baseline"
        payload["short_baseline"] = {
            "shape_digest": digest,
            "fields_baseline": len(base),
            "fields_now": len(current),
            "fields_unconfirmed": diff["added"],
            "recorded_at": entry.get("at"),
            "reads": reads,
        }
    narrowed_rows = diff["narrowed"]
    narrowing_only = (
        bool(narrowed_rows)
        and not diff["added"]
        and not diff["removed"]
        and not diff["type_changed"]
    )
    challenged_narrowing = entry.get("pending_narrowed") or {}
    confirmed_narrowing = (
        narrowing_only
        and challenged_narrowing.get("presence_digest")
        == _narrowing_digest(narrowed_rows)
    )
    if narrowing_only and not confirmed_narrowing:
        # Every path and type is still there; only the family counts fell. That is
        # what a receipt written incrementally looks like as well as what a vendor
        # narrowing a field looks like, and one read cannot separate them. The
        # baseline keeps the older counts; the same narrower counts read again are
        # what make it a narrowing.
        payload["verdict"] = "possible_narrowing"
        priced = list(diff["meter_fields_removed"])
        for row in narrowed_rows:
            if _is_meter_path(row["path"]) and row["path"] not in priced:
                priced.append(row["path"])
        payload["possible_narrowing"] = {
            "rows": narrowed_rows,
            "priced_unconfirmed": priced,
            "fields_baseline": len(base),
            "fields_now": len(current),
            "recorded_at": entry.get("at"),
            "reads": reads,
        }
    elif confirmed_narrowing:
        # These exact counts were already read once at this version and the read
        # was recorded as a suspicion. A second identical reading is what an
        # incrementally written file cannot produce.
        payload["confirmed_narrowing"] = True
    return payload


def drift_report(store, cache_path, receipt):
    """Compare a receipt's current shape with the last shape recorded at another version."""
    caches = store.get("caches", {})
    versions = caches.get(path_identity(cache_path), {}).get("versions", {})
    if not versions:
        versions = caches.get(str(cache_path), {}).get("versions", {})
    current_key, disagree = _version_of(receipt)
    current = shape_of(receipt)
    current_presence = shape_presence(receipt)
    others = {k: v for k, v in versions.items() if k != current_key}
    payload = {
        "source": str(cache_path),
        "version_key": current_key,
        "version_fields_disagree": disagree,
        "versions_recorded": sorted(versions),
        "fields_now": len(current),
        "baseline_version_key": None,

        "measurable": False,
        "verdict": "no_baseline",
        "removed": [],
        "added": [],
        "type_changed": [],
        "meter_fields_removed": [],
        "meter_fields_altered": [],
        "narrowed": [],
        "meter_fields_narrowed": [],
        "regained": [],
        "meter_fields_regained": [],
        "regain_basis": None,
        "baseline_reads": None,
        "presence_compared": False,
        "list_presence_compared": False,
        "presence_axes_unmeasured": [],
        "cross_version_unconfirmed": None,
    }
    payload["within_version"] = within_version_report(
        versions, current, current_key, current_presence
    )
    payload["partial_read"] = payload["within_version"]["partial_read"]
    payload["short_baseline"] = payload["within_version"]["short_baseline"]
    if not others:
        return payload

    baseline_key = next(reversed(others))
    baseline = others[baseline_key]
    base = baseline.get("shape") or {}
    base_presence = baseline.get("presence")
    diff = _shape_diff(
        base,
        current,
        base_presence,
        current_presence,
        presence_compared=base_presence is not None and current_presence is not None,
        list_presence_compared=bool(baseline.get("presence_lists"))
        and current_presence is not None,
        detect_regain=True,
        regain_basis="cross_version",
    )
    removed = diff["removed"]
    added = diff["added"]
    type_changed = diff["type_changed"]
    meter_removed = diff["meter_fields_removed"]
    meter_altered = diff["meter_fields_altered"]
    verdict = diff["verdict"]
    payload.update(
        {
            "baseline_version_key": baseline_key,

            "measurable": True,
            "verdict": verdict,
            "removed": removed,
            "added": added,
            "type_changed": type_changed,
            "meter_fields_removed": meter_removed,
            "meter_fields_altered": meter_altered,
            "narrowed": diff["narrowed"],
            "meter_fields_narrowed": diff["meter_fields_narrowed"],
            "regained": diff["regained"],
            "meter_fields_regained": diff["meter_fields_regained"],
            "regain_basis": diff["regain_basis"],
            "baseline_reads": baseline.get("reads"),
            "presence_compared": diff["presence_compared"],
            "list_presence_compared": diff["list_presence_compared"],
            "presence_axes_unmeasured": diff["presence_axes_unmeasured"],
            "fields_baseline": len(base),
        }
    )
    within_verdict = payload["within_version"]["verdict"]
    if within_verdict in (
        "possible_partial_read",
        "possible_short_baseline",
        "possible_narrowing",
    ) and (added or removed or type_changed or diff["narrowed"]):
        # The same read fed this axis, and this axis has a difference of its own that
        # the suspect record produced. Naming a vendor change from it would be the
        # r89 defect stated across versions. A clean cross-version comparison is left
        # alone: it rests on the other version's record, not on this one.
        payload["verdict"] = within_verdict
    # The upgrade case: version A has a record, version B was just published, and
    # this is the first read at B. A difference that only subtracts - whole paths
    # gone, or a field gone from part of a family - cannot be told from a read
    # taken while the file was being rewritten, and the within-version axis has
    # nothing at B to check it against. Naming a vendor change from that one read
    # would be the r89 defect stated across versions. Both lists are reported
    # unconfirmed, and the read is still recorded: the same shape read twice at B
    # is what makes it a removal or a narrowing (tests/test_cross_version_*).
    narrowing = diff["narrowed"]
    if (
        within_verdict not in ("possible_partial_read", "possible_short_baseline")
        and not added
        and not type_changed
        and (removed or narrowing)
    ):
        entry_here = versions.get(current_key) or {}
        seen_here = bool(entry_here.get("shape")) and shape_digest(
            entry_here["shape"]
        ) == shape_digest(current)
        if not seen_here:
            priced = list(meter_removed)
            for row in narrowing:
                if _is_meter_path(row["path"]) and row["path"] not in priced:
                    priced.append(row["path"])
            payload["verdict"] = (
                "removal_unconfirmed" if removed else "narrowing_unconfirmed"
            )
            payload["cross_version_unconfirmed"] = {
                "version_key": current_key,
                "baseline_version_key": baseline_key,
                "baseline_at": baseline.get("at"),
                "fields_baseline": len(base),
                "fields_now": len(current),
                "fields_unconfirmed": removed,
                "priced_unconfirmed": priced,
                "narrowed_unconfirmed": narrowing,
                "reads_at_current": entry_here.get("reads"),
            }
    return payload


def _regain_reads_line(reads):
    """How many times the other version's record was read, in words."""
    if reads is None:
        return "  the other version's record carries no read count, so its carriers were never checked twice."
    if reads <= 1:
        return "  that record was read once; one read cannot tell a short record from a narrowed one."
    return f"  that record was read {reads} times."


def _regain_block(rows, priced, basis="same_version", reads=None):
    """Header plus one line per regained field, shared by both render paths.

    The header has to carry which axis found this. Inside one version a return is
    a reversal of a narrowing the record itself reported; across versions the older
    record only had fewer carriers, and a short record looks exactly the same. Two
    different claims, two different sentences.
    """
    if basis == "cross_version":
        lines = [
            f"REGAINED ACROSS VERSIONS ({len(rows)}) - the other version's record had"
            " fewer carriers than this read does"
        ]
    else:
        lines = [
            f"REGAINED ({len(rows)}) - the record had fewer carriers than this read does"
        ]
    for row in rows:
        marker = "!" if row["path"] in priced else " "
        lines.append(
            f"  {marker} {row['path']}: {row['now'][0]} of {row['now'][1]} "
            f"{_family_noun(row['family'])} (was {row['was'][0]} of {row['was'][1]})"
        )
    if basis == "cross_version":
        lines.append(_regain_reads_line(reads))
        lines.append(
            "  A vendor putting a carrier back and a record written while the file was"
        )
        lines.append(
            "  still being written produce the same two counts, so nothing here says the"
        )
        lines.append("  vendor restored it.")
    return lines


def _presence_gap_lines(unmeasured):
    """Why a clean shape is not a clean receipt when a presence axis is missing.

    Two axes are now counted - keyed families (model / day) and list members -
    and a baseline can have one without the other. Naming which one is missing
    is the difference between 'unmeasured' and a clean bill.
    """
    head = "  the shape is identical, but presence was not recorded for the baseline"
    if len(unmeasured) > 1 or not unmeasured:
        return [
            head + " at all, so a field dropped",
            "  from one model or one list member would look exactly like this. That axis is",
            "  unmeasured, not clean.",
        ]
    if unmeasured == ["list"]:
        return [
            head + "'s",
            "  list members, so a field dropped from one day of a list would look exactly",
            "  like this. That axis is unmeasured, not clean.",
        ]
    return [
        head + "'s",
        "  model / day counts, so a field dropped from one of those would look exactly",
        "  like this. That axis is unmeasured, not clean.",
    ]


def render_drift(payload):
    payload = redact_home(payload)
    lines = ["RECEIPT SCHEMA DRIFT"]
    lines.append(f"  source:  {payload['source']}")
    lines.append(f"  version: {payload['version_key']}   shape: {_fmt(payload['fields_now'])} fields")
    if payload["version_fields_disagree"]:
        lines.append(
            "  WARNING: `version` and `dailyModelTokensVersion` disagree - "
            "this receipt is mid-migration"
        )
    lines.append("")
    if not payload["measurable"]:
        lines.append("  baseline: none at another version")
        lines.append("")
        lines.append("VERDICT: no_baseline")
        lines.append(
            "  No other receipt version has a shape recorded for this file, so a change"
        )
        lines.append(
            "  across versions is unmeasurable here - that needs the harness to publish a"
        )
        lines.append(
            "  different version. This version against its own last shape is measured below."
        )
        lines.append("")
        versions = ", ".join(payload["versions_recorded"]) or "none"
        lines.append(f"  versions recorded for this file: {versions}")
    else:
        lines.append(
            f"  baseline: {payload['baseline_version_key']} "
            f"({_fmt(payload['fields_baseline'])} fields)"
        )
        lines.append("")
        lines.append(f"VERDICT: {payload['verdict']}")
        if payload.get("regain_basis") == "cross_version" and payload["regained"]:
            lines.extend(
                _regain_block(
                    payload["regained"],
                    payload["meter_fields_regained"],
                    basis="cross_version",
                    reads=payload.get("baseline_reads"),
                )
            )
            lines.append("")
        if payload["verdict"] == "possible_short_baseline":
            part = payload.get("short_baseline") or {}
            lines.append(
                f"  this read holds {_fmt(part.get('fields_now', 0))} fields against the "
                f"{_fmt(part.get('fields_baseline', 0))} recorded at version "
                f"{payload['version_key']}, and changed nothing the record already had."
            )
            lines.append(
                "  Fields appearing at an unchanged version are what a vendor addition looks"
            )
            lines.append(
                "  like - and they are also what a single short read looks like once it is"
            )
            lines.append("  written down as this version's baseline. Only one read produced")
            lines.append("  that record, so it cannot settle which one this is:")
            for path in part.get("fields_unconfirmed") or []:
                lines.append(f"  + {path} (unconfirmed)")
            lines.append(
                "  The fuller read is what gets recorded: a truncated read cannot grow past"
            )
            lines.append("  the record it truncated, so this one is strictly more information.")
            lines.append(
                "  Run drift again on the settled file - the same shape twice makes it a"
            )
            lines.append("  baseline a later change can be measured against.")
        elif payload["verdict"] == "possible_partial_read":
            part = payload.get("partial_read") or {}
            lines.append(
                f"  this read holds {_fmt(part.get('fields_now', 0))} of the "
                f"{_fmt(part.get('fields_baseline', 0))} fields recorded at version "
                f"{payload['version_key']}, and nothing it kept changed type."
            )
            lines.append(
                "  A shorter but valid receipt is what a read taken while the file was being"
            )
            lines.append(
                "  written looks like - and it is also what a vendor deleting a subtree looks"
            )
            lines.append(
                "  like. Both axes below used this one read, so neither can name a deletion"
            )
            lines.append("  from it. The evidence, not yet a verdict:")
            for path in part.get("fields_unconfirmed") or []:
                marker = "!" if path in (part.get("priced_unconfirmed") or []) else " "
                lines.append(f"  {marker} {path} (unconfirmed)")
            lines.append(
                "  Run drift again on the settled file: the same smaller shape twice is a"
            )
            lines.append("  removal, the full shape again means this read was the partial one.")
        elif payload["verdict"] == "removal_unconfirmed":
            part = payload.get("cross_version_unconfirmed") or {}
            lines.append(
                f"  this is the first read at version {part.get('version_key')}. It holds "
                f"{_fmt(part.get('fields_now', 0))} of the"
            )
            lines.append(
                f"  {_fmt(part.get('fields_baseline', 0))} fields recorded at version "
                f"{part.get('baseline_version_key')}, and nothing it kept changed type."
            )
            lines.append(
                "  A vendor that removed a subtree and a read taken while the file was being"
            )
            lines.append(
                "  rewritten produce the same picture in one read, and this version has no"
            )
            lines.append("  earlier record to settle it. So no deletion is named here:")
            for path in part.get("fields_unconfirmed") or []:
                marker = "!" if path in (part.get("priced_unconfirmed") or []) else " "
                lines.append(f"  {marker} {path} (unconfirmed)")
            for row in part.get("narrowed_unconfirmed") or []:
                marker = "!" if row["path"] in (part.get("priced_unconfirmed") or []) else " "
                lines.append(
                    f"  {marker} {row['path']}: {row['now'][0]} of {row['now'][1]} "
                    f"{_family_noun(row['family'])} (was {row['was'][0]} of "
                    f"{row['was'][1]}) (unconfirmed)"
                )
            lines.append(
                "  Run drift again on the settled file: the same smaller shape twice at this"
            )
            lines.append(
                "  version is a removal; the fuller shape again means this read was the short one."
            )
        elif payload["verdict"] == "narrowing_unconfirmed":
            part = payload.get("cross_version_unconfirmed") or {}
            rows = part.get("narrowed_unconfirmed") or []
            lines.append(
                f"  this is the first read at version {part.get('version_key')}. Every field"
            )
            lines.append(
                f"  recorded at version {part.get('baseline_version_key')} is still there and"
            )
            lines.append("  nothing it kept changed type, but the fields below sit on fewer")
            lines.append("  family members than they did:")
            for row in rows:
                marker = "!" if row["path"] in (part.get("priced_unconfirmed") or []) else " "
                lines.append(
                    f"  {marker} {row['path']}: {row['now'][0]} of {row['now'][1]} "
                    f"{_family_noun(row['family'])} (was {row['was'][0]} of "
                    f"{row['was'][1]}) (unconfirmed)"
                )
            lines.append(
                "  A vendor that narrowed a field and a read taken while the file was being"
            )
            lines.append(
                "  rewritten produce the same picture in one read, and this version has no"
            )
            lines.append("  earlier record to settle it. So no narrowing is named here.")
            lines.append(
                "  Run drift again on the settled file: the same narrower shape twice at this"
            )
            lines.append(
                "  version is a narrowing; the fuller shape again means this read was the short one."
            )
        elif payload["meter_fields_removed"]:
            lines.append(
                f"  {len(payload['meter_fields_removed'])} removed field(s) carry a price "
                "(marked ! below):"
            )
            lines.append(
                "  A meter that reads a removed field shows nothing, not an error - this is the"
            )
            lines.append(
                "  opencode 2.2.0 shape: no spend at all for a session that was billed."
            )
        if payload["meter_fields_narrowed"] and payload["verdict"] not in (
            "narrowing_unconfirmed",
            "possible_narrowing",
        ):
            lines.append(
                "  A field that carries a price is still in the receipt, just not on every model:"
            )
            lines.append(
                "  a meter that sums across models now undercounts by the ones that lost it,"
            )
            lines.append("  and nothing in the payload says so.")
        if payload["meter_fields_altered"]:
            lines.append("  A field that carries a price changed type:")
            for row in payload["type_changed"]:
                if row["path"] in payload["meter_fields_altered"]:
                    lines.append(f"    {row['path']}: {row['was']} -> {row['now']}")
        if payload["narrowed"] and payload["verdict"] not in (
            "narrowing_unconfirmed",
            "removal_unconfirmed",
            "possible_narrowing",
        ):
            lines.append("")
            lines.append(
                f"NARROWED ({len(payload['narrowed'])}) - still there, on fewer members than before"
            )
            for row in payload["narrowed"]:
                marker = "!" if row["path"] in payload["meter_fields_narrowed"] else " "
                lines.append(
                    f"  {marker} {row['path']}: {row['now'][0]} of {row['now'][1]} "
                    f"{_family_noun(row['family'])} (was {row['was'][0]} of {row['was'][1]})"
                )
        if payload["removed"]:
            lines.append("")
            lines.append(f"REMOVED ({len(payload['removed'])})")
            for path in payload["removed"]:
                marker = "!" if path in payload["meter_fields_removed"] else " "
                lines.append(f"  {marker} {path}")
        if payload["added"] and payload["verdict"] != "possible_short_baseline":
            lines.append("")
            lines.append(f"ADDED ({len(payload['added'])})")
            for path in payload["added"]:
                lines.append(f"  + {path}")
        if payload["type_changed"]:
            other = [
                r for r in payload["type_changed"] if r["path"] not in payload["meter_fields_altered"]
            ]
            if other:
                lines.append("")
                lines.append(f"TYPE CHANGED ({len(other)})")
                for row in other:
                    lines.append(f"  ~ {row['path']}: {row['was']} -> {row['now']}")
    lines.append("")
    within = payload["within_version"]
    lines.append(f"SINCE THE LAST SHAPE AT THIS VERSION ({payload['version_key']})")
    if not within["measurable"]:
        lines.append(f"  not measured -- {within['reason_text']}")
    else:
        when = within["baseline_at"] or "before timestamps were kept"
        lines.append(f"  baseline: recorded {when} ({_fmt(within['fields_baseline'])} fields)")
        lines.append(f"VERDICT: {within['verdict']}")
        if within["verdict"] == "unchanged":
            lines.append(
                f"  identical to the shape recorded {when}: 0 removed, 0 added,"
                " 0 retyped, no priced field lost a carrier."
            )
        elif within["verdict"] == "unchanged_shape_only":
            lines.extend(_presence_gap_lines(within["presence_axes_unmeasured"]))
        elif within["verdict"] == "possible_short_baseline":
            part = within["short_baseline"]
            lines.append(
                f"  this read holds {_fmt(part['fields_now'])} fields against the "
                f"{_fmt(part['fields_baseline'])} recorded {when}, and changed nothing the"
            )
            lines.append("  record already had - it only has more.")
            lines.append("")
            lines.append(
                "  Fields appearing at an unchanged version are what a vendor addition looks"
            )
            lines.append(
                "  like. They are also what this tool looks like when its single earlier read"
            )
            lines.append(
                "  was short. One unconfirmed record cannot separate those two, so nothing"
            )
            lines.append("  below is reported as added yet:")
            for path in part["fields_unconfirmed"]:
                lines.append(f"    + {path} (unconfirmed)")
            lines.append("")
            lines.append(
                "  The fuller read becomes the baseline - it keeps every field and type the"
            )
            lines.append(
                "  old one had, and a truncated read cannot grow past the record it truncated."
            )
            lines.append(
                "  Run drift again: the same shape twice makes it a record, and only then is a"
            )
            lines.append("  difference measured against it a vendor change.")
        elif within["verdict"] == "possible_partial_read":
            part = within["partial_read"]
            lines.append(
                f"  baseline kept ({_fmt(part['fields_baseline'])} fields, recorded {when});"
                f" this read holds {_fmt(part['fields_now'])}."
            )
            lines.append(
                "  A valid receipt missing a whole subtree is what a read taken while the file"
            )
            lines.append(
                "  was being written looks like. It is also what a vendor deletion looks like,"
            )
            lines.append(
                "  and one read cannot separate the two - so the baseline was not replaced by"
            )
            lines.append("  this one, and nothing below is reported as removed yet:")
            for path in part["fields_unconfirmed"]:
                marker = "!" if path in part["priced_unconfirmed"] else " "
                lines.append(f"    {marker} {path} (unconfirmed)")
            lines.append("")
            lines.append(
                "  Run drift again on the settled file. The same smaller shape twice makes the"
            )
            lines.append(
                "  removal real; the full shape again makes this read the partial one."
            )
        elif within["verdict"] == "possible_narrowing":
            part = within["possible_narrowing"]
            lines.append(
                f"  baseline kept ({_fmt(part['fields_baseline'])} fields, recorded {when});"
                " every path and type is still here."
            )
            lines.append(
                "  What fell is the number of family members carrying them. A vendor"
                " narrowing a field and a receipt read while it was being written - two of"
            )
            lines.append(
                "  three model objects emitted - produce this same picture in one read, so"
                " no narrowing is named here:"
            )
            for row in part["rows"]:
                marker = "!" if row["path"] in part["priced_unconfirmed"] else " "
                lines.append(
                    f"    {marker} {row['path']}: {row['now'][0]} of {row['now'][1]} "
                    f"{_family_noun(row['family'])}"
                    f" (was {row['was'][0]} of {row['was'][1]}) (unconfirmed)"
                )
            lines.append("")
            lines.append(
                "  Run drift again on the settled file: the same narrower counts twice make"
            )
            lines.append(
                "  the narrowing the record; the older counts again mean this read was the"
            )
            lines.append("  short one.")
        elif within["meter_fields_removed"]:
            lines.append(
                "  a field that carried a price was removed WITHOUT a version change:"
            )
            for path in within["meter_fields_removed"]:
                lines.append(f"    ! {path}")
        elif within["narrowed"]:
            lines.append(
                "  a field is still present but on fewer members, WITHOUT a version change:"
            )
            for row in within["narrowed"]:
                marker = "!" if row["path"] in within["meter_fields_narrowed"] else " "
                lines.append(
                    f"    {marker} {row['path']}: {row['now'][0]} of {row['now'][1]} "
                    f"{_family_noun(row['family'])} (was {row['was'][0]} of {row['was'][1]})"
                )
        elif within["regained"]:
            when_record = within.get("reversed_narrowing_at")
            lines.extend(_regain_block(within["regained"], within["meter_fields_regained"]))
            lines.append("")
            if when_record:
                lines.append(
                    f"  The narrowing that made this the record was confirmed at {when_record}:"
                )
                lines.append(
                    "  the field lost a carrier on a second agreeing read. This read has it back."
                )
            else:
                lines.append(
                    "  The record had lost this carrier; this read has it again. Nothing here"
                )
                lines.append("  says when it came back.")
            lines.append(
                "  A stale copy of the older bytes and a record that was simply wrong look the"
            )
            lines.append(
                "  same from here, so the return is not attributed to the vendor."
            )
        else:
            if within["meter_fields_altered"]:
                lines.append("  a field that carries a price changed type, same version:")
                for row in within["type_changed"]:
                    if row["path"] in within["meter_fields_altered"]:
                        lines.append(f"    {row['path']}: {row['was']} -> {row['now']}")
            lines.append(
                f"  removed {len(within['removed'])}, added {len(within['added'])},"
                f" retyped {len(within['type_changed'])}"
            )
            for path in within["removed"]:
                lines.append(f"    - {path}")
        if within["regained"] and within["verdict"] not in (
            "meter_field_regained",
            "field_regained",
        ):
            # The dispatch above gave the screen to a removal, a type change or a
            # guard. A presence gain is still a measured fact about this record and
            # must not vanish because a bigger verdict took the paragraph.
            lines.append("")
            lines.extend(_regain_block(within["regained"], within["meter_fields_regained"]))
            if within.get("reversed_narrowing_at"):
                lines.append(
                    "  Contradicts the narrowing confirmed at "
                    f"{within['reversed_narrowing_at']}."
                )
            else:
                lines.append("  The record had lost this carrier; this read has it again.")
        if within.get("confirmed_removal"):
            lines.append("")
            lines.append(
                "  This smaller shape has now been read twice at this version, so it is recorded:"
            )
            lines.append("  two agreeing reads are what a half-written file cannot produce.")
        if within.get("confirmed_narrowing"):
            lines.append("")
            lines.append(
                "  These narrower counts have now been read twice at this version, so they"
            )
            lines.append(
                "  are the record: an incrementally written file cannot produce them twice."
            )
        if within["verdict"] != "unchanged":
            lines.append("")
            lines.append(
                "  A version number is the vendor's claim that the shape did not move."
            )
            lines.append("  This section is the check on that claim.")
    lines.append("")
    lines.append(
        "LIMIT: names and types only. A field that keeps its name and type but changes"
    )
    lines.append(
        "meaning (a unit, a currency), or one that stays present and always 0, is invisible here."
    )
    lines.append("LIMIT: no values are stored - only paths, type names and presence counts.")
    lines.append(
        "LIMIT: the first read at a brand-new version has no same-version record to be"
    )
    lines.append(
        "checked against, so a partial read there becomes that version's baseline untested."
    )
    lines.append(
        "A later fuller read at that version is then reported as possible_short_baseline, not"
    )
    lines.append(
        "as a vendor addition - but if every read at that version is short the same way, the"
    )
    lines.append("record stays short and nothing here can tell.")
    lines.append(
        "LIMIT: a removal or a narrowing seen once across a version change is unconfirmed,"
    )
    lines.append(
        "never a verdict - both need the same shape read twice at that version. When the"
    )
    lines.append(
        "version does not change, a narrowing is confirmed on the counts instead (the path"
    )
    lines.append(
        "set does not move): moving counts are a fresh suspicion every time, not a verdict."
    )
    lines.append(
        "LIMIT: a list that rolls over (a window of days) can lose a carrier because an old"
    )
    lines.append(
        "member left the window, not because the vendor dropped the field, and the count"
    )
    lines.append("cannot tell those two apart. Two lists under different keys also fold into")
    lines.append("one count, so '2 of 8' can mean 1 of 3 plus 1 of 5.")
    lines.append(
        "LIMIT: a carrier that comes back is named only against this version's own record."
    )
    lines.append(
        "Across a version change it is reported as part of the ordinary difference, and a"
    )
    lines.append(
        "stale copy of the older bytes looks exactly like a return that happened just now."
    )
    return "\n".join(lines) + "\n"


# A receipt does not only describe the present. Read the same key twice and the
# second reading can carry a *smaller* number, because the vendor recomputed
# history. That is the one direction a monotone counter cannot legitimately take,
# so a decrease is the only change here that needs no further explanation - and
# it is a rewrite of the record, not less work having happened.
READINGS_SCHEMA = 1

# Bounded on purpose: two readings make a revision visible, five make a trend
# visible, and a year of readings of a 200-day receipt is somebody's whole
# history sitting in a cache file for no reason.
MAX_READINGS = 5

# A digest can say that something moved; only the per-file facts can say what.
# The manifest is therefore opt-in (`--manifest`) and capped: a receipt block
# that carries a million file names is not a receipt any more, and a capped
# manifest must refuse to name anything rather than name a subset and look
# complete.
MANIFEST_LIMIT = 2000
ATTRIBUTION_LIMIT = 10

READING_STORE_DEFAULT = Path.home() / ".cache" / "quotamirror" / "readings.json"


def reading_of(receipt):
    """The token numbers a second reading must agree with, keyed by what they count.

    Two families, both non-decreasing by construction: a per-model lifetime total
    and one cell per (day, model). Message counts are deliberately left out - they
    are a different unit, and mixing units is what makes a delta unreadable.
    """
    values = {}
    models = receipt.get("modelUsage") or {}
    if isinstance(models, dict):
        for name, usage in models.items():
            if isinstance(usage, dict):
                values[f"model:{name}"] = _tokens_for(usage)
    days = receipt.get("dailyModelTokens") or []
    if isinstance(days, list):
        for entry in days:
            if not isinstance(entry, dict):
                continue
            day = entry.get("date")
            by_model = entry.get("tokensByModel") or {}
            if not isinstance(day, str) or not isinstance(by_model, dict):
                continue
            for name, tokens in by_model.items():
                if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
                    values[f"day:{day}:{name}"] = int(tokens)
    key, _ = _version_of(receipt)
    return {
        "values": values,
        "version_key": key,
        "last_computed": receipt.get("lastComputedDate"),
    }


def load_readings(store_path):
    try:
        with open(store_path, "r", encoding="utf-8") as handle:
            store = json.load(handle)
    except FileNotFoundError:
        return {"schema": READINGS_SCHEMA, "caches": {}}
    if not isinstance(store, dict) or not isinstance(store.get("caches"), dict):
        raise ValueError(f"{store_path} is not a quotamirror readings store")
    return store


def record_reading(store_path, cache_path, receipt, recorded_at=None):
    """Append this receipt's numbers under its path digest, keeping the newest few."""
    store_path = Path(store_path)
    if store_path.is_symlink():
        raise ValueError(f"refusing symlink readings store: {store_path}")
    store = load_readings(store_path)
    if store.get("schema") != READINGS_SCHEMA:
        store = {"schema": READINGS_SCHEMA, "caches": {}}
    reading = reading_of(receipt)
    reading["at"] = recorded_at or _utc_stamp()
    entry = store["caches"].setdefault(path_identity(cache_path), {})
    readings = entry.setdefault("readings", [])
    readings.append(reading)
    del readings[:-MAX_READINGS]
    store_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{store_path.name}.", dir=store_path.parent)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(store, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        if store_path.is_symlink():
            raise ValueError(f"refusing symlink readings store: {store_path}")
        os.replace(temporary, store_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return store, reading


def revise_report(store, cache_path, receipt):
    """Compare this receipt's numbers with the most recent earlier reading."""
    current = reading_of(receipt)
    entry = (store.get("caches") or {}).get(path_identity(cache_path)) or {}
    readings = entry.get("readings") or []
    earlier = readings[-1] if readings else None
    payload = {
        "source": str(cache_path),
        "version_key": current["version_key"],
        "last_computed": current["last_computed"],
        "keys_now": len(current["values"]),
        "keys_earlier": len(earlier["values"]) if earlier else 0,
        "baseline_at": earlier.get("at") if earlier else None,
        "baseline_version_key": earlier.get("version_key") if earlier else None,
        "baseline_last_computed": earlier.get("last_computed") if earlier else None,
        "measurable": bool(earlier),
        "verdict": "no_baseline",
        "compared": 0,
        "down": 0,
        "up": 0,
        "flat": 0,
        "net_delta": 0,
        "closed_day_changes": 0,
        "appeared": [],
        "vanished": [],
        "rows": [],
    }
    if not earlier:
        return payload

    before = earlier.get("values") or {}
    now = current["values"]
    payload["appeared"] = sorted(set(now) - set(before))
    payload["vanished"] = sorted(set(before) - set(now))
    closed_before = earlier.get("last_computed")

    rows = []
    for key in sorted(set(before) & set(now)):
        delta = now[key] - before[key]
        if delta < 0:
            verdict = "revised_down"
        elif delta > 0:
            verdict = "increased"
        else:
            verdict = "unchanged"
        closed = False
        if key.startswith("day:") and isinstance(closed_before, str):
            # the earlier receipt already declared this day computed, so no new
            # work can be attributed to it; a change here is the entry itself
            closed = key.split(":", 2)[1] < closed_before
        rows.append(
            {
                "key": key,
                "earlier": before[key],
                "now": now[key],
                "delta": delta,
                "verdict": verdict,
                "closed": closed,
            }
        )

    payload["rows"] = rows
    payload["compared"] = len(rows)
    payload["down"] = sum(1 for row in rows if row["delta"] < 0)
    payload["up"] = sum(1 for row in rows if row["delta"] > 0)
    payload["flat"] = len(rows) - payload["down"] - payload["up"]
    payload["net_delta"] = sum(row["delta"] for row in rows)
    payload["closed_day_changes"] = sum(
        1 for row in rows if row["closed"] and row["delta"] != 0
    )
    if payload["down"]:
        payload["verdict"] = "history_revised"
    elif payload["up"]:
        payload["verdict"] = "growth_only"
    elif rows:
        payload["verdict"] = "unchanged"
    else:
        payload["verdict"] = "not_comparable"
    return payload


def _signed(value):
    return f"+{value:,}" if value > 0 else f"{value:,}"


def _date_reading(value):
    """A date that is absent from the receipt is named, not printed as None."""
    return value if value else "not recorded in this receipt"


def render_revise(payload, recorded=True):
    lines = ["RECEIPT REVISIONS (the same keys, read twice)"]
    lines.append(f"  source:   {payload['source']}")
    lines.append(
        f"  receipt:  version {payload['version_key']}, "
        f"lastComputedDate: {_date_reading(payload['last_computed'])}"
    )
    lines.append("")
    if not payload["measurable"]:
        lines.append("  this is the first reading of these keys: there is no earlier reading")
        lines.append("  in the store, so nothing can be compared yet.")
        lines.append(f"  recorded: {payload['keys_now']} keys.")
        if payload["keys_now"] == 0:
            lines.append("")
            lines.append(
                "  this receipt carries no monotone token counter, so there is no key a"
            )
            lines.append(
                "  second reading could revise: read again and it will still be 0 keys."
            )
        elif recorded:
            lines.append(
                "  run `quotamirror revise` again later - the second reading is what makes"
            )
            lines.append("  a revision visible; one reading can only show the present.")
        else:
            lines.append(
                "  --no-record: nothing was written, so the next run will also be a first reading."
            )
        return "\n".join(lines) + "\n"

    lines.append(
        f"  earlier:  {payload['baseline_at']}  version {payload['baseline_version_key']}, "
        f"lastComputedDate: {_date_reading(payload['baseline_last_computed'])}"
    )
    lines.append(
        f"  now:      version {payload['version_key']}, "
        f"lastComputedDate: {_date_reading(payload['last_computed'])}"
    )
    lines.append("")
    lines.append(f"  {'key'.ljust(44)} {'earlier':>14} {'now':>14} {'delta':>14}")
    for row in payload["rows"]:
        mark = "" if row["verdict"] == "unchanged" else f"  {row['verdict']}"
        if row["closed"] and row["delta"] != 0:
            mark += " (closed day)"
        lines.append(
            f"  {row['key'].ljust(44)} {row['earlier']:>14,} {row['now']:>14,} "
            f"{_signed(row['delta']):>14}{mark}"
        )
    lines.append("")
    if payload["compared"] == 0:
        lines.append(
            f"  keys compared: 0 of {payload['keys_earlier']} earlier / "
            f"{payload['keys_now']} now; net: nothing to sum - no key is present in both readings."
        )
    else:
        lines.append(
            f"  keys compared: {payload['compared']} of {payload['keys_earlier']} earlier / "
            f"{payload['keys_now']} now; {payload['down']} down, {payload['up']} up, "
            f"{payload['flat']} unchanged; net {_signed(payload['net_delta'])} tokens."
        )
    if payload["appeared"]:
        lines.append(
            f"  appeared in this reading only ({len(payload['appeared'])}): "
            f"{_sample(payload['appeared'], 4)}"
        )
    if payload["vanished"]:
        lines.append(
            f"  present earlier, absent now ({len(payload['vanished'])}): "
            f"{_sample(payload['vanished'], 4)}"
        )
        lines.append(
            "  absent is not zero: a key that stopped being published was not revised"
            " downwards, and it is not counted above."
        )
    lines.append(f"  verdict: {payload['verdict']}")
    lines.append("")
    if payload["down"]:
        lines.append(
            "  A key that reads lower the second time was revised: the receipt's past was"
        )
        lines.append(
            "  rewritten. It is not less usage - the same key cannot un-spend tokens - and"
        )
        lines.append("  this command will not call it a drop in consumption.")
    elif payload["up"]:
        lines.append(
            "  Every change is upward, which is what new work looks like. Two readings"
        )
        lines.append(
            "  cannot separate new work from a late write onto an older day; only the"
        )
        lines.append("  downward direction is unambiguous.")
    elif payload["compared"]:
        lines.append("  Both readings carry the same numbers for every comparable key.")
    else:
        lines.append(
            "  No key is present in both readings, so no revision can be stated either way."
        )
    # A closed day is not only interesting when the number went down: the earlier
    # receipt had already declared it computed, so a change in either direction is
    # the entry being rewritten rather than work landing on it.
    if payload["closed_day_changes"]:
        count = payload["closed_day_changes"]
        if count == 1:
            lines.append(
                "  1 changed key sits on a day the earlier receipt had already declared"
            )
        else:
            lines.append(
                f"  {count} changed keys sit on days the earlier receipt had already declared"
            )
        lines.append(
            "  computed, where a change is the record being rewritten rather than work"
        )
        lines.append("  being added.")
    lines.append("  LIMIT: only downloaded receipts are read; a revision made before the")
    lines.append("  earlier reading, or after this one, is outside both snapshots.")
    return "\n".join(lines) + "\n"




# A rollout's own session_meta says whether it is a root thread or a delegated
# one, and whether it was forked from another thread. Those are the only fields
# in either harness that name a scope, so they are what the stamp is built from.
THREAD_KEYS = (
    "thread_source",
    "parent_thread_id",
    "agent_nickname",
    "forked_from_id",
    "subagent_history_start_ordinal",
)

# Word-aware scope tokens for scanning a receipt's field names. "longestSession"
# is not a scope; "sidechainTokens" is. A name proves a declaration, never a value.
SCOPE_WORDS = {
    "sidechain",
    "subagent",
    "subagents",
    "descendant",
    "descendants",
    "parent",
    "child",
    "children",
    "fork",
    "forked",
    "spawn",
    "spawned",
    "scope",
    "nested",
}

STAMP_SCOPES = ("root", "sidechain", "root+sidechain", "unknown")
WINDOW_BASES = ("record_timestamp", "declared_attribution", "none")

CODEX_MARKERS = (
    '"token_count"',
    '"token_usage_record"',
    '"session_meta"',
    '"turn_context"',
    '"function_call"',
    '"custom_tool_call"',
    '"message"',
)

# Verbs that only ask "is it done yet?" / "are you there?". A turn whose every
# call is one of these bought waiting, not work.
COORDINATION_TOOLS = ("wait", "wait_agent", "list_agents", "write_stdin", "sleep")


def _codex_usage_total(usage):
    """Tokens a single Codex usage object reports, or None when it reports none."""
    if not isinstance(usage, dict):
        return None
    if isinstance(usage.get("total_tokens"), (int, float)):
        return int(usage["total_tokens"])
    parts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
    if any(isinstance(p, (int, float)) for p in parts):
        return sum(int(p or 0) for p in parts)
    return None


def read_codex_session(path):
    """One Codex CLI rollout file: its running-total column, its deltas, its second writer.

    Also reconstructs TURNS: a `token_count` event closes one, carrying that
    turn's own delta; its tool calls are every `function_call` /
    `custom_tool_call` response item seen since the previous `token_count`.
    Calls left over at end of file belong to no closed turn and are counted in
    `unattributed_calls` rather than being attached to the wrong one.

    Lines are pre-filtered on the markers this reader understands, because a
    rollout line can hold an entire file payload; only matching lines are parsed.
    A truncated line is counted (`skipped_lines`), never fatal.
    """
    events = []
    records = []
    turns = []
    pending_calls = []
    pending_boundary = False
    model = None
    session_id = None
    thread = {}
    skipped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not any(marker in line for marker in CODEX_MARKERS):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            kind = obj.get("type")
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                session_id = payload.get("session_id") or session_id
                model = payload.get("model") or model
                for key in THREAD_KEYS:
                    if payload.get(key) is not None:
                        thread[key] = payload[key]
            elif kind == "turn_context":
                model = payload.get("model") or model
            elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
                name = payload.get("name")
                if isinstance(name, str) and name:
                    pending_calls.append(name)
            elif kind == "response_item" and payload.get("type") == "message":
                # A user message opens the coarser unit a human would call "a turn".
                # It is recorded as a boundary, never merged into a tool call.
                if payload.get("role") == "user":
                    pending_boundary = True
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                running = _codex_usage_total(info.get("total_token_usage"))
                if running is None:
                    continue
                delta = _codex_usage_total(info.get("last_token_usage"))
                events.append(
                    {
                        "timestamp": obj.get("timestamp"),
                        "running": running,
                        "delta": 0 if delta is None else delta,
                    }
                )
                turns.append(
                    {
                        "timestamp": obj.get("timestamp"),
                        "delta": 0 if delta is None else delta,
                        "calls": pending_calls,
                        "starts_user_turn": pending_boundary,
                    }
                )
                pending_calls = []
                pending_boundary = False
            elif kind == "token_usage_record":
                tokens = _codex_usage_total(payload.get("usage"))
                if tokens is not None:
                    records.append({"timestamp": obj.get("timestamp"), "tokens": tokens})
    return {
        "path": str(path),
        "session_id": session_id,
        "model": model or "unknown",
        "thread": thread,
        "events": events,
        "records": records,
        "turns": turns,
        "unattributed_calls": len(pending_calls),
        "skipped_lines": skipped,
    }


def _codex_day(timestamp):
    if not isinstance(timestamp, str) or len(timestamp) < 10:
        return None
    day = timestamp[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return day


def _witnesses(delta_total, running_total, records_total):
    """Three numbers describe one quantity. Say which of them agree, and how far apart."""
    entry = {
        "delta_total": delta_total,
        "running_total": running_total,
        "records_total": records_total,
        "agreement": "none",
        "note": "",
    }
    if running_total is None:
        if records_total is None:
            entry["agreement"] = "no_witnesses"
            entry["note"] = "neither column is present"
        else:
            entry["agreement"] = "records_only"
            entry["note"] = "only the per-response records exist; no total is claimed"
        return entry

    delta_ok = delta_total == running_total
    records_ok = records_total is not None and records_total == running_total
    if records_total is None:
        entry["agreement"] = "delta+running" if delta_ok else "none"
        entry["note"] = (
            "per-response records are absent; two witnesses agree"
            if delta_ok
            else "per-response records are absent and the two witnesses differ"
        )
    elif delta_ok and records_ok:
        entry["agreement"] = "all_three"
        entry["note"] = "three independent numbers, one total"
    elif records_ok:
        entry["agreement"] = "running_total+records"
        diff = delta_total - running_total
        entry["note"] = (
            f"deltas overstate the running total by {diff:,}" if diff > 0
            else f"deltas are short of the running total by {-diff:,}"
        )
    elif delta_ok:
        entry["agreement"] = "delta+running"
        diff = records_total - running_total
        entry["note"] = (
            f"per-response records exceed the running total by {diff:,}" if diff > 0
            else f"per-response records are short by {-diff:,}"
        )
    elif records_total == delta_total:
        entry["agreement"] = "delta+records"
        entry["note"] = (
            f"the running total differs from both by {running_total - delta_total:,}"
        )
    else:
        entry["agreement"] = "none"
        entry["note"] = (
            f"three witnesses, three totals (delta {delta_total:,}, running "
            f"{running_total:,}, records {records_total:,}); no total is claimed"
        )
    return entry


def audit_codex_session(session):
    """Apply the cumulative rule to a harness that writes both columns.

    The delta column is the only thing summed. A running total that goes
    backwards is named as a reset and no total is inferred from it.
    """
    audit = {
        "path": session["path"],
        "session_id": session.get("session_id"),
        "model": session.get("model", "unknown"),
        "events": len(session["events"]),
        "skipped_lines": session["skipped_lines"],
        "verdict": "no_token_events",
        "delta_total": None,
        "running_total": None,
        "naive_sum": None,
        "naive_factor": None,
        "gap": None,
        "gap_note": "",
        "resets": [],
        "day_totals": {},
        "two_writers": {"verdict": "no_records", "gap": None},
        "witnesses": None,
        "thread": session.get("thread") or {},
        "first_running": None,
    }
    events = session["events"]
    records = session["records"]
    records_total = sum(r["tokens"] for r in records) if records else None

    if not events:
        audit["witnesses"] = _witnesses(None, None, records_total)
        if records:
            audit["two_writers"] = {
                "verdict": "records_only",
                "records_total": sum(r["tokens"] for r in records),
                "gap": None,
                "note": "per-response records exist but no running total does",
            }
        return audit

    running = [e["running"] for e in events]
    audit["first_running"] = running[0]

    day_totals = {}
    for event in events:
        day = _codex_day(event["timestamp"])
        if day is None:
            continue
        day_totals[day] = day_totals.get(day, 0) + event["delta"]
    audit["day_totals"] = day_totals

    delta_total = sum(e["delta"] for e in events)
    peak = max(running)
    audit["delta_total"] = delta_total
    audit["running_total"] = peak
    audit["naive_sum"] = sum(running)
    if peak > 0:
        audit["naive_factor"] = round(sum(running) / peak, 2)

    resets = [
        {"event_index": i, "previous": running[i - 1], "current": running[i]}
        for i in range(1, len(running))
        if running[i] < running[i - 1]
    ]
    audit["resets"] = resets

    if resets:
        audit["verdict"] = "counter_reset"
        audit["gap_note"] = (
            f"the running total went backwards {len(resets)} time(s); the delta column is "
            f"summed to {delta_total:,} and no lifetime total is claimed"
        )
    elif delta_total == peak:
        audit["verdict"] = "cumulative_confirmed"
        audit["gap_note"] = (
            f"the deltas sum to the last running total ({peak:,}); summing the running "
            f"column instead reports {sum(running):,}"
        )
    else:
        audit["verdict"] = "cumulative_mismatch"
        gap = peak - delta_total
        audit["gap"] = gap
        if gap > 0:
            audit["gap_note"] = (
                f"deltas are short by {gap:,} of the running total ({delta_total:,} vs {peak:,})"
            )
        else:
            audit["gap_note"] = (
                f"deltas exceed the running total by {-gap:,} ({delta_total:,} vs {peak:,})"
            )

    if records:
        gap = records_total - peak
        audit["two_writers"] = {
            "verdict": "two_writers_agree" if gap == 0 else "two_writers_disagree",
            "records_total": records_total,
            "gap": gap,
            "note": (
                "per-response records and the running total agree"
                if gap == 0
                else (
                    f"per-response records are short by {-gap:,}" if gap < 0
                    else f"per-response records exceed the running total by {gap:,}"
                )
            ),
        }
    audit["witnesses"] = _witnesses(delta_total, peak, records_total)
    return audit


def _scope_fields(receipt):
    """Named fields in a receipt whose words declare a scope. A name is a claim, not a value."""
    names = []
    for path in shape_of(receipt):
        leaf = path.rsplit(".", 1)[-1].replace("[]", "")
        if SCOPE_WORDS & set(_words(leaf)):
            names.append(path)
    return sorted(set(names))


def _observation(files, usage_records):
    """Was anything read at all? A zero has to say which kind of zero it is.

    `store_empty` -- no transcript file was even there to read.
    `no_usage_records` -- files were read and none carried a usage block.
    `usage_observed` -- at least one usage record was read.
    """
    count = len(files)
    if count == 0:
        verdict = "store_empty"
    elif not usage_records:
        verdict = "no_usage_records"
    else:
        verdict = "usage_observed"
    return {
        "verdict": verdict,
        "files": count,
        "usage_records": usage_records,
    }


def _observation_text(observation):
    """One line saying how much was read. Shared so every output face uses it."""
    return (
        f"{observation['verdict']} -- {observation['files']} file(s) read, "
        f"{observation['usage_records']:,} usage record(s) read"
    )


NOT_READ = (
    "not_read -- --claude-transcripts was not passed, so no transcript file was read"
)


def _observation_line(observation, note_when_absent=NOT_READ):
    """The printed form of an observation, on every face that compares totals."""
    body = _observation_text(observation) if observation else note_when_absent
    return f"  observation   : {body}"


def _coverage_note(usage_records, untimed):
    """One sentence about what a printed window actually bounds.

    A scan that read nothing has no window at all, so the honest sentence is not
    "the window bounds every record" (vacuously true over zero records) but that
    nothing was measured.
    """
    if not usage_records:
        return (
            "no usage record was read, so no window was measured -- an empty scan is an "
            "absence of observation, not an observation of zero usage"
        )
    if untimed:
        return (
            f"{untimed} usage record(s) carry no timestamp, so the printed window does not bound "
            "their records; their tokens are still in the total"
        )
    return "every usage record carried a timestamp, so the printed window bounds all records read"


def _window(days, basis="none"):
    """Which days a total covers and how that coverage was learned.

    `record_timestamp` means the edges were measured from records this command
    read; `declared_attribution` means a file's own day table said so. The two
    can disagree on the same machine, so the basis travels with the range.
    """
    if basis not in WINDOW_BASES:
        raise ValueError(f"unknown window basis: {basis}")
    ordered = sorted(d for d in days if d)
    if not ordered:
        return {"first": None, "last": None, "days": 0, "basis": basis}
    return {
        "first": ordered[0],
        "last": ordered[-1],
        "days": len(ordered),
        "basis": basis,
    }


def _stamp(total_tokens, includes_descendants, accounting, window, basis, scope_source, **extra):
    """One total stamped with scope, accounting basis, and window.

    `includes_descendants` answers "does this number contain delegated work?";
    `accounting` answers "is this an increment or a running total?". A total
    missing either one cannot be checked by whoever reads it.
    """
    if includes_descendants not in STAMP_SCOPES:
        raise ValueError(f"unknown scope stamp: {includes_descendants}")
    if accounting not in ("delta", "cumulative"):
        raise ValueError(f"unknown accounting stamp: {accounting}")
    stamp = {
        "total_tokens": total_tokens,
        "includes_descendants": includes_descendants,
        "accounting": accounting,
        "window": window,
        "basis": basis,
        "scope_source": scope_source,
    }
    stamp.update(extra)
    return stamp


def _comparability(left, right):
    """Whether two totals cover the same days. Unknown windows are not comparable."""
    lw, rw = left["window"], right["window"]
    if lw["first"] is None or rw["first"] is None:
        return {
            "same_window": "unknown",
            "note": (
                "at least one total declares no dates, so the two cannot be placed on the "
                "same timeline; their difference has no interpretation"
            ),
        }
    if (lw["first"], lw["last"]) == (rw["first"], rw["last"]):
        return {"same_window": True, "note": f"both totals cover {lw['first']}..{lw['last']}"}
    return {
        "same_window": False,
        "note": (
            f"windows differ ({lw['first']}..{lw['last']} vs {rw['first']}..{rw['last']}); "
            "these totals describe different periods and must not be subtracted"
        ),
    }


def _claude_window_pair(claude_days, cache_window, claude_transcripts):
    """Compare a cache-declared window with transcript-record timestamps.

    The cache's day table declares its own range; the transcripts carry record
    timestamps for a second measured range. The result names calendar and
    counting differences without assuming either total is authoritative.
    """
    unavailable = {
        "available": False,
        "reason": (
            "this command reads the cache's declared day table but not the transcripts behind it; "
            "pass --claude-transcripts <dir> to measure the other window here"
        ),
        "cache": cache_window,
        "transcript": None,
        "transcript_scan": None,
        "observation": None,
    }
    if claude_transcripts is None:
        return unavailable

    scan = transcript_window(claude_transcripts)
    observation = scan["observation"]
    if observation["verdict"] != "usage_observed":
        # Nothing was read, or nothing read carried usage: either way there is no
        # second window to place beside the cache's, and the zero must say which.
        detail = {
            "store_empty": "no transcript file was read under --claude-transcripts",
            "no_usage_records": (
                f"{observation['files']} transcript file(s) were read under --claude-transcripts and "
                "none carried a usage record"
            ),
        }[observation["verdict"]]
        return {
            "available": False,
            "reason": (
                f"{observation['verdict']} -- {detail}, so the second window was never measured "
                "(an empty scan is an absence of observation, not an observation of zero usage)"
            ),
            "cache": cache_window,
            "transcript": None,
            "transcript_scan": None,
            "observation": observation,
        }
    transcript_days = set(scan["day_totals"])
    cache_days = set(claude_days)
    table = _table_overlap(transcript_days, cache_days, "transcript", "cache table")
    only_transcript = table["days_left_only"]
    only_cache = table["days_right_only"]
    both = table["days_both"]
    tokens_only_transcript = sum(scan["day_totals"][day] for day in only_transcript)
    total = scan["tokens"]
    transcript_shared = sum(scan["day_totals"][day] for day in both)
    cache_shared = sum(claude_days[day] for day in both)
    gap = transcript_shared - cache_shared
    if not both:
        residual_note = (
            "no day is held by both counters, so nothing can be said about a counting difference"
        )
    elif gap == 0:
        residual_note = (
            f"on the {len(both)} day(s) both counters hold, the two agree exactly -- so the calendar "
            "difference is the whole difference; no counting difference has to be posited"
        )
    else:
        residual_note = (
            f"on the {len(both)} day(s) both counters hold, the transcript scan reads "
            f"{abs(gap):,} token(s) {'more' if gap > 0 else 'less'} than the cache table -- a counting "
            "difference, which aligning the calendar does not explain"
        )
    return {
        "available": True,
        "reason": None,
        "cache": cache_window,
        "transcript": scan["window"],
        "transcript_scan": scan,
        "observation": scan["observation"],
        "days_transcript_only": only_transcript,
        "days_cache_only": only_cache,
        "days_both": both,
        "coverage": table["coverage"],
        "note": table["note"],
        "tokens_transcript_only": tokens_only_transcript,
        "pct_transcript_only": (
            round(100 * tokens_only_transcript / total, 1) if total else None
        ),
        "residual": {
            "shared_days": len(both),
            "transcript_tokens": transcript_shared,
            "cache_tokens": cache_shared,
            "gap": gap,
            "note": residual_note,
        },
    }


def _table_overlap(left_days, right_days, left_label, right_label):
    """Two day tables and the days they share -- plus why a zero is zero.

    A bare `in both: 0` reads as a measurement, but on a machine whose two
    windows are disjoint calendar ranges it is arithmetic: no shared day was
    ever possible. Name the case instead of printing the placeholder. The two
    callers (the two harnesses, and Claude's two counters) differ only in the
    labels, so they share one rule rather than two drifting copies of it.
    """
    left = sorted(set(left_days))
    right = sorted(set(right_days))
    both = sorted(set(left) & set(right))
    result = {
        "days_both": both,
        "days_left_only": sorted(set(left) - set(right)),
        "days_right_only": sorted(set(right) - set(left)),
        "coverage": "shared_days",
        "note": "",
    }
    if not left or not right:
        missing = left_label if not left else right_label
        result["coverage"] = "no_days"
        result["note"] = (
            f"{missing} produced no day at all, so there is no day table to overlap "
            f"(an empty table is not a count of zero shared days)"
        )
        return result
    if both:
        return result
    windows = f"({left_label} {left[0]}..{left[-1]}, {right_label} {right[0]}..{right[-1]})"
    if left[-1] < right[0] or right[-1] < left[0]:
        result["coverage"] = "disjoint_windows"
        result["note"] = f"no day can be in both: the two windows do not intersect {windows}"
        return result
    result["coverage"] = "no_shared_day"
    result["note"] = (
        f"the two windows intersect {windows} but no calendar day carries usage in both tables"
    )
    return result


def day_overlap(codex_days, claude_days, claude_label="claude"):
    """The two harnesses' day tables: shared days, one-sided days, and why.

    `claude_label` names which Claude table these days came from. When `compare`
    measures the transcript window as well, the note has to say *cache table* --
    otherwise "no day can be in both" reads as a statement about Claude as a
    whole while a measured Claude table with shared days is printed below it.
    """
    result = _table_overlap(codex_days, claude_days, "codex", claude_label)
    result["days_codex_only"] = result.pop("days_left_only")
    result["days_claude_only"] = result.pop("days_right_only")
    return result


def compare_harnesses(
    codex_root, claude_cache, max_sessions=None, claude_transcripts=None,
    min_tokens=None, tolerance=None,
):
    """Two harnesses on one machine: each total, its own verdicts, and the day overlap."""
    root = Path(codex_root)
    files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    if max_sessions:
        files = files[-max_sessions:]
    sessions = [audit_codex_session(read_codex_session(f)) for f in files]

    codex_days = {}
    for session in sessions:
        for day, tokens in session["day_totals"].items():
            codex_days[day] = codex_days.get(day, 0) + tokens

    verdicts = {}
    for session in sessions:
        verdicts[session["verdict"]] = verdicts.get(session["verdict"], 0) + 1

    witness_totals = {
        "delta_total": sum(s["witnesses"]["delta_total"] or 0 for s in sessions if s["witnesses"]),
        "running_total": sum(s["witnesses"]["running_total"] or 0 for s in sessions if s["witnesses"]),
        "records_total": sum(s["witnesses"]["records_total"] or 0 for s in sessions if s["witnesses"]),
        "sessions_without_records": sum(
            1 for s in sessions if s["witnesses"] and s["witnesses"]["records_total"] is None
        ),
        "sessions_all_three_agree": sum(
            1 for s in sessions if s["witnesses"] and s["witnesses"]["agreement"] == "all_three"
        ),
    }

    _claude_by_model, claude_days = _daily_totals(claude_cache)
    claude_dollars = summarize(claude_cache)["dollars"]
    claude_lifetime = sum(
        _tokens_for(entry)
        for entry in (claude_cache.get("modelUsage") or {}).values()
        if isinstance(entry, dict)
    )
    claude_daily = sum(claude_days.values())
    if claude_lifetime == claude_daily:
        declared_vs_daily = {
            "gap": 0,
            "note": "the per-model total and the day table agree",
        }
    else:
        gap = claude_lifetime - claude_daily
        toward = "no day" if gap > 0 else "more day rows than the per-model total holds"
        declared_vs_daily = {
            "gap": gap,
            "note": (
                f"{abs(gap):,} token(s) are the difference: the per-model total and the day "
                f"table do not contain the same usage ({toward})"
            ),
        }
    subagent_threads = [
        s for s in sessions
        if s["thread"].get("thread_source") == "subagent" or s["thread"].get("parent_thread_id")
    ]
    forks = [s for s in sessions if s["thread"].get("forked_from_id")]
    inheriting = [
        s for s in sessions
        if s["thread"].get("parent_thread_id") or s["thread"].get("forked_from_id")
    ]
    inherited_prefix = sum(s["first_running"] or 0 for s in inheriting)
    codex_scope = "root+sidechain" if subagent_threads else ("root" if sessions else "unknown")
    codex_window = _window(codex_days, "record_timestamp")
    claude_window = _window(claude_days, "declared_attribution")
    claude_window_pair = _claude_window_pair(claude_days, claude_window, claude_transcripts)
    # `overlap` measures the Claude side the cache declares. When a transcript
    # scan ran, a second, measured Claude day table exists -- and it can land on
    # the other side of the Codex window. Measuring only the cache table there
    # would print "no day can be in both" while holding the shared days.
    overlap_transcript = None
    if claude_window_pair["available"]:
        overlap_transcript = day_overlap(
            codex_days,
            claude_window_pair["transcript_scan"]["day_totals"],
            claude_label="claude transcript",
        )
        overlap_transcript["basis"] = "transcript record timestamps"
    scope_fields = _scope_fields(claude_cache)
    claude_scope_reason = (
        "no field in the cache names a scope, so whether the total contains delegated "
        "work cannot be read off the file"
        if not scope_fields
        else (
            "the cache names scope field(s) "
            + ", ".join(scope_fields)
            + " but their values are not arithmetic this receipt can check, so the scope stays unknown"
        )
    )

    stamps = {
        "codex.delta": _stamp(
            sum(s["delta_total"] or 0 for s in sessions),
            codex_scope,
            "delta",
            codex_window,
            "rollout last_token_usage.total_tokens",
            "session_meta.thread_source / parent_thread_id",
            subagent_sessions=len(subagent_threads),
            fork_sessions=len(forks),
        ),
        "codex.running": _stamp(
            witness_totals["running_total"],
            codex_scope,
            "cumulative",
            codex_window,
            "max(total_token_usage.total_tokens) per session",
            "session_meta.thread_source / parent_thread_id",
            subagent_sessions=len(subagent_threads),
            fork_sessions=len(forks),
            inherited_threads=len(inheriting),
            inherited_prefix_tokens=inherited_prefix,
            witness_note=(
                f"{len(inheriting)} thread(s) carry parent_thread_id or forked_from_id and start "
                f"their counter at a non-zero inherited value ({inherited_prefix:,} tokens across their first "
                "events); summing this column over a thread family repeats that prefix"
                if inheriting
                else "no thread here inherits a counter, so each starts at zero"
            ),
        ),
        "codex.records": _stamp(
            witness_totals["records_total"],
            codex_scope,
            "delta",
            codex_window,
            "token_usage_record responses",
            "session_meta.thread_source / parent_thread_id",
            subagent_sessions=len(subagent_threads),
        ),
        "claude.daily": _stamp(
            claude_daily,
            "unknown",
            "delta",
            claude_window,
            "stats-cache dailyModelTokens",
            "none",
            scope_fields=scope_fields,
            scope_reason=claude_scope_reason,
        ),
        "claude.declared": _stamp(
            claude_lifetime,
            "unknown",
            "cumulative",
            {"first": None, "last": None, "days": 0, "basis": "none"},
            "stats-cache modelUsage",
            "none",
            scope_fields=scope_fields,
            scope_reason=claude_scope_reason,
            window_reason="the per-model total declares no dates, so it is not placed on the day timeline",
        ),
    }
    transcripts = claude_window_pair["transcript_scan"]
    if transcripts is not None:
        stamps["claude.transcripts"] = _stamp(
            transcripts["tokens"],
            "root+sidechain",
            "delta",
            transcripts["window"],
            "transcript record timestamps",
            "message.id merge (max per bucket), earliest day",
            records=transcripts["records"],
            messages=transcripts["messages"],
        )
    floor, tol = _floor(min_tokens, tolerance)
    read_nothing = (claude_window_pair.get("observation") or {}).get("verdict") in {
        "store_empty",
        "no_usage_records",
    }
    if transcripts is not None:
        crosscheck_scan = {"days": transcripts["days"]}
    elif read_nothing:
        # The flag was passed and the directory was read; it held no usage day.
        # Passing None here would make the crosscheck report a missing flag.
        crosscheck_scan = {"days": {}}
    else:
        crosscheck_scan = None
    crosscheck = crosscheck_days(
        claude_days,
        crosscheck_scan,
        min_tokens=floor,
        tolerance=tol,
    )

    comparability = _comparability(stamps["codex.delta"], stamps["claude.daily"])
    comparability["basis_note"] = (
        "these windows come from two different sources (measured record timestamps vs a declared "
        "day table); `quotamirror nested --cache` measures a second window for the claude quantity "
        "and prints the pair, so no total here is an unexplained one"
        if transcripts is None
        else (
            "the claude quantity is measured from both sides in this output (CLAUDE, TWO COUNTERS): "
            "the cache day table declares its range, the transcript scan measures one"
        )
    )
    return {
        "codex": {
            "root": str(root),
            "sessions": len(sessions),
            "files_unreadable": 0,
            "tokens": sum(s["delta_total"] or 0 for s in sessions),
            "day_totals": codex_days,
            "verdicts": verdicts,
            "witnesses": witness_totals,
            "details": [
                {
                    "file": Path(s["path"]).name,
                    "event_count": s["events"],
                    "verdict": s["verdict"],
                    "tokens": s["delta_total"],
                    "running_total": s["running_total"],
                    "naive_factor": s["naive_factor"],
                    "two_writers": s["two_writers"]["verdict"],
                    "witness_agreement": s["witnesses"]["agreement"] if s["witnesses"] else None,
                    "witness_note": s["witnesses"]["note"] if s["witnesses"] else "",
                    "skipped_lines": s["skipped_lines"],
                }
                for s in sessions
            ],
            "dollars": {
                "status": "unknown",
                "usd": None,
                "reason": "rollout files carry no price field; nothing here was priced",
            },
        },
        "claude": {
            "tokens": claude_daily,
            "lifetime_tokens": claude_lifetime,
            "day_totals": claude_days,
            "declared_vs_daily": declared_vs_daily,
            "dollars": claude_dollars,
        },
        "stamps": stamps,
        "claude_window": {k: v for k, v in claude_window_pair.items() if k != "transcript_scan"},
        "claude_day_crosscheck": crosscheck,
        "comparability": comparability,
        "overlap": day_overlap(
            codex_days,
            claude_days,
            claude_label="claude cache table" if overlap_transcript else "claude",
        ),
        "overlap_basis": "claude cache day table",
        "overlap_transcript": overlap_transcript,
        "unit_note": (
            "the two totals are not the same quantity: Codex counts total_tokens as "
            "input+output with cached input nested inside input, while the Claude cache "
            "sums four separate buckets. Compare coverage, never bills."
        ),
    }


def _stamp_text(stamp):
    """The two stamps and the window, on one line, verbatim -- never summarised away."""
    window = stamp["window"]
    span = (
        f"window={window['first']}..{window['last']}"
        if window["first"]
        else "window=undeclared"
    )
    return (
        f"includes_descendants={stamp['includes_descendants']} "
        f"accounting={stamp['accounting']} {span} basis={window.get('basis', 'none')}"
    )


def _codex_summary_line(payload):
    codex = payload["codex"]
    verdicts = codex["verdicts"]
    ordered = ", ".join(f"{name} {count}" for name, count in sorted(verdicts.items()))
    return f"{codex['sessions']} session(s), {codex['tokens']:,} tokens from the delta column" + (
        f" ({ordered})" if ordered else ""
    )


def _render_overlap_block(title, overlap, lines):
    lines.append(title)
    if overlap["coverage"] == "no_days":
        lines.append(f"  in both     : not measured -- {overlap['note']}")
    else:
        both_line = f"  in both     : {len(overlap['days_both'])}"
        if overlap["note"]:
            both_line += f" -- {overlap['note']}"
        lines.append(both_line)
    for label, key, side in (
        ("codex only ", "days_codex_only", "codex"),
        ("claude only", "days_claude_only", "claude"),
    ):
        days = overlap[key]
        surface = f"  {label} : {len(days)}"
        if days:
            surface += f" ({_sample(days, 6)})"
        elif overlap["coverage"] == "shared_days":
            # A zero here is a real measurement once both tables exist, but it
            # still has to say what it measured: every day on THIS side is
            # present on the other. (Naming the other side would invert it.)
            surface += f" -- every {side} day is in both tables"
        lines.append(surface)


def _render_day_overlap(payload, lines):
    """One Claude day table per block, each naming the table it measured.

    Without a transcript scan there is exactly one Claude table and the plain
    header stays plain. With one, the cache's declared days and the measured
    record days are two different ranges that can sit on opposite sides of the
    Codex window, so a single "claude" column would be answering a question the
    reader cannot see.
    """
    measured = payload.get("overlap_transcript")
    if measured is None:
        _render_overlap_block("DAY OVERLAP", payload["overlap"], lines)
        return
    _render_overlap_block("DAY OVERLAP (claude side: cache day table)", payload["overlap"], lines)
    lines.append("")
    _render_overlap_block(
        "DAY OVERLAP (claude side: transcript record dates)", measured, lines
    )


def render_compare(payload):
    payload = redact_home(payload)
    codex = payload["codex"]
    claude = payload["claude"]
    lines = []
    lines.append("CROSS-HARNESS COMPARE  (two meters, two shapes, no bill)")
    lines.append(f"  codex rollouts: {codex['root']}")
    stamps = payload["stamps"]
    lines.append(f"  {_codex_summary_line(payload)}")
    lines.append(f"    [{_stamp_text(stamps['codex.delta'])}]")
    lines.append(f"  claude cache  : {claude['tokens']:,} tokens over {len(claude['day_totals'])} day(s)"
                 f" (per-model total {claude['lifetime_tokens']:,})")
    lines.append(f"    day table   : [{_stamp_text(stamps['claude.daily'])}]")
    lines.append(f"    per-model   : [{_stamp_text(stamps['claude.declared'])}]")
    if not stamps["claude.declared"]["window"]["first"]:
        lines.append(f"      {stamps['claude.declared']['window_reason']}")
    if claude["declared_vs_daily"]["gap"]:
        lines.append(f"    {claude['declared_vs_daily']['note']}")
    lines.append("")
    lines.append("WITNESSES (one quantity, three numbers)")
    w = codex["witnesses"]
    lines.append(f"  delta column     : {w['delta_total']:,}   [{_stamp_text(stamps['codex.delta'])}]")
    lines.append(f"  running totals   : {w['running_total']:,}   [{_stamp_text(stamps['codex.running'])}]")
    lines.append(f"  per-response recs: {w['records_total']:,}   [{_stamp_text(stamps['codex.records'])}]"
                 + (f"  ({w['sessions_without_records']} session(s) have none)"
                    if w["sessions_without_records"] else ""))
    if stamps["codex.running"].get("inherited_threads"):
        lines.append(f"    {stamps['codex.running']['witness_note']}")
    lines.append(f"  sessions where all three agree: {w['sessions_all_three_agree']} of {codex['sessions']}")
    lines.append("")
    lines.append("VERDICTS (per session)")
    if not codex["details"]:
        lines.append("  no rollout files were found; nothing to audit")
    for detail in codex["details"]:
        extra = ""
        if detail["naive_factor"] and detail["naive_factor"] > 1.0 and detail["verdict"] == "cumulative_confirmed":
            extra = f", summing the running column would read {detail['naive_factor']}x"
        lines.append(
            f"  {detail['file']}: {detail['verdict']}  [{detail['tokens'] if detail['tokens'] is not None else 'unknown'}"
            f" tokens, {detail['event_count']} events{extra}]"
        )
        if detail["skipped_lines"]:
            lines.append(f"    {detail['skipped_lines']} unparseable line(s) counted, not fatal")
        if detail["witness_note"] and detail["witness_agreement"] != "all_three":
            lines.append(f"    {detail['witness_note']}")
        if detail["two_writers"] == "two_writers_disagree":
            lines.append("    the per-response records and the running total disagree")
    lines.append("")
    lines.append("COMPARABILITY")
    lines.append(f"  {payload['comparability']['note']}")
    lines.append(f"  {payload['comparability']['basis_note']}")
    lines.append("")
    _render_crosscheck(payload["claude_day_crosscheck"], lines)
    lines.append("")
    _render_day_overlap(payload, lines)
    lines.append("")
    lines.append("CLAUDE, TWO COUNTERS (one quantity, two windows)")
    pair = payload["claude_window"]
    if not pair["available"]:
        lines.append(f"  transcript   : not measured -- {pair['reason']}")
        lines.append(_observation_line(pair["observation"]))
        lines.append(f"  cache table  : {_window_text(pair['cache'])}")
    else:
        lines.append(f"  transcript   : {_window_text(pair['transcript'])}"
                     f"  [{_stamp_text(stamps['claude.transcripts'])}]")
        lines.append(_observation_line(pair["observation"]))
        lines.append(f"  cache table  : {_window_text(pair['cache'])}  [{_stamp_text(stamps['claude.daily'])}]")
        if pair["coverage"] == "no_days":
            lines.append(f"  days in both : not measured -- {pair['note']}")
        else:
            both_line = f"  days in both : {len(pair['days_both'])}"
            if pair["note"]:
                both_line += f" -- {pair['note']}"
            lines.append(both_line)
        lines.append(f"    transcript only: {len(pair['days_transcript_only'])}"
                     f" | cache only: {len(pair['days_cache_only'])}")
        if pair["days_transcript_only"]:
            only = pair["days_transcript_only"]
            shown = _sample(only, 6)
            pct = pair["pct_transcript_only"]
            lines.append(
                f"    in the transcript, not in the cache table: {shown} -- "
                f"{pair['tokens_transcript_only']:,} token(s)"
                + (f" = {pct}% of the transcript total" if pct is not None else "")
            )
        if pair["days_cache_only"]:
            only = pair["days_cache_only"]
            lines.append("    in the cache table, not in the transcript: "
                         + _sample(only, 6))
        lines.append(f"  {pair['residual']['note']}")
    lines.append("")
    lines.append("PRICE")
    lines.append(f"  codex  : {codex['dollars']['status']} -- {codex['dollars']['reason']}")
    lines.append(f"  claude : {claude['dollars']['status']} -- {claude['dollars'].get('reason', '')}")
    lines.append("")
    lines.append("UNIT NOTE")
    lines.append(f"  {payload['unit_note']}")
    return "\n".join(lines) + "\n"


def _count_share(turns):
    """One granularity's worth of counting. `turns` are {delta, calls} dicts."""
    counts = {
        "turns_total": 0,
        "turns_with_calls": 0,
        "coordination_turns": 0,
        "tokens_total": 0,
        "tokens_with_calls": 0,
        "coordination_tokens": 0,
    }
    for turn in turns:
        counts["turns_total"] += 1
        counts["tokens_total"] += turn["delta"]
        calls = turn["calls"]
        if not calls:
            continue
        counts["turns_with_calls"] += 1
        counts["tokens_with_calls"] += turn["delta"]
        if all(name in COORDINATION_TOOLS for name in calls):
            counts["coordination_turns"] += 1
            counts["coordination_tokens"] += turn["delta"]
    counts["share_pct"] = (
        round(100 * counts["coordination_tokens"] / counts["tokens_total"], 1)
        if counts["tokens_total"]
        else None
    )
    return counts


def _merge_user_turns(turns):
    """Fold request spans into the spans a human would call one turn.

    A request span starts a user turn when a user message preceded it. Calls are
    concatenated, so a request that only polled inside a user turn that also ran
    real work stops being coordination-only: that is the whole difference between
    the two denominators.
    """
    merged = []
    current = None
    for turn in turns:
        if current is None or turn.get("starts_user_turn"):
            current = {"delta": 0, "calls": []}
            merged.append(current)
        current["delta"] += turn["delta"]
        current["calls"].extend(turn["calls"])
    return merged


def poll_share(codex_root, max_sessions=None):
    """Share of a rollout corpus whose turns asked only "is it done yet?".

    Two granularities are counted from the same bytes, because they answer
    different questions and disagree: a REQUEST span (one `token_count` event)
    is what the model was billed for, while a USER TURN (spans between user
    messages) is what a person remembers doing. A poll that happens inside a
    user turn that also did work is coordination at one granularity and work at
    the other, so both are printed with their own denominator and neither is
    presented as the answer.
    """
    root = Path(codex_root)
    files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    if max_sessions:
        files = files[-max_sessions:]
    request_turns = []
    user_turns = []
    coarse_with_boundary = 0
    coarse_without_boundary = 0
    unattributed_calls = 0
    skipped_lines = 0
    tool_calls = {}
    for path in files:
        session = read_codex_session(path)
        unattributed_calls += session["unattributed_calls"]
        skipped_lines += session["skipped_lines"]
        request_turns.extend(session["turns"])
        if any(turn.get("starts_user_turn") for turn in session["turns"]):
            coarse_with_boundary += 1
            user_turns.extend(_merge_user_turns(session["turns"]))
        else:
            coarse_without_boundary += 1
        for turn in session["turns"]:
            for name in turn["calls"]:
                tool_calls[name] = tool_calls.get(name, 0) + 1

    counts = _count_share(request_turns)
    coarse = _count_share(user_turns)
    coarse["available"] = coarse_with_boundary > 0
    coarse["sessions_without_user_boundary"] = coarse_without_boundary
    if not request_turns:
        verdict = "no_turns"
    elif not coarse["available"]:
        # No user message in the corpus marks a boundary, so the coarser unit
        # cannot be formed at all. Refusing to form it beats printing one
        # giant turn and calling the result a denominator.
        verdict = "coordination_found" if counts["coordination_turns"] else "no_coordination"
    elif counts["coordination_turns"] == 0 and coarse["coordination_turns"] == 0:
        verdict = "no_coordination"
    elif counts["coordination_turns"] != coarse["coordination_turns"]:
        verdict = "granularity_dependent"
    else:
        verdict = "coordination_found"
    payload = dict(counts)
    payload.update(
        {
            "root": str(root),
            "sessions": len(files),
            "unattributed_calls": unattributed_calls,
            "skipped_lines": skipped_lines,
            "tool_calls": tool_calls,
            "coordination_tools": list(COORDINATION_TOOLS),
            "by_user_turn": coarse,
            "verdict": verdict,
            "rule": (
                "a turn is coordination only when it called at least one tool and every "
                "call was one of " + ", ".join(COORDINATION_TOOLS) + "; a turn that also "
                "ran real work is not split, because its tokens cannot be divided "
                "honestly; text-only turns stay in the denominator"
            ),
            "granularity_note": (
                "by request: one token_count span, which is what the meter bills. "
                "by user turn: spans between user messages, which is what a person "
                "remembers doing. The same poll counts as coordination in the first "
                "and as work in the second when the turn also did real work."
            ),
        }
    )
    return payload


def _render_share_block(label, counts, noun):
    if counts.get("available") is False:
        return [
            f"  {label:<12}: no user message marks a turn boundary in "
            f"{counts['sessions_without_user_boundary']} session(s); the coarse "
            f"denominator cannot be formed, so no share is printed"
        ]
    if counts["turns_total"] == 0:
        return [f"  {label:<12}: no {noun} were read; a share needs a denominator, so none is printed"]
    return [
        f"  {label:<12}: {counts['share_pct']}% of {counts['tokens_total']:,} tokens over "
        f"{counts['turns_total']} {noun} "
        f"({counts['coordination_turns']} of {counts['turns_total']} were coordination-only)"
    ]


def render_poll_share(payload):
    lines = []
    lines.append("COORDINATION SPEND  (tokens that bought waiting)")
    lines.append(f"  sessions read : {payload['sessions']}")
    lines.extend(_render_share_block("by request", payload, "turns (request spans)"))
    lines.extend(
        _render_share_block("by user turn", payload["by_user_turn"], "turns (user turns)")
    )
    if payload["turns_total"]:
        lines.append(
            f"  request spans carrying tool calls: {payload['turns_with_calls']} of "
            f"{payload['turns_total']}"
        )
    if payload["tool_calls"]:
        top = sorted(payload["tool_calls"].items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        lines.append(
            "  tool calls    : " + ", ".join(f"{name} {count:,}" for name, count in top)
        )
    if payload["unattributed_calls"]:
        lines.append(
            f"  {payload['unattributed_calls']} call(s) arrived after the last token event: "
            f"no turn closed, so they are counted here and in no share"
        )
    if payload["skipped_lines"]:
        lines.append(f"  {payload['skipped_lines']} unparseable line(s) counted, not fatal")
    lines.append("RULE")
    lines.append(f"  {payload['rule']}")
    lines.append(f"  {payload['granularity_note']}")
    lines.append("  verdict: " + payload["verdict"])
    return "\n".join(lines) + "\n"


CLAUDE_TOKEN_BUCKETS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
NON_CACHE_BUCKETS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")


def _usage_record(record):
    """One transcript record's own usage, or None when it carries none."""
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    tokens = {}
    for bucket in CLAUDE_TOKEN_BUCKETS:
        value = usage.get(bucket)
        tokens[bucket] = int(value) if isinstance(value, (int, float)) else 0
    if not any(tokens.values()):
        return None
    return message.get("id"), tokens


def _record_day(record):
    """The UTC day a transcript record was written, or None when it has no usable timestamp."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str) or len(stamp) < 10:
        return None
    day = stamp[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return day


def _content_hash(path, chunk=1 << 20):
    """sha256[:16] of one transcript's bytes, read in bounded chunks.

    The inventory digest is over (name, size, mtime) because that is what a
    directory walk already knows. Size and mtime cannot see an edit that keeps
    both - a rewrite that changes digits, or a tool that restores the mtime -
    and they move when a file is merely touched. A digest over the bytes has
    neither problem, at the cost of reading each file once more.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()[:16]


def scan_usage(root, max_files=None, manifest=False):
    """Read the Claude transcript corpus once: one merge rule, one set of counts.

    `nested` and `compare --claude-transcripts` used to carry a copy of this loop
    each; a copy is a place for the two commands to start disagreeing about the
    same corpus. The rule kept here is the one both already printed: one
    `message.id` counts once, the larger value per bucket wins (a streaming
    partial write repeats the usage), and a message whose records land on two
    days is attributed to the earlier one so the day table sums to the total.

    Per-record facts that the callers disagree about (whether a record came from
    a subagent) are carried on the entry instead of being decided here.
    """
    root_path = Path(root)
    files = _transcript_files(root_path, max_files=max_files) if root_path.exists() else []
    files_skipped = 0
    files_read = 0
    bytes_read = 0
    inventory_parts = []
    content_parts = []
    file_facts = []
    day_files = {}
    skipped_lines = 0
    records = 0
    naive_tokens = 0
    timed = 0
    untimed = 0
    order = []
    entries = {}

    for path in files:
        try:
            file_stat = path.stat()
            content_hash = _content_hash(path)
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            files_skipped += 1
            continue
        files_read += 1
        bytes_read += file_stat.st_size
        try:
            label = str(path.relative_to(root_path))
        except ValueError:
            label = str(path)
        inventory_parts.append(f"{label}|{file_stat.st_size}|{file_stat.st_mtime_ns}")
        content_parts.append(f"{label}|{content_hash}")
        if manifest:
            file_facts.append((label, file_stat.st_size, file_stat.st_mtime_ns, content_hash))
        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped_lines += 1
                    continue
                if not isinstance(record, dict):
                    skipped_lines += 1
                    continue
                parsed = _usage_record(record)
                if parsed is None:
                    continue
                message_id, tokens = parsed
                message = record.get("message")
                model = message.get("model") if isinstance(message, dict) else None
                model = model if isinstance(model, str) and model else "unknown"
                records += 1
                naive_tokens += sum(tokens.values())
                day = _record_day(record)
                if day:
                    timed += 1
                    day_files.setdefault(day, set()).add(str(path))
                else:
                    untimed += 1
                key = f"id:{message_id}" if message_id else f"uuid:{record.get('uuid') or len(order)}"
                entry = entries.get(key)
                if entry is None:
                    entry = {
                        "tokens": tokens,
                        "days": set(),
                        "scopes": set(),
                        "models": set(),
                        "records": 0,
                        "naive_tokens": 0,
                    }
                    entries[key] = entry
                    order.append(key)
                else:
                    entry["tokens"] = {
                        bucket: max(entry["tokens"][bucket], tokens[bucket])
                        for bucket in CLAUDE_TOKEN_BUCKETS
                    }
                entry["records"] += 1
                entry["naive_tokens"] += sum(tokens.values())
                entry["models"].add(model)
                if day:
                    entry["days"].add(day)
                entry["scopes"].add("sidechain" if record.get("isSidechain") else "root")

    tokens = {bucket: 0 for bucket in CLAUDE_TOKEN_BUCKETS}
    days = {}
    unattributed_tokens = 0
    for key in order:
        entry = entries[key]
        for bucket in CLAUDE_TOKEN_BUCKETS:
            tokens[bucket] += entry["tokens"][bucket]
        if not entry["days"]:
            unattributed_tokens += sum(entry["tokens"].values())
            continue
        day = min(entry["days"])
        entry_total = sum(entry["tokens"].values())
        record_day = days.setdefault(
            day,
            {
                "total_tokens": 0,
                "records": 0,
                "naive_tokens": 0,
                "by_bucket": {bucket: 0 for bucket in CLAUDE_TOKEN_BUCKETS},
                "by_model": {},
            },
        )
        record_day["total_tokens"] += entry_total
        record_day["records"] += entry["records"]
        record_day["naive_tokens"] += entry["naive_tokens"]
        for bucket in CLAUDE_TOKEN_BUCKETS:
            record_day["by_bucket"][bucket] += entry["tokens"][bucket]
        # One message id carries one model in practice; when a duplicate disagrees
        # the tokens are not split between them, because splitting would invent a
        # number nobody wrote.
        model_key = next(iter(entry["models"])) if len(entry["models"]) == 1 else "|".join(
            sorted(entry["models"])
        )
        per_model = record_day["by_model"].setdefault(
            model_key,
            {"total_tokens": 0, "by_bucket": {bucket: 0 for bucket in CLAUDE_TOKEN_BUCKETS}},
        )
        per_model["total_tokens"] += entry_total
        for bucket in CLAUDE_TOKEN_BUCKETS:
            per_model["by_bucket"][bucket] += entry["tokens"][bucket]

    for record_day in days.values():
        record_day["duplicate_tokens"] = record_day["naive_tokens"] - record_day["total_tokens"]

    result = {
        "root": str(root_path),
        "files": files,
        "files_skipped": files_skipped,
        "files_read": files_read,
        "bytes_read": bytes_read,
        "reading_digest": hashlib.sha256("\n".join(sorted(inventory_parts)).encode("utf-8")).hexdigest()[:12],
        "digest_basis": "path+size+mtime_ns",
        "content_digest": hashlib.sha256("\n".join(sorted(content_parts)).encode("utf-8")).hexdigest()[:12],
        "content_basis": "file bytes",
        "day_files": {day: len(paths) for day, paths in day_files.items()},
        "skipped_lines": skipped_lines,
        "records": records,
        "messages": len(order),
        "naive_tokens": naive_tokens,
        "tokens": tokens,
        "days": days,
        "unattributed_tokens": unattributed_tokens,
        "entries": entries,
        "order": order,
        "records_timed": timed,
        "records_without_timestamp": untimed,
        "dedupe": "message_id_max",
        "day_attribution": "earliest_recorded_day",
    }
    if manifest:
        facts = sorted(file_facts)
        result["file_facts"] = facts[:MANIFEST_LIMIT]
        result["manifest_total"] = len(facts)
        result["manifest_truncated"] = len(facts) > MANIFEST_LIMIT
    return result


def _within(value, target, tolerance):
    """Is `value` within `tolerance` (a fraction) of `target`? Zero is its own question."""
    if target == 0:
        return value == 0
    return abs(value - target) / abs(target) <= tolerance


def crosscheck_days(cache_days, scan, min_tokens=1_000_000, tolerance=0.02):
    """Compare each cache day with merged-message and raw-record totals.

    Repeated streamed writes can make a day match the sum of transcript records
    rather than the sum of distinct messages. This check states which total the
    cache agrees with and keeps small days from deciding the verdict.

    Only days at or above `min_tokens` may decide the verdict: a hundred tiny
    days that happen to match must not outvote the one day that carries the
    bill. Those days are counted, not hidden. A day the cache and the transcript
    both know but which is too small to decide is reported as below the floor;
    a day only one side knows is counted as one-sided, never as agreement.
    """
    min_tokens = int(min_tokens)
    transcript_days = (scan or {}).get("days") or {}
    empty = {
        "available": scan is not None,
        "verdict": "unavailable",
        "coverage": "not_read",
        "days": [],
        "days_compared": 0,
        "days_below_floor": 0,
        "days_cache_only": 0,
        "days_transcript_only": 0,
        "cache_tokens": 0,
        "merged_tokens": 0,
        "naive_tokens": 0,
        "worst_day": None,
        "tolerance": tolerance,
        "min_tokens": min_tokens,
    }
    if scan is None:
        empty["reason"] = (
            "no transcript scan was given (--claude-transcripts DIR), so the cache's day table "
            "has nothing on this machine to be checked against"
        )
        return empty

    shared = _table_overlap(cache_days, transcript_days, "cache table", "transcript")
    rows = []
    below_floor = 0
    for date in shared["days_both"]:
        entry = transcript_days[date] or {}
        merged = int(entry.get("total_tokens") or 0)
        naive = int(entry.get("naive_tokens") or 0)
        cache_value = int(cache_days.get(date) or 0)
        if max(cache_value, merged, naive) < min_tokens:
            below_floor += 1
            continue
        near_merged = _within(cache_value, merged, tolerance)
        near_naive = _within(cache_value, naive, tolerance)
        if near_merged and near_naive:
            closer = "both"
        elif near_naive:
            closer = "record_sum"
        elif near_merged:
            closer = "merged"
        else:
            closer = "neither"
        rows.append(
            {
                "date": date,
                "cache": cache_value,
                "merged": merged,
                "naive": naive,
                "duplicate_tokens": naive - merged,
                "closer_to": closer,
                "cache_over_merged": (round(cache_value / merged, 4) if merged else None),
            }
        )

    result = dict(empty)
    result["available"] = True
    result["reason"] = None
    result["days"] = rows
    result["days_compared"] = len(rows)
    result["days_below_floor"] = below_floor
    result["days_cache_only"] = len(shared["days_left_only"])
    result["days_transcript_only"] = len(shared["days_right_only"])
    result["cache_tokens"] = sum(row["cache"] for row in rows)
    result["merged_tokens"] = sum(row["merged"] for row in rows)
    result["naive_tokens"] = sum(row["naive"] for row in rows)
    result["worst_day"] = (
        max(rows, key=lambda row: row["duplicate_tokens"]) if rows else None
    )

    record_sum_days = sum(1 for row in rows if row["closer_to"] == "record_sum")
    merged_days = sum(1 for row in rows if row["closer_to"] == "merged")
    neither_days = sum(1 for row in rows if row["closer_to"] == "neither")
    result["days_matching_record_sum"] = record_sum_days
    result["days_matching_merged"] = merged_days
    result["days_matching_neither"] = neither_days
    if not rows:
        result["verdict"] = "no_overlap"
        if shared["coverage"] == "shared_days":
            result["coverage"] = "all_below_floor"
            result["reason"] = (
                f"{below_floor} shared day(s) exist, but every one is under the "
                f"{min_tokens:,}-token floor, so none of them may decide the verdict "
                "(the floor is this check's rule, not a fact about the cache)"
            )
        else:
            result["coverage"] = shared["coverage"]
            result["reason"] = shared["note"]
    elif record_sum_days > merged_days:
        result["coverage"] = "shared_days"
        result["verdict"] = "record_sum_confirmed"
        result["reason"] = (
            f"{record_sum_days} of {len(rows)} comparable day(s) match the record sum and "
            f"{merged_days if merged_days else 'none'} match the merged total: this cache counts "
            f"repeated writes of the same "
            "message, so its day table is a record sum and not a bill"
        )
    elif merged_days > record_sum_days:
        result["coverage"] = "shared_days"
        result["verdict"] = "merged_confirmed"
        result["reason"] = (
            f"{merged_days} of {len(rows)} comparable day(s) match the merged total and "
            f"{record_sum_days if record_sum_days else 'none'} match the record sum: on this corpus "
            f"the cache's day table is "
            "one count per message"
        )
    else:
        result["coverage"] = "shared_days"
        result["verdict"] = "mixed"
        result["reason"] = (
            f"the comparable days split evenly ({record_sum_days} record sum, {merged_days} "
            f"merged, {neither_days} matching neither), so neither shape describes the table"
        )
    return result


def _render_crosscheck(result, lines):
    lines.append("DAY CROSS-CHECK (is the cache's day table a bill or a record sum?)")
    if not result["available"]:
        lines.append(f"  not measured -- {result['reason']}")
        return
    if result.get("coverage") == "no_days":
        # One side produced no day at all: printing the other side's count beside
        # it would read as a one-sided comparison, which is not what happened.
        lines.append(f"  not measured -- {result['reason']}")
        return
    lines.append(
        f"  compared {result['days_compared']} day(s) of at least {result['min_tokens']:,} tokens "
        f"(within {int(result['tolerance'] * 100)}% counts as a match); "
        f"{result['days_below_floor']} shared day(s) below that floor excluded, "
        f"{result['days_cache_only']} cache-only, {result['days_transcript_only']} transcript-only"
    )
    if not result["days"]:
        lines.append(f"  verdict: {result['verdict']} -- {result['reason']}")
        return
    lines.append(
        f"  cache {result['cache_tokens']:,} | merged {result['merged_tokens']:,} | "
        f"record sum {result['naive_tokens']:,}"
    )
    worst = result["worst_day"]
    if worst:
        lines.append(
            f"  worst day {worst['date']}: cache {worst['cache']:,}, record sum {worst['naive']:,}, "
            f"merged {worst['merged']:,} ({worst['duplicate_tokens']:,} repeated)"
        )
    if result["merged_tokens"]:
        ratio = result["cache_tokens"] / result["merged_tokens"]
        lines.append(
            f"  cache / merged = {ratio:.3f}x over the same {result['days_compared']} day(s) "
            f"({result['cache_tokens']:,} vs {result['merged_tokens']:,})"
        )
    lines.append(
        f"  verdict: {result['verdict']} -- {result['reason']}"
    )


def transcript_window(root, max_files=None):
    """The Claude window measured from records, for the command that could only point at it.

    `nested` measures this window and `compare` printed the cache's *declared* day
    table; until now the only way to see both in one place was to run one command
    and then the other. The merge rule is `nested`'s (one `message.id` counted
    once, larger value per bucket), so the two commands cannot disagree about the
    same corpus -- there is a test for exactly that.

    A message whose records fall on two days is attributed to the earlier one, so
    the day table always sums to the stamped total instead of being a second,
    slightly different quantity.
    """
    scan = scan_usage(root, max_files=max_files)
    day_totals = {day: table["total_tokens"] for day, table in scan["days"].items()}
    untimed = scan["records_without_timestamp"]

    return {
        "root": scan["root"],
        "files": len(scan["files"]),
        "files_skipped": scan["files_skipped"] + scan["skipped_lines"],
        "records": scan["records"],
        "messages": scan["messages"],
        "naive_tokens": scan["naive_tokens"],
        "tokens": sum(scan["tokens"].values()),
        "unattributed_tokens": scan["unattributed_tokens"],
        "dedupe": scan["dedupe"],
        "day_attribution": scan["day_attribution"],
        "window": _window(set(day_totals), "record_timestamp"),
        "day_totals": day_totals,
        "days": scan["days"],
        "observation": _observation(scan["files"], scan["records"]),
        "coverage": {
            "records_timed": scan["records_timed"],
            "records_without_timestamp": untimed,
            "note": _coverage_note(scan["records"], untimed),
        },
    }


def _day_of(value):
    """A YYYY-MM-DD prefix as a date, or None - never a guess from a partial stamp."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _receipt_day_table(cache):
    """The receipt's own per-day message counts, keyed by date.

    A row is a positive statement ("this day was computed"); its message count
    may still be zero. The row set is what the span is read from, so a day the
    table computed and found empty is not confused with a day outside the table.
    """
    table = {}
    for row in cache.get("dailyActivity") or []:
        if not isinstance(row, dict):
            continue
        day = _day_of(row.get("date"))
        if day is None:
            continue
        try:
            table[day] = int(row.get("messageCount") or 0)
        except (TypeError, ValueError):
            continue
    return table


def _receipt_token_table(cache):
    """The receipt's own per-day token totals, keyed by date.

    `dailyActivity` counts messages and says nothing about tokens; this is the
    receipt's *second* day table, and on this machine it is the only place the
    receipt states a token number per day. A row with a positive total is the
    receipt speaking for that day. A row that sums to zero states nothing, so it
    is left out: a zero must never upgrade a day into a measured one.
    """
    table = {}
    for row in cache.get("dailyModelTokens") or []:
        if not isinstance(row, dict):
            continue
        day = _day_of(row.get("date"))
        if day is None:
            continue
        by_model = row.get("tokensByModel")
        if not isinstance(by_model, dict):
            continue
        total = 0
        usable = True
        for value in by_model.values():
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                usable = False
                break
        if usable and total > 0:
            table[day] = total
    return table


def _receipt_model_table(cache):
    """The receipt's per-day token totals split by model, keyed by date.

    `_receipt_token_table` collapses the same rows to one number per day. This
    keeps the names, because the meter check's other two axes sum every model
    together and cannot say whether a gap lives in one model or in all of them.
    A row that names no model, or a value that is not a number, is dropped
    rather than coerced - an invented zero would read as a measured one.
    """
    table = {}
    for row in cache.get("dailyModelTokens") or []:
        if not isinstance(row, dict):
            continue
        day = _day_of(row.get("date"))
        if day is None:
            continue
        by_model = row.get("tokensByModel")
        if not isinstance(by_model, dict):
            continue
        split = {}
        for name, value in by_model.items():
            if not isinstance(name, str) or not name:
                continue
            try:
                tokens = int(value or 0)
            except (TypeError, ValueError):
                continue
            if tokens:
                split[name] = tokens
        if split:
            table[day] = split
    return table


def _model_axis(billed_by_model, transcript_by_model, deciding, tol):
    """Split the meter check's deciding days by model name.

    The returned keys are prefixed `by_model_` on purpose: this dict is merged
    into the meter check block, whose own `verdict` must not be overwritten by
    a per-model one.

    A model is compared only when both sides name it over the deciding days. A
    model only one side names is listed, never counted as agreeing and never
    dropped: a name mismatch and a model the other store does not meter leave
    the same gap, and the two are indistinguishable from the numbers alone.
    `largest_gap_share` is that model's share of the total positive excess, so
    a gap spread across every model is distinguished from one that sits in a
    single model.
    """
    fragment = {
        "by_model_verdict": "not_measured",
        "by_model_rows": [],
        "models_compared": 0,
        "models_agreeing": 0,
        "models_receipt_heavy": 0,
        "models_transcript_heavy": 0,
        "largest_gap_model": None,
        "largest_gap_share": None,
        "models_receipt_only": [],
        "models_transcript_only": [],
        "model_ratio_min": None,
        "model_ratio_max": None,
    }
    if not billed_by_model and not transcript_by_model:
        return fragment
    receipt = {}
    transcript = {}
    for day in deciding:
        for name, tokens in (billed_by_model.get(day) or {}).items():
            receipt[name] = receipt.get(name, 0) + tokens
        for name, tokens in (transcript_by_model.get(day) or {}).items():
            transcript[name] = transcript.get(name, 0) + tokens
    if not receipt and not transcript:
        return fragment
    shared = sorted(
        set(receipt) & set(transcript), key=lambda name: -receipt.get(name, 0)
    )
    for name in sorted(set(receipt) - set(transcript)):
        fragment["models_receipt_only"].append(name)
    for name in sorted(set(transcript) - set(receipt)):
        fragment["models_transcript_only"].append(name)
    rows = []
    for name in shared:
        billed = receipt[name]
        written = transcript[name]
        ratio = round(billed / written, 4) if written else None
        if ratio is None:
            verdict = "no_transcript_record"
        elif abs(ratio - 1) <= tol:
            verdict = "agree"
        elif ratio > 1:
            verdict = "receipt_heavy"
        else:
            verdict = "transcript_heavy"
        rows.append(
            {
                "model": name,
                "receipt_tokens": billed,
                "transcript_tokens": written,
                "ratio": ratio,
                "verdict": verdict,
            }
        )
    fragment["by_model_rows"] = rows
    fragment["models_compared"] = len(rows)
    fragment["models_agreeing"] = sum(1 for row in rows if row["verdict"] == "agree")
    fragment["models_receipt_heavy"] = sum(
        1 for row in rows if row["verdict"] == "receipt_heavy"
    )
    fragment["models_transcript_heavy"] = sum(
        1 for row in rows if row["verdict"] == "transcript_heavy"
    )
    ratios = [row["ratio"] for row in rows if row["ratio"] is not None]
    if ratios:
        fragment["model_ratio_min"] = min(ratios)
        fragment["model_ratio_max"] = max(ratios)
    excess = [
        (row["receipt_tokens"] - row["transcript_tokens"], row["model"])
        for row in rows
        if row["receipt_tokens"] > row["transcript_tokens"]
    ]
    if excess:
        total = sum(amount for amount, _ in excess)
        amount, name = max(excess)
        fragment["largest_gap_model"] = name
        fragment["largest_gap_share"] = round(amount / total, 4) if total else None
    if not rows:
        fragment["by_model_verdict"] = "no_shared_model"
    elif fragment["models_agreeing"] == len(rows):
        fragment["by_model_verdict"] = "every_model_agrees"
    elif fragment["models_receipt_heavy"] == len(rows):
        fragment["by_model_verdict"] = "every_model_receipt_heavy"
    else:
        fragment["by_model_verdict"] = "mixed"
    return fragment


def _witness_rows(billed_days, record_sum, floor, tol, unmeasured=()):
    """Classify each billed day that reaches the floor. Returns (rows, receipt_total).

    Split out so the replay axis can read the same day verdicts instead of
    re-deciding them with a second copy of the tolerance rule.
    """
    days = [
        day
        for day, tokens in (billed_days or {}).items()
        if tokens and int(tokens) >= floor
    ]
    rows = []
    receipt_total = 0
    unknown = set(unmeasured or ())
    for day in sorted(days):
        billed = int(billed_days.get(day) or 0)
        written = int((record_sum or {}).get(day, 0) or 0)
        receipt_total += billed
        if day in unknown:
            # The scan holds the day but states no record sum for it, so an
            # unbalanced day here would be a missing measurement rather than
            # missing evidence.
            state = "unmeasured"
        elif written == 0:
            state = "absent"
        elif abs(billed / written - 1) <= tol:
            state = "exact"
        elif billed > written:
            state = "remnant"
        else:
            state = "over"
        rows.append({"day": day, "receipt": billed, "record_sum": written, "state": state})
    return rows, receipt_total


def _replay_axis(rows, deduped_by_day):
    """Of the tokens written on a witnessed day, how many are repeat writes?

    The witness axis certifies which billed days have a complete record set.
    Only on those days may the record sum be compared with the deduped total:
    the difference is then the same message written more than once (a streamed
    response lands in the corpus repeatedly), not records that went missing.

    The share is deliberately refused when no day is witnessed - a dedupe over
    incomplete records measures the loss, not the repeats - and short or absent
    days are counted as excluded rather than pooled into the denominator.

    Refusing the share is not the same as having nothing to say about those
    days. A short day still holds records, and those records still dedupe, so
    the repeat rate among the records that survived is measurable - as a rate
    over the records present, never as a share of the tokens the day bills. It
    is kept in its own `short_days` block for that reason: pooling it into
    `replay_share` would make one denominator stand for two different questions.
    """
    block = {
        "available": False,
        "verdict": "not_measured",
        "days": 0,
        "days_excluded": 0,
        "written_tokens": 0,
        "deduped_tokens": 0,
        "replay_tokens": 0,
        "replay_share": None,
        "short_days": {
            "days": 0,
            "available": False,
            "written_tokens": 0,
            "deduped_tokens": 0,
            "replay_tokens": 0,
            "billed_tokens": 0,
            "share": None,
            "scope": "records_present",
            "absent_days": 0,
            "verdict": "no_short_day",
            "reason": None,
        },
        "reason": None,
    }
    short = block["short_days"]
    short["absent_days"] = sum(1 for row in rows if row["state"] == "absent")
    remnant = [row for row in rows if row["state"] == "remnant"]
    if remnant:
        written = sum(int(row["record_sum"]) for row in remnant)
        deduped = sum(int((deduped_by_day or {}).get(row["day"], 0) or 0) for row in remnant)
        short.update(
            days=len(remnant),
            available=True,
            written_tokens=written,
            deduped_tokens=deduped,
            billed_tokens=sum(int(row["receipt"]) for row in remnant),
        )
        if deduped > written:
            # A dedupe cannot add tokens; if it does, the two sides are not
            # counting the same records and no rate may be printed from them.
            short["verdict"] = "not_comparable"
            short["reason"] = (
                "the deduped total on these days exceeds the records they hold, so the two "
                "sides do not count the same records"
            )
        else:
            short["replay_tokens"] = written - deduped
            short["share"] = round(short["replay_tokens"] / written, 4) if written else None
            short["verdict"] = "repeat_measured" if short["replay_tokens"] else "no_repeat_observed"
    exact = [row for row in rows if row["state"] == "exact"]
    block["days_excluded"] = len(rows) - len(exact)
    if not exact:
        block["verdict"] = "no_exact_day"
        block["reason"] = (
            "no billed day carries a complete record set, and a dedupe over incomplete "
            "records is a floor on the repeat count rather than a share of it"
        )
        return block
    block["days"] = len(exact)
    for row in exact:
        block["written_tokens"] += row["record_sum"]
        block["deduped_tokens"] += int((deduped_by_day or {}).get(row["day"], 0) or 0)
    block["replay_tokens"] = block["written_tokens"] - block["deduped_tokens"]
    block["available"] = True
    if block["written_tokens"]:
        block["replay_share"] = round(block["replay_tokens"] / block["written_tokens"], 4)
    block["verdict"] = "repeat_measured" if block["replay_tokens"] else "no_repeat_observed"
    return block


def _reading_axis(inventory, rows):
    """Which corpus did the verdict just read?

    A witness verdict is a statement about a *reading*: the files this run
    opened, the records it parsed, the bytes on disk at that moment. Without
    that inventory, two runs that print different day splits cannot be told
    apart - "the corpus changed" and "the tool read a different corpus" look
    identical. An earlier build caught exactly that: one billed day's record sum
    fell by most of its value between two runs of the same code, and neither
    payload said which files or how many records the reading covered.

    So this block states the reading itself. `digest` is a sha256[:12] over
    (name relative to the root, size, mtime_ns) for every file read, and
    `content_digest` is a sha256[:12] over (name, sha256 of the file's bytes).
    The two answer different questions and both travel, because neither alone
    can support the sentence "the same corpus was read": an equal `digest` does
    NOT mean the same bytes (an edit that keeps size and mtime, or a rewrite
    with the mtime restored, leaves it equal), and an unequal `digest` does not
    mean different bytes (a touch moves it on its own). `content_digest` is the
    axis that can carry that claim; `digest` is kept because it is cheap and
    because a moved one is still a reason to look. Both identify a reading, not
    a corpus across machines - file names are local facts.

    `per_day` ties each witness day to the files and records standing behind
    it, so a day whose split moved can be read as "3 files held it last time,
    1 file now" instead of a bare number. Two record counts are kept apart on
    purpose: `records_parsed` is every usage record read, while
    `per_day[...]['records']` is the records the day table attributes to that
    day (a message whose records land on two days is counted on the earlier
    one), so it sums with the untimed records to `records_parsed` rather than
    being a second count. `per_day[...]['files']` counts the files holding a
    raw record stamped that day, so a message streamed across midnight puts a
    file under a day whose attributed record count can be zero.
    """
    block = {
        "available": False,
        "verdict": "not_measured",
        "files_read": 0,
        "files_skipped": 0,
        "skipped_lines": 0,
        "records_parsed": 0,
        "messages_parsed": 0,
        "bytes": 0,
        "digest": None,
        "digest_basis": None,
        "content_digest": None,
        "content_basis": None,
        "manifest": None,
        "manifest_total": 0,
        "manifest_truncated": False,
        "per_day": {},
        "reason": None,
    }
    if not inventory:
        block["verdict"] = "no_reading"
        block["reason"] = (
            "no transcript scan is in hand, so this verdict names no corpus it read"
        )
        return block
    block.update(
        available=True,
        verdict="reading_identified",
        files_read=int(inventory.get("files_read") or 0),
        files_skipped=int(inventory.get("files_skipped") or 0),
        skipped_lines=int(inventory.get("skipped_lines") or 0),
        records_parsed=int(inventory.get("records") or 0),
        messages_parsed=int(inventory.get("messages") or 0),
        bytes=int(inventory.get("bytes_read") or 0),
        digest=inventory.get("reading_digest"),
        digest_basis=inventory.get("digest_basis"),
        content_digest=inventory.get("content_digest"),
        content_basis=inventory.get("content_basis"),
        manifest=inventory.get("file_facts") or None,
        manifest_total=int(inventory.get("manifest_total") or 0),
        manifest_truncated=bool(inventory.get("manifest_truncated")),
    )
    day_files = inventory.get("day_files") or {}
    day_records = inventory.get("day_records") or {}
    for row in rows:
        day = row["day"]
        key = day if isinstance(day, str) else day.isoformat()
        block["per_day"][key] = {
            "files": int(day_files.get(day, day_files.get(key, 0)) or 0),
            "records": int(day_records.get(day, day_records.get(key, 0)) or 0),
        }
    return block


_PRIOR_KEYS = {
    # A reading reaches this tool in one of two shapes: the `_reading_axis`
    # block (files_read / bytes / records_parsed / digest) or the scan-shaped
    # dict quiet_days builds (files_read / bytes_read / records /
    # reading_digest). Both are accepted, and the pair of names is the only
    # place the two vocabularies meet.
    "files": ("files_read", "files"),
    "bytes": ("bytes", "bytes_read"),
    "records": ("records_parsed", "records"),
    "digest": ("digest", "reading_digest"),
    "content_digest": ("content_digest",),
}


def _prior_reading(doc):
    """Normalise a supplied reading, or refuse a document that is not one."""
    if not isinstance(doc, dict):
        raise ValueError(
            f"--compare-reading holds a JSON {type(doc).__name__}, not an object"
        )
    inner = doc.get("arbitration")
    if isinstance(inner, dict) and isinstance(inner.get("reading"), dict):
        doc = inner["reading"]
    block = {
        "files": None,
        "bytes": None,
        "records": None,
        "digest": None,
        "content_digest": None,
        "digest_basis": None,
        "content_basis": None,
    }
    found = False
    for name, keys in _PRIOR_KEYS.items():
        for key in keys:
            if key not in doc:
                continue
            found = True
            raw = doc[key]
            if name in {"files", "bytes", "records"}:
                try:
                    block[name] = int(raw)
                except (TypeError, ValueError):
                    block[name] = None
            else:
                block[name] = raw if isinstance(raw, str) and raw else None
            break
    if not found:
        raise ValueError(
            "--compare-reading holds JSON but no reading: expected a `quiet --json` "
            "payload or a reading block with a `content_digest`"
        )
    block["digest_basis"] = doc.get("digest_basis")
    block["content_basis"] = doc.get("content_basis")
    block["manifest"], block["manifest_total"], block["manifest_truncated"] = _prior_manifest(doc)
    return block


def _prior_manifest(doc):
    """Normalise a supplied file manifest, or refuse one that is not a manifest.

    A manifest is the only part of a reading that is not a fixed-size digest,
    so it is also the only part that can arrive malformed. Entries are
    `[name, size, mtime_ns, content_hash]` - the exact tuple the scan built -
    and a wrong shape is refused here rather than skipped at comparison time,
    because a silently dropped entry would make a changed file invisible.
    """
    raw = doc.get("manifest")
    total = doc.get("manifest_total")
    truncated = doc.get("manifest_truncated")
    try:
        total = int(total) if total is not None else 0
    except (TypeError, ValueError):
        total = 0
    if raw is None:
        return None, total, bool(truncated)
    if not isinstance(raw, list):
        raise ValueError("--compare-reading holds a manifest that is not a list of files")
    entries = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise ValueError(
                "--compare-reading holds a manifest entry that is not "
                "[name, size, mtime_ns, content_hash]"
            )
        name, size, mtime_ns, digest = item
        if not isinstance(name, str) or not name:
            raise ValueError("--compare-reading holds a manifest entry without a name")
        try:
            entries.append((name, int(size), int(mtime_ns), str(digest)))
        except (TypeError, ValueError):
            raise ValueError(
                f"--compare-reading holds a manifest entry with a non-numeric size or mtime: {name}"
            ) from None
    return entries, total or len(entries), bool(truncated)


def read_prior_reading(path):
    """Read a saved reading from disk, refusing anything that is not one."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read --compare-reading {path}: {exc.strerror or exc}")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--compare-reading is not JSON: {exc}")
    return _prior_reading(doc)


def _manifest_index(entries):
    return {name: (size, mtime_ns, digest) for name, size, mtime_ns, digest in entries}


def _pair_moves(added, removed, now_index, prior_index):
    """Pair a vanished name with a new one that carries the same bytes.

    A rename arrives as one `added` plus one `removed` - the same shape an add
    beside a delete takes. Size and content hash are already in the manifest,
    so the pair can be named as a move, but only when the pairing is forced:
    two files with identical content on either side make every pairing a guess,
    and a guessed subject is worse than a counted one.
    """

    def content_of(name, index):
        size, _mtime, digest = index[name]
        return (size, digest)

    by_added = {}
    for name in added:
        by_added.setdefault(content_of(name, now_index), []).append(name)
    by_removed = {}
    for name in removed:
        by_removed.setdefault(content_of(name, prior_index), []).append(name)

    moved = []
    paired_added = set()
    paired_removed = set()
    ambiguous = 0
    for content, new_names in by_added.items():
        old_names = by_removed.get(content) or []
        if not old_names:
            continue
        if len(new_names) == 1 and len(old_names) == 1:
            moved.append({"from": old_names[0], "to": new_names[0], "size": content[0]})
            paired_added.add(new_names[0])
            paired_removed.add(old_names[0])
        else:
            ambiguous += len(new_names)
    moved.sort(key=lambda row: row["from"])
    left_added = [name for name in added if name not in paired_added]
    left_removed = [name for name in removed if name not in paired_removed]
    return moved, left_added, left_removed, ambiguous


def _name_changes(current, prior):
    """Name the files behind a moved digest, or say exactly why naming stopped.

    A verdict says a corpus changed; it never says which file did. Two digests
    are one-way, so the subject can only come from a manifest both readings
    carry. Every refusal below is a case where a name could be missing and a
    partial list would read as a complete one.
    """
    attribution = {
        "available": False,
        "reason": None,
        "capped_at": ATTRIBUTION_LIMIT,
        "mtime_only": [],
        "mtime_only_total": 0,
        "content_changed": [],
        "content_changed_total": 0,
        "added": [],
        "added_total": 0,
        "removed": [],
        "removed_total": 0,
        "moved": [],
        "moved_total": 0,
        "moved_ambiguous_total": 0,
    }
    now_manifest = (current or {}).get("manifest")
    prior_manifest = (prior or {}).get("manifest") if isinstance(prior, dict) else None
    if not now_manifest and not prior_manifest:
        attribution["reason"] = (
            "neither reading carries a file manifest, so a changed file is counted but "
            "not named; re-run both with --manifest to name it"
        )
        return attribution
    if not now_manifest:
        attribution["reason"] = (
            "this run carries no file manifest, so the supplied one has nothing to be "
            "matched against; re-run with --manifest to name the files"
        )
        return attribution
    if not prior_manifest:
        attribution["reason"] = (
            "the supplied reading carries no file manifest (it was taken without "
            "--manifest), so a changed file can be counted but not named"
        )
        return attribution
    if (current or {}).get("manifest_truncated") or (prior or {}).get("manifest_truncated"):
        attribution["reason"] = (
            f"one of the manifests hit the {MANIFEST_LIMIT}-file cap, so a name can be "
            "missing; refusing to name part of a corpus"
        )
        return attribution
    for label, side in (("this run", current), ("the supplied reading", prior)):
        entries = side.get("manifest") or []
        total = side.get("manifest_total") or len(entries)
        if total != len(entries):
            attribution["reason"] = (
                f"{label} holds {len(entries)} of {total} file(s) in its manifest, so a "
                "name could be missing"
            )
            return attribution
        files = _as_int(side.get("files"))
        if files is not None and files != len(entries):
            attribution["reason"] = (
                f"{label} names {len(entries)} file(s) but reports {files} file(s) read, "
                "so a name could be missing"
            )
            return attribution
    now_index = _manifest_index(current["manifest"])
    prior_index = _manifest_index(prior["manifest"])
    mtime_only = []
    content_changed = []
    for name in sorted(set(now_index) & set(prior_index)):
        now_size, now_mtime, now_hash = now_index[name]
        prior_size, prior_mtime, prior_hash = prior_index[name]
        if now_hash != prior_hash:
            content_changed.append(name)
        elif (now_size, now_mtime) != (prior_size, prior_mtime):
            mtime_only.append(name)
    added = sorted(set(now_index) - set(prior_index))
    removed = sorted(set(prior_index) - set(now_index))
    if current.get("content_equal") and content_changed:
        # The byte digest says every file is identical; the manifest says one
        # of them is not. One of the two readings was taken under another rule.
        attribution["reason"] = (
            "the content digests agree while the manifest names a changed file "
            f"({content_changed[0]}), so the two readings were not produced under one rule"
        )
        return attribution
    attribution["available"] = True
    moved, added, removed, ambiguous = _pair_moves(added, removed, now_index, prior_index)
    attribution["moved"] = moved[:ATTRIBUTION_LIMIT]
    attribution["moved_total"] = len(moved)
    attribution["moved_ambiguous_total"] = ambiguous
    attribution["mtime_only"] = mtime_only[:ATTRIBUTION_LIMIT]
    attribution["mtime_only_total"] = len(mtime_only)
    attribution["content_changed"] = content_changed[:ATTRIBUTION_LIMIT]
    attribution["content_changed_total"] = len(content_changed)
    attribution["added"] = added[:ATTRIBUTION_LIMIT]
    attribution["added_total"] = len(added)
    attribution["removed"] = removed[:ATTRIBUTION_LIMIT]
    attribution["removed_total"] = len(removed)
    return attribution


def compare_readings(current, prior):
    """Compare two readings, then name the files behind the difference.

    The verdict is decided by `_compare_readings`; the naming is added here for
    the same reason `quiet_days` wraps its own body - every early return carries
    the naming block, so a refusal path cannot silently drop the sentence that
    says why no file was named.
    """
    block = _compare_readings(current, prior)
    now_side = {
        "manifest": (current or {}).get("manifest"),
        "manifest_total": (current or {}).get("manifest_total"),
        "manifest_truncated": (current or {}).get("manifest_truncated"),
        "files": (block.get("current") or {}).get("files"),
        "content_equal": block.get("content_equal"),
    }
    block["attribution"] = _name_changes(now_side, prior)
    return block


def _compare_readings(current, prior):
    """Say which of five differences the gap between two readings is.

    One digest cannot answer this on its own, and two digests only can when
    both readings are in hand - which is why `quiet` takes a prior reading from
    the caller instead of writing one (it stays read-only). The classes are
    decided by the pair, never by one side:

    * `same_reading` - both digests equal: same names, same bytes, same mtime.
    * `metadata_only` - the content digest is equal and the path digest is not:
      no byte in the corpus moved, so the corpus was not edited; what moved is
      (name, size, mtime). This is the verdict the r312 debt named.
    * `bytes_differ` - same file count and same total bytes, different content
      digest: an edit that kept the size, a rename, or a swap between files.
    * `size_differs` - same file count, different total bytes.
    * `file_set_differs` - a different number of files.

    Refusals are verdicts, not silence: no supplied reading (`no_prior`), a run
    with no reading of its own (`no_current_reading`), a supplied block from a
    version with no byte axis or two blocks that contradict each other
    (`not_comparable`). A contradiction is checked rather than classified: two
    readings produced under the same rule cannot hold the same content digest
    and a different file or record count, so such a pair is one of them being
    edited or taken under another rule.
    """
    now = {
        "files": _as_int((current or {}).get("files_read")),
        "bytes": _as_int((current or {}).get("bytes")),
        "records": _as_int((current or {}).get("records_parsed")),
        "digest": (current or {}).get("digest"),
        "content_digest": (current or {}).get("content_digest"),
    }
    block = {
        "available": False,
        "verdict": None,
        "reason": None,
        "content_equal": None,
        "path_equal": None,
        "files_delta": None,
        "bytes_delta": None,
        "records_delta": None,
        "current": now,
        "prior": None,
    }
    if prior is None:
        block["verdict"] = "no_prior"
        block["reason"] = (
            "--compare-reading was not given, so this reading is compared with no "
            "other and whether the bytes moved is not measured"
        )
        return block
    block["prior"] = dict(prior)
    if not (current or {}).get("available"):
        block["verdict"] = "no_current_reading"
        block["reason"] = (
            "this run holds no reading, so the supplied one has nothing to be compared with"
        )
        return block
    if not prior.get("content_digest"):
        block["verdict"] = "not_comparable"
        block["reason"] = (
            "the supplied reading carries no content_digest, so it has no byte axis: it "
            "was taken before 0.48.0 or was not a reading at all"
        )
        return block
    for name, field in (("files", "files_delta"), ("bytes", "bytes_delta"), ("records", "records_delta")):
        if now[name] is not None and prior.get(name) is not None:
            block[field] = now[name] - prior[name]
    content_equal = prior["content_digest"] == now["content_digest"]
    path_equal = None
    if prior.get("digest"):
        path_equal = prior["digest"] == now["digest"]
    block["content_equal"] = content_equal
    block["path_equal"] = path_equal
    if content_equal:
        contradictions = []
        if block["files_delta"] not in (None, 0):
            contradictions.append(f"{prior['files']} vs {now['files']} file(s)")
        if block["records_delta"] not in (None, 0):
            contradictions.append(f"{prior['records']} vs {now['records']} record(s)")
        if contradictions:
            block["verdict"] = "not_comparable"
            block["reason"] = (
                "the two blocks state the same content digest but a different count ("
                + ", ".join(contradictions)
                + "), so one of them was not produced under this rule"
            )
            block["available"] = False
            return block
        block["available"] = True
        if path_equal is False:
            block["verdict"] = "metadata_only"
        else:
            block["verdict"] = "same_reading"
            if path_equal is None:
                block["reason"] = (
                    "the supplied reading carries no path digest, so only the byte axis "
                    "was compared"
                )
        return block
    block["available"] = True
    if block["files_delta"] not in (None, 0):
        block["verdict"] = "file_set_differs"
    elif block["bytes_delta"] not in (None, 0):
        block["verdict"] = "size_differs"
    else:
        block["verdict"] = "bytes_differ"
    return block


_CHAIN_ORDER_NOTE = (
    "no reading stamps when it was taken, so the order below is the order handed in "
    "(oldest first) and is the caller's claim, not a measured sequence"
)


def _chain_side(row, index, label):
    """One reading as the chain sees it: counts, digests, and whether it exists.

    Two vocabularies meet here for the same reason `_PRIOR_KEYS` exists: a saved
    reading says `files`/`records`, while the block this run builds says
    `files_read`/`records_parsed`. Reading only the saved names would leave every
    delta against the current run unmeasured - the pair a reader trusts most.
    """
    row = row or {}

    def first(*keys):
        for key in keys:
            if row.get(key) is not None:
                return row[key]
        return None

    return {
        "index": index,
        "label": label,
        "present": bool(row),
        "files": first("files_read", "files"),
        "bytes": first("bytes", "bytes_read"),
        "records": first("records_parsed", "records"),
        "digest": first("digest", "reading_digest"),
        "content_digest": row.get("content_digest"),
        "manifest": row.get("manifest"),
        "manifest_total": row.get("manifest_total"),
        "manifest_truncated": bool(row.get("manifest_truncated")),
    }


_CHAIN_NAME_ROWS = 4
_CHAIN_NAME_ROOM = 54


def _fit_name(name, room=_CHAIN_NAME_ROOM):
    """Shorten one name for the screen, keeping both ends of it.

    A transcript path can be longer than a line, and the wrap helper cannot
    break a token without spaces. The end of a manifest name is the file name
    and the start is the directory, so the middle is what a reader can lose -
    and losing it is stated on a line of its own rather than left to look like
    the whole name.
    """
    name = str(name)
    if len(name) <= room:
        return name
    head = max(8, room // 2 - 1)
    tail = max(8, room - head - 1)
    return name[:head] + "\u2026" + name[-tail:]

_CHAIN_NAMES_LIMIT = (
    "names are relative to the transcript root; content is compared under a name that stayed "
    "(the manifest's 16-hex sha256) and between a name that left and one that entered when their "
    "bytes are identical and the match is one-to-one, so a rename that also edits its bytes still "
    "reads as one name leaving while another enters"
)


def _chain_renames(named, contents):
    """Pair a name that vanished with one that appeared carrying the same bytes.

    A rename arrives in a chain as one name leaving and another entering on the
    same step, which is exactly the shape a delete beside a create takes. The
    manifest already carries size and a content hash per name, so the pair can
    be forced when the bytes are identical and unique on both sides - the rule
    `_pair_moves` uses for a single pair of readings. Two names that share bytes
    are not paired (a guessed subject is worse than a counted one) and are
    counted as ambiguous instead.

    Only neighbouring readings are compared. A name that skipped a reading may
    have been renamed in the gap, and nothing here can see that step.
    """
    by_index = {side["index"]: side for side in named}
    order = sorted(by_index)
    renames = []
    ambiguous = 0
    for position in range(len(order) - 1):
        before, later = order[position], order[position + 1]
        if later != before + 1:
            continue
        prior_names = {item[0] for item in (by_index[before]["manifest"] or []) if item}
        now_names = {item[0] for item in (by_index[later]["manifest"] or []) if item}
        # A name with no content hash cannot be matched, and dropping it here
        # keeps an unhashed file from blocking the pairs that are provable.
        gone = [name for name in sorted(prior_names - now_names)
                if contents[name][before][1] is not None]
        came = [name for name in sorted(now_names - prior_names)
                if contents[name][later][1] is not None]
        if not gone or not came:
            continue
        prior_index = {
            name: (contents[name][before][0], 0, contents[name][before][1]) for name in gone
        }
        now_index = {
            name: (contents[name][later][0], 0, contents[name][later][1]) for name in came
        }
        moved, _left_added, _left_removed, here = _pair_moves(
            came, gone, now_index, prior_index
        )
        ambiguous += here
        for pair in moved:
            renames.append(
                {"from": pair["from"], "to": pair["to"], "step": later, "size": pair["size"]}
            )
    return renames, ambiguous


def _chain_names(sides):
    """Follow each file name across the readings, or refuse to name any.

    The pair rows answer "did the bytes move between 2 and 3". They cannot
    answer "which file", because a 2000-entry manifest has no place on one
    line. This walks the same readings by name and reports the step a name
    first and last appears on - the only part of a chain that a digest cannot
    carry.

    It refuses in four cases, each with its own sentence: a reading without a
    manifest (nothing to follow), a truncated manifest (a name could be missing
    from it and would then look like it left), a manifest whose length
    contradicts its own file count (a partial list dressed as a whole one), and
    fewer than two readings carrying names.

    The manifest also carries a content hash per file, so where a name stayed it
    can be compared with itself across readings without inventing an identity:
    the name is the identity. `edited_at` lists the step at which that file's
    bytes changed. A name whose hash is missing on either side of a step is
    counted in `content_unmeasured` and never called edited - an absent hash is
    not a hash that matched.

    Two names can also be one file: a name that vanishes on a step while another
    appears with the *same* bytes is a rename, and counting it as one departure
    plus one arrival reports one event twice. The pairing rule is the one the
    pair row already uses (`_pair_moves`): size and content hash must match and
    the match must be forced one-to-one. A guessed rename is worse than a
    counted one, so an ambiguous match is counted and named as ambiguous, and a
    pairing is only attempted between neighbouring readings that both carry a
    manifest - a name that skipped a reading could have moved anywhere in the
    gap.
    """
    block = {
        "available": False,
        "verdict": "not_named",
        "reason": None,
        "basis": "file name, relative to the transcript root",
        "limit": _CHAIN_NAMES_LIMIT,
        "files": [],
        "throughout": 0,
        "entered": 0,
        "left": 0,
        "blink": 0,
        "changed": 0,
        "edited": 0,
        "content_unmeasured": 0,
        "renamed_total": 0,
        "renamed_ambiguous_total": 0,
        "renames": [],
        "readings_named": 0,
    }
    present = [side for side in sides if side["present"]]
    named = [side for side in present if side.get("manifest") is not None]
    block["readings_named"] = len(named)
    if len(present) < 2:
        block["reason"] = "fewer than two readings carry a reading, so no name can be followed"
        return block
    unnamed = [side["index"] for side in present if side.get("manifest") is None]
    if unnamed:
        block["reason"] = (
            f"reading(s) {', '.join(str(index) for index in unnamed)} carry no file manifest, "
            "so no name can be followed across this chain (pass --manifest when the reading "
            "is taken)"
        )
        return block
    truncated = [side["index"] for side in named if side.get("manifest_truncated")]
    if truncated:
        block["reason"] = (
            f"reading(s) {', '.join(str(index) for index in truncated)} hold a truncated "
            "manifest, so a name missing from it would look like it left"
        )
        return block
    for side in named:
        held = len(side["manifest"] or [])
        if side.get("files") is not None and side["files"] != held:
            block["reason"] = (
                f"reading {side['index']} counts {side['files']} file(s) but its manifest "
                f"holds {held}, so the list is not the whole reading"
            )
            return block

    indices = [side["index"] for side in present]
    first_index, last_index = indices[0], indices[-1]
    seen = {}
    hashes = {}
    contents = {}
    for side in named:
        for item in side["manifest"] or []:
            if not item:
                continue
            name = item[0]
            seen.setdefault(name, set()).add(side["index"])
            digest = item[3] if len(item) > 3 else None
            digest = digest.strip() if isinstance(digest, str) and digest.strip() else None
            hashes.setdefault(name, {})[side["index"]] = digest
            contents.setdefault(name, {})[side["index"]] = (item[1], digest)

    rows = []
    for name, steps in sorted(seen.items()):
        ordered = sorted(steps)
        span = [index for index in indices if ordered[0] <= index <= ordered[-1]]
        gap = len(ordered) != len(span)
        after = [index for index in indices if index > ordered[0]]
        left_at = next((index for index in after if index not in steps), None)
        if gap:
            state = "blink"
        elif ordered[0] == first_index and ordered[-1] == last_index:
            state = "throughout"
        elif ordered[-1] == last_index:
            state = "entered"
        elif ordered[0] == first_index:
            state = "left"
        else:
            state = "blink"
        by_step = hashes.get(name) or {}
        edited_at = []
        unmeasured = False
        for position in range(len(ordered) - 1):
            before, later = ordered[position], ordered[position + 1]
            # Only neighbouring readings compare content: a name that was absent
            # in between is a return, and its bytes on either side of the gap say
            # nothing about the step it came back on.
            if later != before + 1:
                continue
            was, now = by_step.get(before), by_step.get(later)
            if not was or not now:
                unmeasured = True
            elif was != now:
                edited_at.append(later)
        rows.append(
            {
                "name": name,
                "state": state,
                "steps": ordered,
                "entered_at": ordered[0] if ordered[0] != first_index else None,
                "left_at": left_at if state in {"left", "blink"} else None,
                "edited_at": edited_at,
                "content_unmeasured": unmeasured,
                "renamed_to": None,
                "renamed_from": None,
            }
        )
    block["available"] = True
    block["files"] = rows
    block["throughout"] = sum(1 for row in rows if row["state"] == "throughout")
    block["entered"] = sum(1 for row in rows if row["state"] == "entered")
    block["left"] = sum(1 for row in rows if row["state"] == "left")
    block["blink"] = sum(1 for row in rows if row["state"] == "blink")
    block["changed"] = block["entered"] + block["left"] + block["blink"]
    block["edited"] = sum(1 for row in rows if row["edited_at"])
    block["content_unmeasured"] = sum(1 for row in rows if row["content_unmeasured"])
    renames, ambiguous_renames = _chain_renames(named, contents)
    by_name = {row["name"]: row for row in rows}
    for pair in renames:
        if pair["from"] in by_name:
            by_name[pair["from"]]["renamed_to"] = pair["to"]
        if pair["to"] in by_name:
            by_name[pair["to"]]["renamed_from"] = pair["from"]
    block["renames"] = renames
    block["renamed_total"] = len(renames)
    block["renamed_ambiguous_total"] = ambiguous_renames
    if block["changed"]:
        block["verdict"] = "names_changed"
    elif block["edited"]:
        # The name set held, but bytes under it did not: calling that
        # `names_stable` would print an all-clear beside a pair that says
        # otherwise on the same screen.
        block["verdict"] = "names_held_content_moved"
    else:
        block["verdict"] = "names_stable"
    return block


def _chain_pair(now, prior):
    """Classify two neighbouring readings with the same rule a single pair gets.

    A pair needs a byte axis on *both* sides. `_compare_readings` refuses a prior
    without one, but a new reading with no content digest would otherwise fall
    through to a size comparison and read as a change; in a chain that would be
    a step invented at the position that has the least to say.
    """
    for side, name in ((now, "the later reading"), (prior, "the earlier reading")):
        if not side.get("content_digest"):
            return {
                "available": False,
                "verdict": "not_comparable",
                "content_equal": None,
                "files_delta": None,
                "bytes_delta": None,
                "records_delta": None,
                "reason": (
                    f"{name} of the pair carries no content_digest, so there is no byte axis "
                    "on that side"
                ),
            }
    return _compare_readings(
        {
            "available": True,
            "files_read": now["files"],
            "bytes": now["bytes"],
            "records_parsed": now["records"],
            "digest": now["digest"],
            "content_digest": now["content_digest"],
        },
        prior,
    )


def reading_chain(priors, current):
    """Walk a sequence of readings, classifying each neighbouring pair.

    A pair verdict says what changed between two readings. It cannot say *which*
    of a sequence of readings is the one that moved, and it silently drops a
    reading in the middle that carries no byte axis. This block answers both by
    walking the supplied readings in the order handed in:

    * one row per neighbouring pair, classified by `_compare_readings`, so the
      words are the ones the single-pair comparison already prints;
    * `verdict` in one word: `no_byte_moved` (every pair's content digest
      matched), `bytes_moved` (at least one did not), or `hole` (some reading
      could not be compared, so the chain is not a chain across it). A hole is
      named by position rather than skipped, because a three-reading chain that
      quietly compares as two is a wrong statement about which step moved;
    * `repeated_pairs` - positions whose content digests are equal while a pair
      between them differs. That is either a change that was undone or two
      trees that happen to hold the same bytes; the readings cannot separate
      those, so the count carries no cause. A pair whose counts contradict its
      equal digest is *not* counted as a return - it goes to `contradictions`.

    Nothing about the ordering is measured: a reading holds no timestamp, so the
    sequence is the caller's claim and `order_note` says so in every payload.
    """
    priors = list(priors or [])
    block = {
        "available": False,
        "verdict": "not_a_chain",
        "reason": None,
        "order": "as_given",
        "order_note": _CHAIN_ORDER_NOTE,
        "readings": [],
        "pairs": [],
        "changed_pairs": 0,
        "hole_pairs": 0,
        "repeated_pairs": [],
        "repeated_total": 0,
        "contradiction_total": 0,
    }
    sides = [_chain_side(row, index + 1, str(index + 1)) for index, row in enumerate(priors)]
    sides.append(_chain_side(current, len(priors) + 1, "this run"))
    block["readings"] = sides
    block["names"] = _chain_names(sides)
    if len(priors) < 2:
        block["reason"] = (
            "fewer than two prior readings were handed in, so there is no chain to walk: "
            "one prior reading is the pair comparison above"
        )
        return block
    block["available"] = True
    missing = [side["index"] for side in sides if not side["present"]]
    blind = [
        side["index"]
        for side in sides
        if side["present"] and not side.get("content_digest")
    ]
    for position in range(len(sides) - 1):
        left, right = sides[position], sides[position + 1]
        if not left["present"] or not right["present"]:
            block["pairs"].append(
                {
                    "from": left["index"],
                    "to": right["index"],
                    "verdict": "not_comparable",
                    "available": False,
                    "same_bytes": None,
                    "files_delta": None,
                    "bytes_delta": None,
                    "records_delta": None,
                    "reason": "this position holds no reading",
                }
            )
            continue
        pair = _chain_pair(right, left)
        block["pairs"].append(
            {
                "from": left["index"],
                "to": right["index"],
                "verdict": pair["verdict"],
                "available": bool(pair.get("available")),
                "same_bytes": pair.get("content_equal"),
                "files_delta": pair.get("files_delta"),
                "bytes_delta": pair.get("bytes_delta"),
                "records_delta": pair.get("records_delta"),
                "reason": pair.get("reason"),
            }
        )
    block["changed_pairs"] = sum(1 for pair in block["pairs"] if pair["same_bytes"] is False)
    block["hole_pairs"] = sum(1 for pair in block["pairs"] if not pair["available"])
    for i, left in enumerate(sides):
        for right in sides[i + 2 :]:
            if not left["present"] or not right["present"]:
                continue
            if not left.get("content_digest") or not right.get("content_digest"):
                continue
            if left["content_digest"] != right["content_digest"]:
                continue
            counts_differ = any(
                left.get(name) is not None
                and right.get(name) is not None
                and left[name] != right[name]
                for name in ("files", "records")
            )
            if counts_differ:
                block["contradiction_total"] += 1
                continue
            block["repeated_pairs"].append([left["index"], right["index"]])
    block["repeated_total"] = len(block["repeated_pairs"])
    if missing:
        block["verdict"] = "hole"
        block["reason"] = (
            f"position {', '.join(str(index) for index in missing)} of the sequence holds no "
            "reading, so the pairs on either side of it were not compared"
        )
    elif blind:
        block["verdict"] = "hole"
        block["reason"] = (
            f"position {', '.join(str(index) for index in blind)} carries no content_digest, "
            "so the pairs on both sides of it have no byte axis to compare"
        )
    elif block["changed_pairs"]:
        block["verdict"] = "bytes_moved"
    else:
        block["verdict"] = "no_byte_moved"
    return block


_CHAIN_VERDICT_TEXT = {
    "no_byte_moved": "every neighbouring pair holds the same bytes -- no edit along the chain",
    "bytes_moved": "at least one neighbouring pair holds different bytes",
    "hole": "the chain is not connected across every position, so it says nothing about a step",
    "not_a_chain": "fewer than two prior readings, so nothing was walked",
}


def _chain_lines(chain, width=80):
    """Render the chain, or a sentence saying why there is none."""
    if not chain:
        return []
    lines = []

    def wrap(head, text):
        if len(head + text) <= width:
            lines.append(head + text)
            return
        current = head
        pad = " " * len(head)
        for word in text.split():
            if current.strip() and len(current) + 1 + len(word) > width:
                lines.append(current.rstrip())
                current = pad + word
            else:
                current += (" " if current.strip() else "") + word
        lines.append(current.rstrip())

    if not chain.get("available"):
        wrap("    chain        ", "not measured -- " + (chain.get("reason") or "no chain"))
        return lines
    wrap(
        "    chain        ",
        f"{len(chain['readings'])} reading(s) walked oldest first, "
        f"{len(chain['pairs'])} neighbouring pair(s)",
    )
    wrap("    order        ", chain["order_note"])
    for pair in chain["pairs"]:
        left = chain["readings"][pair["from"] - 1]["label"]
        right = chain["readings"][pair["to"] - 1]["label"]
        if pair["available"]:
            deltas = ", ".join(
                f"{name} {'n/a' if pair[key] is None else f'{pair[key]:+d}'}"
                for name, key in (
                    ("files", "files_delta"),
                    ("bytes", "bytes_delta"),
                    ("records", "records_delta"),
                )
            )
            text = f"{pair['verdict']} ({deltas})"
        else:
            text = f"not compared -- {pair.get('reason') or 'no byte axis'}"
        wrap(f"    {left} -> {right}", "  " + text)
    verdict = chain["verdict"]
    summary = (
        f"{verdict} -- {_CHAIN_VERDICT_TEXT.get(verdict, '')}; "
        f"{chain['changed_pairs']} of {len(chain['pairs'])} pair(s) moved bytes"
    )
    if chain["hole_pairs"]:
        summary += f", {chain['hole_pairs']} not compared"
    if chain["reason"]:
        summary += f" -- {chain['reason']}"
    wrap("    chain verdict", " " + summary)
    if chain["repeated_total"]:
        spots = ", ".join(f"{left} and {right}" for left, right in chain["repeated_pairs"])
        wrap(
            "    returned     ",
            f"reading(s) {spots} hold bytes already seen, with a different pair in between "
            "-- an edit that was undone or another tree with the same bytes; the readings "
            "cannot say which",
        )
    if chain["contradiction_total"]:
        wrap(
            "    unpaired     ",
            f"{chain['contradiction_total']} pair(s) of readings hold the same content digest "
            "but a different count, so they are not counted as a return",
        )
    names = chain.get("names") or {}
    if names.get("available"):
        summary = (
            f"{len(names['files'])} file name(s) across {names['readings_named']} reading(s): "
            f"{names['throughout']} in every reading, {names['entered']} entered, "
            f"{names['left']} left, {names['blink']} blinked"
        )
        if names.get("edited"):
            summary += f", {names['edited']} edited in place"
        if names.get("content_unmeasured"):
            summary += f", {names['content_unmeasured']} with no content hash"
        wrap("    names        ", summary)
        if names.get("renamed_total"):
            for pair in names["renames"][: _CHAIN_NAME_ROWS]:
                wrap(
                    "    renamed      ",
                    f" {_fit_name(pair['from'], 24)} -> {_fit_name(pair['to'], 24)} on reading "
                    f"{pair['step']}, same size and content hash -- the name that left and the "
                    "one that entered are this one file",
                )
        if names.get("renamed_ambiguous_total"):
            wrap(
                "    renamed      ",
                f"{names['renamed_ambiguous_total']} name(s) left while another entered with the "
                "same bytes on one step, but the match is not one-to-one, so no rename is named",
            )
        changed = [
            item
            for item in names["files"]
            if item["state"] != "throughout" or item.get("edited_at")
        ]
        shortened = 0
        for item in changed[: _CHAIN_NAME_ROWS]:
            shown = _fit_name(item["name"])
            if shown != item["name"]:
                shortened += 1
            edits = item.get("edited_at") or []
            tail = ""
            if edits:
                where = (
                    f"reading {edits[0]}"
                    if len(edits) == 1
                    else "readings " + ", ".join(str(i) for i in edits)
                )
                tail = f", bytes changed on {where}"
            label = item["state"]
            if item["state"] == "entered":
                if item["entered_at"] == item["steps"][-1]:
                    text = f"{shown} -- entered on reading {item['entered_at']}, the last reading"
                else:
                    text = (
                        f"{shown} -- entered on reading {item['entered_at']}, "
                        f"held to reading {item['steps'][-1]}"
                    )
                if item.get("renamed_from"):
                    # Without this the row reads as an arrival beside the
                    # `left` row's departure, which is the one reading the
                    # renamed line above already refused.
                    text += f", renamed from {_fit_name(item['renamed_from'])}"
            elif item["state"] == "left":
                text = (
                    f"{shown} -- last on reading {item['steps'][-1]}, "
                    f"absent on reading {item['left_at']}"
                )
                if item.get("renamed_to"):
                    text += f", so this file is now named {_fit_name(item['renamed_to'])}"
            elif item["state"] == "blink":
                lost = [
                    index
                    for index in range(item["steps"][0], item["steps"][-1] + 1)
                    if index not in item["steps"]
                ]
                text = (
                    f"{shown} -- on reading(s) {', '.join(str(i) for i in item['steps'])}, "
                    f"absent on {', '.join(str(i) for i in lost)}"
                )
            else:
                # The name held and only its bytes moved, so the step is the
                # whole statement: saying "in every reading" alone would be an
                # all-clear beside a pair that found different bytes.
                label = "edited"
                text = (
                    f"{shown} -- same name on reading(s) "
                    f"{', '.join(str(i) for i in item['steps'])}"
                )
            wrap(f"    {label:<13}", " " + text + tail)
        hidden = len(changed) - _CHAIN_NAME_ROWS
        if hidden > 0:
            wrap("    ", f" +{hidden} more name(s) changed; the full list is in --json")
        if shortened:
            wrap(
                "    names full   ",
                f"{shortened} name(s) shortened to fit; the full name is in --json",
            )
        wrap("    names limit  ", names["limit"])
    elif names:
        wrap("    names        ", "not named -- " + (names.get("reason") or "no name to follow"))
    return lines


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_COMPARE_TEXT = {
    "same_reading": "same names, same bytes, same mtime -- nothing moved",
    "metadata_only": "no byte moved; only (name, size, mtime) did -- a touch, not an edit",
    "bytes_differ": "same file count and same byte total, different bytes",
    "size_differs": "same file count, a different byte total",
    "file_set_differs": "a different number of files",
}


def _compare_lines(block, width=80):
    """Render the comparison, or a sentence saying it did not happen."""
    if not block:
        return []
    lines = []

    def emit(label, text):
        head = " " * 17 + f"{label:<8}"
        if len(head + text) <= width:
            lines.append(head + text)
            return
        current = head
        for word in text.split():
            if current.strip() and len(current) + 1 + len(word) > width:
                lines.append(current.rstrip())
                current = " " * 26 + word
            else:
                current += (" " if current.strip() else "") + word
        lines.append(current.rstrip())

    if not block.get("available"):
        emit("compare", "not measured -- " + block["reason"])
        return lines
    prior = block["prior"]
    size = prior.get("bytes")
    size_text = ""
    if isinstance(size, int):
        if size >= 1024 ** 3:
            size_text = f", {size / (1024 ** 3):.2f} GiB"
        else:
            size_text = f", {size / (1024 ** 2):.1f} MiB"
    emit(
        "prior",
        f"{prior.get('files')} file(s), {prior.get('records')} record(s)" + size_text,
    )
    change = []
    for label, key in (("files", "files_delta"), ("bytes", "bytes_delta"), ("records", "records_delta")):
        value = block.get(key)
        change.append(f"{label} {'n/a' if value is None else f'{value:+d}'}")
    emit("change", ", ".join(change) + " (this run minus the supplied one)")
    verdict = block["verdict"]
    emit("verdict", f"{verdict} -- {_COMPARE_TEXT.get(verdict, '')}")
    if block.get("reason"):
        emit("", block["reason"])
    attribution = block.get("attribution") or {}
    if not attribution.get("available"):
        emit("named", "not measured -- " + (attribution.get("reason") or "no manifest"))
        return lines
    parts = []
    if attribution.get("moved_total"):
        rows = attribution.get("moved") or []
        total = attribution["moved_total"]
        shown = ", ".join(f"{row['from']} -> {row['to']}" for row in rows)
        more = f" (+{total - len(rows)} more)" if total > len(rows) else ""
        parts.append(f"moved {total}: {shown}{more}")
    for key, label in (
        ("mtime_only", "touched"),
        ("content_changed", "edited"),
        ("added", "added"),
        ("removed", "removed"),
    ):
        total = attribution.get(f"{key}_total") or 0
        if not total:
            continue
        names = attribution.get(key) or []
        shown = ", ".join(names)
        more = f" (+{total - len(names)} more)" if total > len(names) else ""
        parts.append(f"{label} {total}: {shown}{more}")
    emit("named", "; ".join(parts) if parts else "no file in the shared manifest moved")
    ambiguous = attribution.get("moved_ambiguous_total") or 0
    if ambiguous:
        emit(
            "unpaired",
            f"{ambiguous} file(s) share content with a missing name but cannot be paired "
            "one-to-one, so they stay counted as added and removed",
        )
    return lines


def _witness_axis(billed_days, record_sum, floor, tol, unmeasured=()):
    """Does the corpus still hold the records the receipt summed?

    The aggregate ratio in `_quiet_meter_check` divides the receipt's day table
    by whatever records survive in the transcripts. That division is only a
    meter disagreement when both sides read the same records. This axis states,
    per billed day, which of the three cases the corpus is in:

    * `exact` - the receipt's day total equals the sum of the usage records the
      corpus holds for that day, so the evidence for that day is present and
      the two stores agree on it;
    * `remnant` - the corpus holds some records and less than the receipt, so
      the day's evidence is incomplete and the difference cannot be read as a
      meter factor;
    * `absent` - the corpus holds no record for the day at all: the receipt
      states a number and nothing on this machine can check it.

    A fourth state, `over`, covers the opposite direction - the corpus holds
    more than the receipt - which is a genuine disagreement and is never folded
    into the evidence holes. `checked_share` is the share of the receipt's
    tokens that sits on exact days, so a total ratio can be read against how
    much of the bill was checkable in the first place.
    """
    block = {
        "available": False,
        "verdict": "not_measured",
        "days": {
            "exact": 0,
            "remnant": 0,
            "absent": 0,
            "over": 0,
            "unmeasured": 0,
        },
        "tokens": {"exact": 0, "remnant": 0, "absent": 0, "over": 0},
        # Days the floor keeps out of this axis. They are not evidence holes -
        # they are simply too small to weigh - but leaving them uncounted made
        # `receipt_total` a different number from the bill printed above it.
        "min_tokens": floor,
        "days_below_floor": 0,
        "tokens_below_floor": 0,
        "receipt_total": 0,
        "record_sum_total": 0,
        "residual_tokens": 0,
        "unwitnessed_tokens": 0,
        "checked_share": None,
        "worst_day": None,
        "worst_day_ratio": None,
        "reason": None,
    }
    # Deliberately not limited to the window where both stores reach: a day the
    # receipt bills and the corpus never reaches is the absent case, and the
    # intersection would drop exactly those days.
    rows, receipt_total = _witness_rows(
        billed_days, record_sum, floor, tol, unmeasured
    )
    below = [
        int(tokens)
        for tokens in (billed_days or {}).values()
        if tokens and int(tokens) < floor
    ]
    block["days_below_floor"] = len(below)
    block["tokens_below_floor"] = sum(below)
    if not rows:
        block["reason"] = (
            f"no billed day reaches the {floor:,}-token floor, so no day's evidence can be "
            "weighed"
        )
        return block
    block["receipt_total"] = receipt_total
    for row in rows:
        state = row["state"]
        billed = row["receipt"]
        block["days"][state] += 1
        if state in block["tokens"]:
            block["tokens"][state] += billed
        block["record_sum_total"] += row["record_sum"]
    block["available"] = True

    remnant = [row for row in rows if row["state"] == "remnant"]
    block["residual_tokens"] = sum(row["receipt"] - row["record_sum"] for row in remnant)
    block["unwitnessed_tokens"] = block["tokens"]["remnant"] + block["tokens"]["absent"]
    if block["receipt_total"]:
        block["checked_share"] = round(block["tokens"]["exact"] / block["receipt_total"], 4)
    if remnant:
        worst = max(remnant, key=lambda row: row["receipt"])
        # Billed days arrive keyed by `date` from the receipt's own table; a date
        # object reached the JSON writer once already, so the label is stringified
        # here and nowhere else.
        day = worst["day"]
        block["worst_day"] = day if isinstance(day, str) else day.isoformat()
        block["worst_day_ratio"] = round(worst["receipt"] / worst["record_sum"], 4)
    if block["days"]["over"]:
        block["verdict"] = "corpus_heavier_on_a_witnessed_day"
    elif block["days"]["exact"] == len(rows):
        block["verdict"] = "every_day_exact"
    elif block["days"]["exact"] and not block["days"]["unmeasured"]:
        block["verdict"] = "exact_where_evidence_survives"
    else:
        block["verdict"] = "no_exact_day"
    return block


def _quiet_meter_check(
    billed_days,
    transcript_days,
    transcript_written,
    window,
    floor,
    tol,
    written_unknown=(),
    billed_by_model=None,
    transcript_by_model=None,
    reading=None,
):
    """Do the two token meters count the same thing, or only the same days?

    `quiet` prints the receipt's in-window total beside the transcripts' total.
    A reader will subtract them, so the difference has to be measured rather
    than named. Both stores can differ in one mechanical way this machine can
    see: a streamed response writes its usage record more than once, so the
    transcripts hold a record sum (every write) and a deduped total (one
    message, largest value per bucket).

    The receipt's day table is compared against BOTH figures over the days both
    meters state a number for. Three outcomes are allowed: the receipt matches
    the record sum, it matches the deduped total, or it matches neither - and
    in the last case no cause is claimed, because the numbers do not support
    one. A day below the token floor is counted and does not vote.
    """
    block = {
        "available": False,
        "verdict": "not_measured",
        "days_compared": 0,
        "days_below_floor": 0,
        "min_tokens": floor,
        "tolerance": tol,
        "receipt_tokens": None,
        "transcript_deduped": None,
        "transcript_record_sum": None,
        "ratio_deduped": None,
        "ratio_record_sum": None,
        "per_day_verdict": "not_measured",
        "days_matching_individually": None,
        "days_receipt_heavy": None,
        "days_transcript_heavy": None,
        "max_day_ratio": None,
        "min_day_ratio": None,
        "reason": None,
    }
    block.update(_model_axis({}, {}, [], tol))
    block["witness"] = _witness_axis(
        billed_days, transcript_written, floor, tol, written_unknown
    )
    witness_rows, _ = _witness_rows(
        billed_days, transcript_written, floor, tol, written_unknown
    )
    block["replay"] = _replay_axis(witness_rows, transcript_days)
    block["reading"] = _reading_axis(reading, witness_rows)
    if not billed_days:
        block["verdict"] = "no_token_meter"
        block["reason"] = (
            "this receipt carries no per-day token table, so it states no number a transcript "
            "total could be checked against"
        )
        return block

    start = end = None
    if window:
        start, end = window
    shared = [
        day
        for day, billed in billed_days.items()
        if billed and transcript_days.get(day)
        and (start is None or start <= day <= end)
    ]
    if not shared:
        block["verdict"] = "no_shared_token_days"
        block["reason"] = (
            "no day is stated by both token meters inside the window, so the two totals cover "
            "different days and cannot be compared"
        )
        return block

    unknown = [day for day in shared if day in set(written_unknown)]
    if unknown:
        block["verdict"] = "no_record_sum"
        block["reason"] = (
            f"{len(unknown)} day(s) hold no record sum in the scan, so the receipt cannot be "
            "checked against both transcript figures; the deduped total is not substituted for it"
        )
        return block

    deciding = sorted(day for day in shared if billed_days[day] >= floor)
    block["days_below_floor"] = len(shared) - len(deciding)
    if not deciding:
        block["verdict"] = "no_day_above_floor"
        block["reason"] = (
            f"{len(shared)} day(s) are stated by both token meters and every one is below "
            f"{floor:,} token(s), so no day is large enough to decide anything"
        )
        return block

    receipt = sum(billed_days[day] for day in deciding)
    deduped = sum(transcript_days[day] for day in deciding)
    written = sum((transcript_written or {}).get(day, 0) for day in deciding)
    block["available"] = True
    block["days_compared"] = len(deciding)
    block["receipt_tokens"] = receipt
    block["transcript_deduped"] = deduped
    block["transcript_record_sum"] = written
    if deduped:
        block["ratio_deduped"] = round(receipt / deduped, 4)
    if written:
        block["ratio_record_sum"] = round(receipt / written, 4)

    def _matches(ratio):
        return ratio is not None and abs(ratio - 1) <= tol

    deduped_hit = _matches(block["ratio_deduped"])
    written_hit = _matches(block["ratio_record_sum"])
    if deduped_hit and written_hit:
        block["verdict"] = "both_match"
        block["reason"] = (
            "both transcript figures are within tolerance of the receipt, so on these days the "
            "corpus holds no repeated write and the two stores agree on the amount"
        )
    elif written_hit:
        block["verdict"] = "record_sum_matches"
        block["reason"] = (
            "the receipt's day table is the transcript record sum: the two sides count writes and "
            "unique messages, so the gap is a repeated-write artifact and neither number is a "
            "meter error"
        )
    elif deduped_hit:
        block["verdict"] = "deduped_matches"
        block["reason"] = (
            "the receipt's day table matches the deduped transcript total, so the receipt counts "
            "each message once and the repeated writes in the transcript corpus are not in it"
        )
    else:
        block["verdict"] = "neither"
        block["reason"] = (
            "neither transcript figure is within tolerance of the receipt, so repeated writes do "
            "not account for the gap on their own and the remainder is not explained here"
        )

    # The two sums above are taken over the same days, which is weaker than it
    # looks: a receipt-heavy day and a transcript-heavy day can cancel exactly,
    # leaving the ratio at 1.00 while no day agrees. So each deciding day is
    # also compared with its own pair, and a cancelled-out aggregate is not
    # upgraded to agreement.
    matched_axes = []
    if written_hit:
        matched_axes.append(transcript_written)
    if deduped_hit:
        matched_axes.append(transcript_days)
    axes = matched_axes or [transcript_days]
    per_day_tolerance = tol
    agreeing = 0
    for day in deciding:
        if all(
            abs((billed_days[day] or 0) / axis[day] - 1) <= per_day_tolerance
            for axis in axes
            if axis.get(day)
        ):
            agreeing += 1
    heavy = sum(
        1 for day in deciding if billed_days[day] > transcript_days[day] * (1 + tol)
    )
    light = sum(
        1 for day in deciding if billed_days[day] < transcript_days[day] * (1 - tol)
    )
    day_ratios = [(billed_days[day] / transcript_days[day], day) for day in deciding]
    block["days_matching_individually"] = agreeing
    block["days_receipt_heavy"] = heavy
    block["days_transcript_heavy"] = light
    block.update(_model_axis(billed_by_model or {}, transcript_by_model or {}, deciding, tol))
    block["max_day_ratio"] = round(max(day_ratios)[0], 4)
    block["min_day_ratio"] = round(min(day_ratios)[0], 4)
    block["max_day"] = max(day_ratios)[1].isoformat()
    block["min_day"] = min(day_ratios)[1].isoformat()
    if agreeing == len(deciding):
        block["per_day_verdict"] = "every_day_agrees"
    elif written_hit or deduped_hit:
        block["per_day_verdict"] = "aggregate_only"
        block["reason"] += (
            f"; day by day only {agreeing} of {len(deciding)} pair(s) agree "
            f"({heavy} receipt-heavy, {light} transcript-heavy), so the matching total is an "
            "aggregate result and not evidence that both meters count the same thing daily"
        )
    else:
        block["per_day_verdict"] = "no_agreement"
    return block


def quiet_days(cache, scan, min_tokens=None, tolerance=None, prior=None, priors=None):
    """Name the days each of two stores is silent on, and compare readings.

    `_quiet_days` decides the days; this wrapper adds the comparison of the
    reading it made against one the caller supplies (`--compare-reading`). The
    comparison is computed here rather than at each of the five exit paths, so
    a path that stops early still states what it compared - the same rule the
    reading block itself follows.

    `priors` is the whole sequence the caller handed in, oldest first. With two
    or more the wrapper also walks them as a chain, which is the only way a
    third reading gets a verdict: `reading_compare` still compares this run with
    the nearest one, and the chain says which neighbouring pair moved. One prior
    adds no chain key at all, so a payload from a single comparison does not
    change shape.
    """
    payload = _quiet_days(cache, scan, min_tokens=min_tokens, tolerance=tolerance)
    reading = (payload.get("arbitration") or {}).get("reading") or {}
    payload["reading_compare"] = compare_readings(reading, prior)
    if priors and len(priors) >= 2:
        payload["reading_chain"] = reading_chain(priors, reading)
    return payload


def _billed_reason(days, tokens, inside, span):
    """What a billed day the transcripts miss can honestly be said to be.

    `inside` is how many of those `days` fall between the first and the last
    day the transcripts reach. The window this command judges is the
    intersection of the two spans, so on this code path that count is all of
    them; the split is still computed and named, because a sentence about the
    edge of a corpus has to be paid for by the corpus, not assumed.
    """
    head = (
        f"{days} day(s) carry {tokens:,} token(s) in the receipt's own day table while the "
        "transcripts hold no token record for them"
    )
    tail = "the transcript total inside the window is a floor, not a measurement of it"
    if inside == days:
        return (
            f"{head}: all {days} lie inside the transcript span {span[0]}..{span[1]}, so the "
            "corpus reaches those dates and holds nothing for them - a hole inside the span, "
            f"not the edge of it; {tail}"
        )
    return (
        f"{head}: {inside} of {days} lie inside the transcript span {span[0]}..{span[1]} and "
        f"{days - inside} fall outside it; {tail}"
    )


def _quiet_days(cache, scan, min_tokens=None, tolerance=None):
    """Name the days each of two independent stores is silent on.

    `recent --by-day` can already say a day carries no token record. It cannot
    say why, and inside one store it never will: a holiday and an unmetered day
    leave the same shape. The receipt and the transcripts are separate stores,
    and each carries a positive claim of its own - the receipt counts messages
    and, in its `dailyModelTokens` table, states a token total per day; the
    transcripts count tokens.

    So this command never answers "this day was quiet". It answers which side
    holds a record and which does not:

    * both sides count something -> the day is measured;
    * the receipt's token table bills the day while the transcripts hold no
      token record -> the receipt states the number and the transcripts hold
      nothing for that day. The window is the two spans' intersection, so such
      a day lies *inside* the corpus and the gap is a hole in its records, not
      the edge of it, and the verdict says which of the two it is. Those tokens
      are counted separately and never added to the transcript side, and the
      day's own number is printed;
    * the receipt counts messages but neither token table states a number for
      the day -> the day is *not* quiet, but no store can say what it cost;
    * the transcripts hold tokens while the receipt has no activity row -> the
      receipt's message table does not cover that day;
    * no side holds a record -> silent, printed as an absence and never as a
      zero.

    The window is the intersection of the two spans, because a day only one
    store reaches cannot be called silent on both. A day the receipt's token
    table states is part of the receipt's span, even when it has no activity
    row. Days outside the window are counted, not dropped, in
    `days_outside_window`.
    """
    activity = _receipt_day_table(cache)
    billed_days = _receipt_token_table(cache)
    billed_by_model = _receipt_model_table(cache)
    floor, tol = _floor(min_tokens, tolerance)
    transcript_days = {}
    transcript_written = {}
    transcript_by_model = {}
    written_unknown = set()
    day_records = {}
    for key, entry in ((scan or {}).get("days") or {}).items():
        day = _day_of(key)
        if day is None or not isinstance(entry, dict):
            continue
        if "records" in entry:
            day_records[day] = int(entry.get("records") or 0)
        tokens = int(entry.get("total_tokens") or 0)
        split = {}
        for name, model_entry in (entry.get("by_model") or {}).items():
            if not isinstance(model_entry, dict):
                continue
            try:
                model_tokens = int(model_entry.get("total_tokens") or 0)
            except (TypeError, ValueError):
                continue
            if model_tokens:
                split[name] = model_tokens
        if split:
            transcript_by_model[day] = split
        if "naive_tokens" in entry:
            transcript_written[day] = int(entry.get("naive_tokens") or 0)
        else:
            # Substituting the deduped total here would make both candidates
            # the same number and turn a missing measurement into agreement.
            written_unknown.add(day)
        if tokens:
            transcript_days[day] = tokens

    # The witness/replay verdicts are statements about this reading, so the
    # reading travels with them: what was opened, what was parsed, and a digest
    # that lets a later run say whether it read the same bytes.
    reading = None
    if scan:
        reading = {
            "files_read": scan.get("files_read"),
            "files_skipped": scan.get("files_skipped"),
            "skipped_lines": scan.get("skipped_lines"),
            "records": scan.get("records"),
            "messages": scan.get("messages"),
            "bytes_read": scan.get("bytes_read"),
            "reading_digest": scan.get("reading_digest"),
            "digest_basis": scan.get("digest_basis"),
            "content_digest": scan.get("content_digest"),
            "content_basis": scan.get("content_basis"),
            "manifest_total": scan.get("manifest_total") or 0,
            "manifest_truncated": bool(scan.get("manifest_truncated")),
            "file_facts": scan.get("file_facts"),
            "day_files": scan.get("day_files") or {},
            "day_records": day_records,
        }

    payload = {
        "available": False,
        "verdict": "unavailable",
        "reason": None,
        "receipt_span": None,
        "last_computed": None,
        "receipt_token_days": len(billed_days),
        "transcript_span": None,
        "window": None,
        "days_in_window": 0,
        "days_measured": 0,
        "days_receipt_billed": 0,
        "days_activity_only": 0,
        "days_tokens_only": 0,
        "days_silent": 0,
        "days_outside_window": 0,
        "days_after_receipt": 0,
        "transcript_tail": None,
        "transcript_tail_tokens": 0,
        "measured_messages": 0,
        "measured_tokens": 0,
        "receipt_tokens_billed": 0,
        "receipt_tokens_in_window": 0,
        "transcript_tokens_in_window": 0,
        "arbitration": _quiet_meter_check(
            billed_days,
            transcript_days,
            transcript_written,
            None,
            floor,
            tol,
            written_unknown,
            billed_by_model,
            transcript_by_model,
            reading=reading,
        ),
        "rows": [],
    }

    if not activity:
        payload["verdict"] = "no_day_table"
        if billed_days:
            payload["reason"] = (
                f"the receipt carries {len(billed_days)} day(s) of per-day token totals but no "
                "dailyActivity rows, so there is no message count to compare with the "
                "transcripts"
            )
            payload["receipt_span"] = [str(min(billed_days)), str(max(billed_days))]
        else:
            payload["reason"] = (
                "the receipt carries no dailyActivity rows, so it makes no statement about any "
                "day and cannot be compared with the transcripts"
            )
        if transcript_days:
            payload["available"] = True
            payload["transcript_span"] = [
                str(min(transcript_days)),
                str(max(transcript_days)),
            ]
        return payload

    receipt_start, receipt_last = min(activity), max(activity)
    if billed_days:
        # A per-day token row is the receipt speaking for that day; leaving it
        # outside the span would drop a number the receipt in hand carries.
        receipt_start = min(receipt_start, min(billed_days))
        receipt_last = max(receipt_last, max(billed_days))
    computed = _day_of(cache.get("lastComputedDate"))
    receipt_end = max(receipt_last, computed) if computed else receipt_last
    payload["receipt_span"] = [str(receipt_start), str(receipt_end)]
    payload["last_computed"] = str(computed) if computed else None

    if not transcript_days:
        payload["verdict"] = "no_transcripts"
        payload["available"] = True
        payload["reason"] = (
            "no transcript on this machine carries a token record, so the receipt's days have "
            "nothing on this side to be checked against"
        )
        return payload

    payload["available"] = True
    transcript_start, transcript_end = min(transcript_days), max(transcript_days)
    payload["transcript_span"] = [str(transcript_start), str(transcript_end)]

    # The window is the intersection, so any day the transcripts record after
    # the receipt's last stated day falls off its end and is judged by nothing.
    # Counted and named here rather than folded into the window: the receipt
    # makes no claim about those days, and a window that silently stopped a
    # month before the corpus did is how a stale cache reads as a healthy one.
    tail_days = sorted(day for day in transcript_days if day > receipt_end)
    if tail_days:
        payload["days_after_receipt"] = len(tail_days)
        payload["transcript_tail"] = [str(tail_days[0]), str(tail_days[-1])]
        payload["transcript_tail_tokens"] = sum(transcript_days[day] for day in tail_days)

    start = max(receipt_start, transcript_start)
    end = min(receipt_end, transcript_end)
    if start > end:
        payload["verdict"] = "no_overlap"
        payload["reason"] = (
            "the receipt's span and the transcript span do not meet, so no day is witnessed "
            "by both stores and none may be called silent"
        )
        return payload

    payload["window"] = [str(start), str(end)]
    payload["arbitration"] = _quiet_meter_check(
        billed_days,
        transcript_days,
        transcript_written,
        (start, end),
        floor,
        tol,
        written_unknown,
        billed_by_model,
        transcript_by_model,
        reading=reading,
    )
    rows = []
    day = start
    while day <= end:
        messages = activity.get(day, 0)
        tokens = transcript_days.get(day)
        billed = billed_days.get(day)
        payload["receipt_tokens_in_window"] += billed or 0
        if messages > 0 and tokens is not None:
            payload["days_measured"] += 1
            payload["measured_messages"] += messages
            payload["measured_tokens"] += tokens
            day += timedelta(days=1)
            continue
        if tokens is not None:
            payload["days_tokens_only"] += 1
            rows.append(
                {
                    "date": str(day),
                    "kind": "tokens_only",
                    "messages": None,
                    "tokens": tokens,
                    "receipt_tokens": billed,
                }
            )
        elif billed:
            # The receipt states a token number for this day; only the
            # transcripts are silent. Calling this "no token record" would hide
            # a number the receipt in hand carries.
            payload["days_receipt_billed"] += 1
            payload["receipt_tokens_billed"] += billed
            rows.append(
                {
                    "date": str(day),
                    "kind": "receipt_billed",
                    "messages": messages if messages > 0 else None,
                    "tokens": None,
                    "receipt_tokens": billed,
                }
            )
        elif messages > 0:
            payload["days_activity_only"] += 1
            rows.append(
                {
                    "date": str(day),
                    "kind": "activity_only",
                    "messages": messages,
                    "tokens": None,
                    "receipt_tokens": None,
                }
            )
        else:
            payload["days_silent"] += 1
            rows.append(
                {
                    "date": str(day),
                    "kind": "silent",
                    "messages": None,
                    "tokens": None,
                    "receipt_tokens": None,
                }
            )
        day += timedelta(days=1)

    payload["rows"] = rows
    payload["days_in_window"] = (end - start).days + 1
    payload["transcript_tokens_in_window"] = sum(
        tokens for day, tokens in transcript_days.items() if start <= day <= end
    )
    payload["days_outside_window"] = len(
        [d for d in activity if not start <= d <= end]
    ) + len([d for d in transcript_days if not start <= d <= end]) + len(
        [d for d in billed_days if not start <= d <= end]
    )

    if payload["days_receipt_billed"]:
        # Every day in this window lies between the first and the last day the
        # transcripts reach, because the window is their intersection. So the
        # count below is the evidence for the sentence: if all of the billed
        # days fall inside the span, the corpus reaches their dates and holds
        # nothing for them, which is a hole inside the span and not its edge.
        inside = sum(
            1
            for row in rows
            if row["kind"] == "receipt_billed"
            and transcript_start <= _day_of(row["date"]) <= transcript_end
        )
        payload["verdict"] = "billed_without_transcripts"
        payload["receipt_billed_days_inside_span"] = inside
        payload["reason"] = _billed_reason(
            payload["days_receipt_billed"],
            payload["receipt_tokens_billed"],
            inside,
            (transcript_start, transcript_end),
        )
    elif payload["days_activity_only"]:
        payload["verdict"] = "activity_without_tokens"
        payload["reason"] = (
            f"{payload['days_activity_only']} day(s) carry message counts in the receipt while "
            "the transcripts hold no token record: those days are not quiet, whatever the "
            "cause of the missing record"
        )
    elif payload["days_tokens_only"]:
        payload["verdict"] = "tokens_without_activity"
        payload["reason"] = (
            f"{payload['days_tokens_only']} day(s) carry tokens in the transcripts while the "
            "receipt's day table holds no activity row: the table does not cover them"
        )
    elif payload["days_silent"]:
        payload["verdict"] = "silent_days"
        payload["reason"] = (
            f"{payload['days_silent']} of {payload['days_in_window']} day(s) hold a record on "
            "neither side; they are printed as absences, which is not the same as zero usage"
        )
    else:
        payload["verdict"] = "all_measured"
        payload["reason"] = (
            "every day in the shared window carries a record on both sides, so no silent day "
            "is left to explain"
        )
    return payload


def _carries_figure(token):
    """True when a token carries a digit, so it keeps its neighbour on the line."""
    return any(ch.isdigit() for ch in token)


def _label_prefix(line, max_words=3):
    """Everything before the first value, by this screen's own convention.

    A label word is followed by one space and the value starts after a gap of
    two or more, so `    reading      937 ...` puts its value in column 17 while
    `  meter check  27 ...` puts it in column 15. A token that carries a figure
    always starts the value, so a number is never absorbed into the label.
    """
    words = [(match.start(), match.group()) for match in re.finditer(r"\S+", line)]
    if not words:
        return ""
    end = words[0][0] + len(words[0][1])
    taken = 1
    for start, token in words[1:]:
        gap = start - end
        if taken >= max_words or gap >= 2 or _carries_figure(token):
            break
        end = start + len(token)
        taken += 1
    rest = line[end:]
    gap = len(rest) - len(rest.lstrip())
    return line[: end + max(gap, 1)]


def _repair_cut(current, word, head, width):
    """Move a mid-clause break back to the clause boundary beside it.

    Greedy filling already breaks at a pipe when the pipe is where the line
    runs out, so a row built from `term | term | term` needs nothing here. The
    damage is a break in the middle of a sentence: `... are not the` /
    `same measure` separates two words that belong together, and the row it
    came from was two clauses with a `--` between them. So when the natural
    break lands mid-clause, the last pipe or dash that still fills at least
    half the screen becomes the break instead. Nothing is moved when there is
    no such separator, since a greedy break is then the only one there is.
    """
    if word in {"|", "--"} or current[-1] == "|":
        return None
    floor = width // 2
    best = None
    for index, token in enumerate(current):
        cut = index + 1 if token == "|" else index if token == "--" else None
        if cut is None or cut < 1 or cut > len(current):
            continue
        if len(head + " ".join(current[:cut])) < floor:
            continue
        best = cut
    return best


def _fold_lines(lines, width=80):
    """Fold any line wider than `width`, keeping the label column.

    The chain wraps its own rows under a label; every other line of the same
    screen was assembled as one f-string and nothing measured it, so on a real
    corpus 22 of the 45 lines `quiet` prints were wider than a terminal (the
    widest 254). A number printed off the right edge is a number the reader
    cannot check, which is why the chain had a width guard and this screen did
    not.

    The label keeps its column and the continuation is indented to where the
    first value started, so a folded row still reads as one row. A break is not
    placed between a token that carries a figure and its neighbour: `silent 138`
    and `2.44 GiB` travel together, because a figure separated from its label or
    its unit is the reading this tool refuses elsewhere.

    Only whitespace moves. The words are the same words in the same order, so
    folding loses nothing; a token longer than the whole screen is printed as it
    is, since truncating it would hide the figure instead.
    """
    folded = []
    for line in lines:
        if len(line) <= width or not line.strip():
            folded.append(line)
            continue
        prefix = _label_prefix(line)
        words = line[len(prefix) :].split()
        if not prefix or not words:
            folded.append(line)
            continue
        pad = " " * len(prefix)
        out = []
        current = [words[0]]
        for word in words[1:]:
            head = prefix if not out else pad
            if len(head + " ".join([*current, word])) <= width:
                current.append(word)
                continue
            cut = _repair_cut(current, word, head, width)
            if cut:
                out.append(head + " ".join(current[:cut]))
                current = [*current[cut:], word]
                continue
            if len(current) >= 2 and (
                _carries_figure(current[-1]) or _carries_figure(word)
            ):
                moved = current.pop()
                out.append(head + " ".join(current))
                head = prefix if not out else pad
                current = [moved]
                if len(head + " ".join([*current, word])) <= width:
                    current.append(word)
                    continue
            out.append(head + " ".join(current))
            current = [word]
        out.append((prefix if not out else pad) + " ".join(current))
        folded.extend(out)
    return folded


def render_quiet(payload):
    lines = ["SILENT DAYS (which days are quiet, and which only look quiet)", ""]
    lines.append(f"  receipt     {_range(*payload['receipt_span']) if payload['receipt_span'] else 'none'}")
    if payload["last_computed"]:
        lines.append(f"  computed    through {payload['last_computed']}")
    if payload["receipt_token_days"]:
        lines.append(
            f"  token meter {payload['receipt_token_days']} day(s) with a per-day total; "
            f"{_fmt(payload['receipt_tokens_in_window'])} in the window"
        )
    else:
        lines.append("  token meter absent -- the receipt states no per-day token total")
    lines.append(
        f"  transcripts {_range(*payload['transcript_span']) if payload['transcript_span'] else 'none'}"
    )
    if payload["window"]:
        lines.append(
            f"  window      {payload['window'][0]} .. {payload['window'][1]} "
            f"({payload['days_in_window']} day(s))"
        )
    else:
        lines.append("  window      none -- the two stores share no day")
    if payload["days_after_receipt"]:
        span = _range(*payload["transcript_tail"])
        lines.append(
            f"  tail        {payload['days_after_receipt']} day(s) after the receipt's "
            f"last day ({span},"
        )
        lines.append(
            f"              {_fmt(payload['transcript_tail_tokens'])} token(s)), "
            "not judged: the window ends there"
        )
    lines.append("")
    # Printed on every path, including the ones that stop before any day is
    # judged: a verdict's claim about a corpus is only readable if the corpus
    # it read is stated, and "no scan in hand" is itself a statement.
    reading = (payload.get("arbitration") or {}).get("reading") or {}
    if reading.get("available"):
        size = reading["bytes"]
        if size >= 1024 ** 3:
            size_text = f"{size / (1024 ** 3):.2f} GiB"
        else:
            size_text = f"{size / (1024 ** 2):.1f} MiB"
        read_of = f"{reading['files_read']} file(s) read"
        if reading["files_skipped"]:
            read_of += f", {reading['files_skipped']} unreadable"
        lines.append(
            f"    reading      {read_of}, {reading['records_parsed']:,} usage record(s), "
            f"{size_text} -- the corpus this verdict read"
        )
        # Two digests, because one of them cannot carry the claim alone: an
        # equal (name, size, mtime) digest is not a statement about the bytes.
        lines.append(
            f"                 digest {reading['digest']} ({reading['digest_basis']})"
        )
        lines.append(
            f"                 content {reading['content_digest']} ({reading['content_basis']})"
        )
        lines.extend(_compare_lines(payload.get("reading_compare")))
        lines.extend(_chain_lines(payload.get("reading_chain")))
    elif reading:
        lines.append(f"    reading      reading not identified -- {reading['reason']}")
    if not payload["available"] or payload["verdict"] in {
        "no_day_table",
        "no_transcripts",
        "no_overlap",
    }:
        lines.append(f"  not measured -- {payload['reason']}")
        lines.append(f"VERDICT: {payload['verdict']}")
        return "\n".join(_fold_lines(lines)) + "\n"

    parts = [
        f"measured {payload['days_measured']}",
        f"activity without tokens {payload['days_activity_only']}",
        f"tokens without activity {payload['days_tokens_only']}",
        f"silent {payload['days_silent']}",
    ]
    if payload["days_receipt_billed"]:
        # A fifth term, so the four printed states still add up to the window.
        parts.insert(1, f"receipt bills, transcripts silent {payload['days_receipt_billed']}")
    lines.append("  DAYS: " + " | ".join(parts))
    lines.append(
        f"  totals      receipt {_fmt(payload['receipt_tokens_in_window'])} | "
        f"transcripts {_fmt(payload['transcript_tokens_in_window'])} "
        "-- the two sides are not the same measure"
    )
    block = payload.get("arbitration") or {}
    if block.get("available"):
        # The pair of totals above invites a subtraction, so the difference is
        # measured here instead of being left to the reader.
        head = (
            f"  meter check  {block['days_compared']} day(s) stated by both token meters "
            f"(>= {block['min_tokens']:,}, tolerance {block['tolerance']:.0%})"
        )
        if block["days_below_floor"]:
            head += f" | {block['days_below_floor']} day(s) below the floor do not vote"
        lines.append(head)
        lines.append(f"    receipt                 {_fmt(block['receipt_tokens'])}")
        lines.append(
            f"    transcripts as written  {_fmt(block['transcript_record_sum'])}  "
            "(every usage record)"
        )
        lines.append(
            f"    transcripts deduped     {_fmt(block['transcript_deduped'])}  "
            "(one message counted once)"
        )
        ratios = []
        if block.get("ratio_record_sum") is not None:
            ratios.append(f"receipt / as written {block['ratio_record_sum']:.2f}x")
        if block.get("ratio_deduped") is not None:
            ratios.append(f"receipt / deduped {block['ratio_deduped']:.2f}x")
        if ratios:
            lines.append("    " + " | ".join(ratios))
        if block.get("days_matching_individually") is not None:
            # The aggregate can match while no single day does, so the pairs are
            # printed beside it rather than implied by it.
            lines.append(
                f"    per day      {block['days_matching_individually']} of "
                f"{block['days_compared']} day(s) agree with their own pair "
                f"(tolerance {block['tolerance']:.0%}) | "
                f"{block['days_receipt_heavy']} receipt-heavy, "
                f"{block['days_transcript_heavy']} transcript-heavy | "
                f"widest {block['max_day_ratio']:.2f}x ({block['max_day']}), narrowest "
                f"{block['min_day_ratio']:.2f}x ({block['min_day']}) -- {block['per_day_verdict']}"
            )
        if block.get("by_model_rows"):
            # Both axes above sum every model together, so a gap in one model
            # and an opposite gap in another cancel there. This line splits the
            # same days by name and says how much of the excess sits in one.
            axis = (
                f"    by model     {block['models_agreeing']} of {block['models_compared']} "
                f"model(s) agree | {block['models_receipt_heavy']} receipt-heavy, "
                f"{block['models_transcript_heavy']} transcript-heavy"
            )
            if block.get("model_ratio_min") is not None:
                # One constant factor and a range of factors are different
                # findings; the counts alone cannot tell them apart.
                axis += (
                    f" | ratio {block['model_ratio_min']:.2f}x..{block['model_ratio_max']:.2f}x"
                )
            if block.get("largest_gap_model"):
                axis += (
                    f" | largest gap {block['largest_gap_model']} "
                    f"({block['largest_gap_share']:.0%} of the excess)"
                )
            axis += f" -- {block['by_model_verdict']}"
            lines.append(axis)
        if block.get("models_receipt_only") or block.get("models_transcript_only"):
            sides = []
            if block.get("models_receipt_only"):
                sides.append(
                    "receipt only: " + ", ".join(block["models_receipt_only"][:4])
                )
            if block.get("models_transcript_only"):
                sides.append(
                    "transcripts only: " + ", ".join(block["models_transcript_only"][:4])
                )
            lines.append(
                "    one side only " + " | ".join(sides)
                + " -- named by one store, counted by neither"
            )
        witness = block.get("witness") or {}
        if witness.get("available"):
            # The ratios above divide by the records that survive, not by the
            # records the meter read. Until that difference is stated, a total
            # ratio reads as a meter factor.
            day_parts = [f"{witness['days']['exact']} day(s) exact"]
            if witness["days"]["remnant"]:
                day_parts.append(
                    f"{witness['days']['remnant']} short ({_fmt(witness['tokens']['remnant'])} "
                    f"billed, {_fmt(witness['residual_tokens'])} missing)"
                )
            if witness["days"]["absent"]:
                day_parts.append(
                    f"{witness['days']['absent']} absent "
                    f"({_fmt(witness['tokens']['absent'])} billed, no record)"
                )
            if witness["days"]["over"]:
                day_parts.append(
                    f"{witness['days']['over']} heavier in the corpus "
                    f"({_fmt(witness['tokens']['over'])} billed)"
                )
            if witness["days"]["unmeasured"]:
                day_parts.append(f"{witness['days']['unmeasured']} with no record sum")
            line = (
                f"    witness      {' | '.join(day_parts)} -- checked "
                f"{witness['checked_share']:.1%} of {_fmt(witness['receipt_total'])} billed "
                "tokens"
                + (
                    f" above the {witness['min_tokens']:,}-token floor "
                    f"({witness['days_below_floor']} day(s) holding "
                    f"{_fmt(witness['tokens_below_floor'])} token(s) are below it)"
                    if witness.get("days_below_floor")
                    else ""
                )
                + f"; {witness['verdict'].replace('_', ' ')}"
            )
            if witness.get("worst_day"):
                line += (
                    f"; widest short day {witness['worst_day']} "
                    f"{witness['worst_day_ratio']:.2f}x"
                )
            lines.append(line)
            replay = block.get("replay") or {}
            if replay.get("available"):
                # Only witnessed days enter this share: on a day with missing
                # records a dedupe measures the loss, not the repeats.
                lines.append(
                    f"    repeat       {_fmt(replay['replay_tokens'])} of "
                    f"{_fmt(replay['written_tokens'])} token(s) written on "
                    f"{replay['days']} day(s) are the same message written again "
                    f"({replay['replay_share']:.1%}); deduped {_fmt(replay['deduped_tokens'])}, "
                    f"{replay['days_excluded']} day(s) kept out of this share"
                )
            elif replay:
                lines.append(f"    repeat       repeat share not measured -- {replay['reason']}")
            # A short day's own records still dedupe. That rate answers a
            # different question than the share above - it divides by the
            # records present, not by the bill - so it gets its own line and
            # never enters the number above.
            short = (replay or {}).get("short_days") or {}
            if short.get("days"):
                if short.get("share") is None:
                    lines.append(
                        f"    repeat/short not measured on {short['days']} short day(s) -- "
                        f"{short.get('reason')}"
                    )
                else:
                    lines.append(
                        f"    repeat/short {_fmt(short['replay_tokens'])} of the "
                        f"{_fmt(short['written_tokens'])} token(s) {short['days']} short day(s) "
                        f"hold dedupe away ({short['share']:.1%}) -- a rate over the records "
                        f"present, not a share of the {_fmt(short['billed_tokens'])} they bill"
                    )

    lines.append(f"  meter check: {block.get('verdict')} -- {block.get('reason')}")
    lines.append(
        f"  outside     {payload['days_outside_window']} day(s) are reached by only one store "
        "- not judged here"
    )
    gaps = [row for row in payload["rows"] if row["kind"] != "silent"]
    if gaps:
        lines.append("")
        lines.append("  one side holds a record, the other does not:")
        for row in gaps:
            if row["kind"] == "receipt_billed":
                head = (
                    f"{_fmt(row['messages'])} message(s)"
                    if row["messages"] is not None
                    else "no activity row"
                )
                # Saying this in one line is the point, not a style choice: a
                # row that only fits by breaking after "token(s)" leaves the
                # date on one line and the absence on the next, and the reader
                # has to pair them up. "billed" carries the receipt side, so the
                # verb does the work the dropped words used to do.
                lines.append(
                    f"    {row['date']}  {head}  {_fmt(row['receipt_tokens'])} "
                    "token(s) billed, no transcript"
                )
            elif row["kind"] == "activity_only":
                lines.append(
                    f"    {row['date']}  {_fmt(row['messages'])} message(s)  no token record"
                )
            else:
                lines.append(
                    f"    {row['date']}  no activity row  {_fmt(row['tokens'])} token(s)"
                )
    silents = [row["date"] for row in payload["rows"] if row["kind"] == "silent"]
    if silents:
        lines.append("")
        lines.append(
            f"  silent {len(silents)} day(s): {_range(silents[0], silents[-1])} "
            "-- no record on either side, printed as an absence and not as zero"
        )
    lines.append("")
    lines.append(f"VERDICT: {payload['verdict']}")
    lines.append(f"  {payload['reason']}")
    return "\n".join(_fold_lines(lines)) + "\n"


RECENT_DAYS_DEFAULT = 7


def recent_window(root, days=RECENT_DAYS_DEFAULT, until=None, max_files=None):
    """Tokens inside a declared trailing window, placed by a recorded day.

    Every other command measures the whole store (`nested`), the whole receipt
    (`scan`) or a pair of windows (`compare`). The question a quota asks is
    narrower - "the last N days" - and it has three ways to be answered wrongly:

    * a window placed by the clock. If the newest record is three weeks old, a
      "last 7 days" window computed from today is empty, and an empty window
      reads as zero usage. Here the anchor is the newest *recorded* day unless
      `--until` supplies one, and the payload says which it was.
    * a total that hides the days it does not cover. A day inside the window
      with no record makes the sum a floor; the count of those days is printed
      with the total, not left to be inferred from a coverage line elsewhere.
    * records with no timestamp. They belong to no window, so they are counted
      as tokens *outside* it instead of being dropped from a total the reader
      is told is complete.

    The merge rule is `scan_usage`'s, so this window and `nested`'s lifetime
    total cannot disagree about the same corpus. Nothing is written.
    """
    if days is None or int(days) < 1:
        raise ValueError("--days must be at least 1")
    days = int(days)
    given_anchor = date.fromisoformat(until) if until is not None else None

    scan = scan_usage(root, max_files=max_files)
    observation = _observation(scan["files"], scan["records"])
    payload = {
        "root": scan["root"],
        "files": len(scan["files"]),
        "files_skipped": scan["files_skipped"] + scan["skipped_lines"],
        "records": scan["records"],
        "messages": scan["messages"],
        "observation": observation,
        "days_requested": days,
        "dedupe": scan["dedupe"],
        "day_attribution": scan["day_attribution"],
    }

    if observation["verdict"] != "usage_observed":
        # Zero files read, or files that carried no usage: no day can place a
        # window, and a total here would be a claim about nothing read.
        payload.update(
            {
                "verdict": "unobserved",
                "reason": observation["verdict"],
                "window": None,
                "anchor": None,
                "total_tokens": None,
                "tokens": None,
                "rows": [],
                "by_day": [],
                "share_basis": None,
                "raw_record_sum": None,
                "days_with_records": None,
                "missing_days": [],
                "missing_count": None,
                "floor": None,
                "outside_window": None,
            }
        )
        return payload

    day_tables = scan["days"]
    if given_anchor is not None:
        last = given_anchor
        source = "given"
    else:
        last = max(date.fromisoformat(day) for day in day_tables)
        source = "record"
    first = last - timedelta(days=days - 1)

    tokens = {bucket: 0 for bucket in CLAUDE_TOKEN_BUCKETS}
    by_model = {}
    non_cache_by_model = {}
    raw_record_sum = 0
    days_with_records = 0
    missing = []
    by_day = []
    for offset in range(days):
        day = (first + timedelta(days=offset)).isoformat()
        table = day_tables.get(day)
        if table is None:
            missing.append(day)
            # No record is not a zero: the row says the day is uncovered, and
            # carries no number a reader could mistake for a measurement.
            by_day.append({"day": day, "recorded": False, "tokens": None})
            continue
        days_with_records += 1
        by_day.append({"day": day, "recorded": True, "tokens": table["total_tokens"]})
        raw_record_sum += table["naive_tokens"]
        for bucket in CLAUDE_TOKEN_BUCKETS:
            tokens[bucket] += table["by_bucket"][bucket]
        for model, per_model in table["by_model"].items():
            by_model[model] = by_model.get(model, 0) + per_model["total_tokens"]
            # the share is taken over non-cache-read tokens, so the numerator must
            # use the same basis as the denominator or the percentages do not sum
            non_cache_by_model[model] = non_cache_by_model.get(model, 0) + sum(
                per_model["by_bucket"][bucket] for bucket in NON_CACHE_BUCKETS
            )

    tokens_before = 0
    days_before = 0
    tokens_after = 0
    days_after = 0
    for day, table in day_tables.items():
        if day < first.isoformat():
            days_before += 1
            tokens_before += table["total_tokens"]
        elif day > last.isoformat():
            # `--until` can put real days on the far side of the window; they are
            # outside it exactly like the days before, and must be counted there
            days_after += 1
            tokens_after += table["total_tokens"]

    # A window no recorded day falls into has no total: printing 0 there would be
    # the exact reading this command exists to prevent (absence read as zero).
    measured = bool(days_with_records)
    total = sum(tokens.values()) if measured else None
    share_total = sum(tokens[bucket] for bucket in NON_CACHE_BUCKETS) if measured else 0
    rows = []
    for model, model_tokens in sorted(by_model.items(), key=lambda item: (-item[1], item[0])):
        rows.append(
            {
                "model": model,
                "total_tokens": model_tokens,
                "share_tokens": non_cache_by_model.get(model, 0),
                "share_pct": round(non_cache_by_model.get(model, 0) * 100.0 / share_total, 1)
                if share_total
                else None,
                "of_tokens": share_total,
            }
        )

    from_outside = scan["unattributed_tokens"]
    payload.update(
        {
            "verdict": "window_measured" if days_with_records else "no_records_in_window",
            "reason": None,
            "anchor": {"day": last.isoformat(), "source": source},
            "window": {
                "first": first.isoformat(),
                "last": last.isoformat(),
                "days": days,
                "basis": "record_timestamp",
            },
            "total_tokens": total,
            "tokens": tokens if measured else None,
            "rows": rows,
            "by_day": by_day,
            "share_basis": {"name": "non_cache_read_tokens", "tokens": share_total}
            if measured
            else None,
            "raw_record_sum": raw_record_sum if measured else None,
            "days_with_records": days_with_records,
            "missing_days": missing,
            "missing_count": len(missing),
            "floor": bool(missing) and bool(days_with_records),
            "outside_window": {
                "records_without_timestamp": scan["records_without_timestamp"],
                "tokens_without_timestamp": from_outside,
                "days_before_window": days_before,
                "tokens_before_window": tokens_before,
                "days_after_window": days_after,
                "tokens_after_window": tokens_after,
                "tokens": from_outside + tokens_before + tokens_after,
            },
        }
    )
    return payload


def _bucket_text(tokens):
    labels = {
        "input_tokens": "input",
        "output_tokens": "output",
        "cache_creation_input_tokens": "cache_creation",
        "cache_read_input_tokens": "cache_read",
    }
    return ", ".join(f"{labels[bucket]} {tokens[bucket]:,}" for bucket in CLAUDE_TOKEN_BUCKETS)


def _recent_token_lines(payload):
    """The model table, buckets and the repeated-write note for a measured window."""
    out = ["TOKENS IN THE WINDOW (deduped by message id, larger value per bucket)"]
    if payload["rows"]:
        width = max([len(row["model"]) for row in payload["rows"]] + [len("TOTAL")])
        for row in payload["rows"]:
            share = (
                f"{row['share_pct']:5.1f}% of {row['of_tokens']:,} non_cache_read_tokens"
                if row["share_pct"] is not None
                else "share not measured -- the share basis is zero"
            )
            out.append(f"  {row['model']:<{width}}  {row['total_tokens']:>16,}  {share}")
        out.append(f"  {'TOTAL':<{width}}  {payload['total_tokens']:>16,}")
    else:
        out.append(f"  {'TOTAL':<8}  {payload['total_tokens']:>16,}")
    out.append(f"  buckets: {_bucket_text(payload['tokens'])}")
    if payload["tokens"]["cache_read_input_tokens"] and payload["share_basis"]["tokens"]:
        read_share = payload["tokens"]["cache_read_input_tokens"] * 100.0 / (
            payload["share_basis"]["tokens"] + payload["tokens"]["cache_read_input_tokens"]
        )
        out.append(
            f"  cache_read is outside the share basis: {payload['tokens']['cache_read_input_tokens']:,} "
            f"= {read_share:.1f}% of this window's tokens and would drown every other bucket"
        )
    if payload["raw_record_sum"] and payload["raw_record_sum"] != payload["total_tokens"]:
        extra = payload["raw_record_sum"] - payload["total_tokens"]
        pct = extra * 100.0 / payload["total_tokens"] if payload["total_tokens"] else None
        line = f"  raw record sum: {payload['raw_record_sum']:,} ({extra:,} above the deduped total"
        line += f" = {pct:.1f}%" if pct is not None else ""
        out.append(
            line + "; repeated writes of one message, so neither is interchangeable with the other)"
        )
    return out


def _recent_by_day_lines(payload):
    """One row per day in the window, so the floor is auditable instead of described.

    A day no record fell on prints `no record`. It never prints 0: this is the
    same rule the empty-store path follows, applied per day. The rows are the
    window's own day tables, so their sum is the printed total by construction.
    """
    out = [""]
    out.append("BY DAY (one row per day in the window; a blank day is an absence, never a zero)")
    recorded = [row for row in payload["by_day"] if row["recorded"]]
    for row in payload["by_day"]:
        if row["recorded"]:
            out.append(f"  {row['day']}  {row['tokens']:>16,}")
        else:
            out.append(f"  {row['day']}  {'no record':>16}")
    if payload["total_tokens"] is None:
        out.append("  no row carries a number, so there is no sum to take")
    else:
        row_sum = sum(row["tokens"] for row in recorded)
        out.append(
            f"  rows sum to {row_sum:,} = TOTAL ({len(payload['by_day'])} day(s), "
            f"{payload['missing_count']} without a record)"
        )
        out.append(
            "  A blank row is an absence of records, not a measured quiet day: this store only holds "
            "what it wrote,"
        )
        out.append("  so a day it did not write and a day nothing happened look the same in it.")
    return out


def render_recent_window(payload, by_day=False):
    """The printed form: what was read, where the window sits, and what it excludes."""
    out = ["RECENT WINDOW  (tokens inside a declared trailing window)"]
    out.append(f"  source        : {payload.get('source') or payload.get('root')}")
    dedupe = ""
    if payload["messages"] is not None:
        dedupe = f" -> {payload['messages']:,} after dedupe by message id"
    records = payload["records"] if payload["records"] is not None else 0
    out.append(f"  transcripts   : {payload['files']} file(s), {records:,} usage record(s){dedupe}")
    out.append(_observation_line(payload["observation"]))

    if payload["window"] is None:
        out.append(
            f"  window        : not measured ({payload['reason']} -- no usage record was read, "
            "so no day can place a window)"
        )
        out.append("")
        out.append(f"VERDICT: {payload['verdict']}")
        out.append(
            "  An empty store is an absence of observation, not an observation of zero usage:"
        )
        out.append("  nothing here says what was used in the last few days, and no total is printed.")
        return "\n".join(out) + "\n"

    window = payload["window"]
    anchor = payload["anchor"]
    out.append(f"  window        : {window['first']}..{window['last']} ({window['days']} day(s), basis={window['basis']})")
    if anchor["source"] == "given":
        out.append(
            f"  anchor        : {anchor['day']} (given, not measured -- no record has to exist on this day)"
        )
    else:
        out.append(
            f"  anchor        : last recorded day {anchor['day']} (measured from the records this run read)"
        )
    out.append("")
    if payload["total_tokens"] is None:
        out.append(
            "  total         : not measured -- no day in the window carries a record, so there is no sum"
        )
    else:
        out.extend(_recent_token_lines(payload))
    if by_day:
        out.extend(_recent_by_day_lines(payload))

    out.append("")
    out.append("COVERAGE")
    if payload["floor"]:
        out.append(
            f"  {payload['days_with_records']} of {payload['days_requested']} day(s) in the window carry a "
            f"record; the total is a floor for those {payload['days_requested']} days, not a full-window measurement"
        )
        shown = payload["missing_days"][:8]
        more = payload["missing_count"] - len(shown)
        line = f"  no record: {', '.join(shown)}"
        line += f" +{more} more" if more > 0 else ""
        out.append(line)
    elif payload["verdict"] == "no_records_in_window":
        out.append(
            "  no record falls inside the window, so nothing here is a measurement of it -- not even a zero"
        )
    else:
        out.append(
            f"  every one of {payload['days_requested']} day(s) in the window carries a record, so the total "
            "covers the window without a gap"
        )
    outside = payload["outside_window"]
    out.append(
        f"  {outside['tokens']:,} token(s) are outside the window: {outside['tokens_without_timestamp']:,} in "
        f"{outside['records_without_timestamp']} record(s) with no timestamp, {outside['tokens_before_window']:,} "
        f"on {outside['days_before_window']} day(s) before it, {outside['tokens_after_window']:,} on "
        f"{outside['days_after_window']} day(s) after it"
    )
    out.append("")
    verdict = payload["verdict"] + (" (floor)" if payload["floor"] else "")
    out.append(f"VERDICT: {verdict}")
    if payload["floor"]:
        out.append(
            "  Days with no record are counted, not assumed quiet: the meter may not have written them, "
            "and this command cannot tell a quiet day from a missing one."
        )
    elif payload["verdict"] == "no_records_in_window":
        out.append(
            "  No day in the window carries a record, so no total is printed: an absence of records is "
            "not an observation of zero usage."
        )
    else:
        out.append("  Every day in the window carries a recorded total; nothing here is interpolated.")
    out.append("")
    out.append(
        "LIMIT: the window is placed by the newest recorded day, not by the clock, so a store whose newest"
    )
    out.append(
        "record is three weeks old reports three weeks ago instead of an empty last week."
    )
    out.append(
        "LIMIT: no price and no quota ceiling is assumed -- this is a token count inside a named window,"
    )
    out.append("not a fraction of a plan allowance.")
    return "\n".join(out) + "\n"


DEFAULT_MIN_TOKENS = 1_000_000
DEFAULT_TOLERANCE = 0.02


def _floor(min_tokens, tolerance):
    """The two arguments that decide a day's verdict, with the defaults made explicit.

    They are printed beside every verdict, so they must also be settable: a floor
    a reader cannot change is a verdict a reader cannot re-run.
    """
    floor = DEFAULT_MIN_TOKENS if min_tokens is None else int(min_tokens)
    tol = DEFAULT_TOLERANCE if tolerance is None else float(tolerance)
    if floor < 0:
        raise ValueError("min_tokens cannot be negative")
    if not 0 <= tol <= 1:
        raise ValueError("tolerance is a fraction of the target, so it must be in [0, 1]")
    return floor, tol


def _undecided_arbitration(floor, tol, reason):
    return {
        "available": False,
        "verdict": "undecided",
        "coverage": "not_read",
        "reason": reason,
        "days": [],
        "days_compared": 0,
        "days_below_floor": 0,
        "days_cache_only": 0,
        "days_transcript_only": 0,
        "days_matching_record_sum": 0,
        "days_matching_merged": 0,
        "days_matching_neither": 0,
        "cache_tokens": 0,
        "merged_tokens": 0,
        "naive_tokens": 0,
        "worst_day": None,
        "min_tokens": floor,
        "tolerance": tol,
    }


def _tilde(path):
    """Help text is copied into issues and screenshots; keep the home directory out of it."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if home and text.startswith(home) else text


def _add_path_arg(parser, default, what):
    """`--path` says which file it opens, and which one it opens when omitted.

    The default is not the same for every command - the stats cache for the
    receipt commands, the projects directory for the transcript commands - so a
    bare `--path PATH` left the reader guessing which of the two they were about
    to hand to the tool.
    """
    parser.add_argument("--path", default=str(default), help=f"{what} (default {_tilde(default)})")


def _add_floor_args(parser):
    """The two numbers that decide a day's verdict, exposed so a reader can re-run it."""
    parser.add_argument(
        "--min-tokens",
        type=int,
        default=None,
        help=(
            "only days at or above this token count may decide the day cross-check "
            f"(default {DEFAULT_MIN_TOKENS:,})"
        ),
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help=(
            "a day counts as a match when the two numbers are within this fraction of each other "
            # argparse formats help with `%` substitution, so a literal percent is `%%`;
            # writing `2%` here crashed `--help` on the two commands that take this floor.
            f"(default {DEFAULT_TOLERANCE} = {round(DEFAULT_TOLERANCE * 100)}%%)"
        ),
    )


def nested_audit(root, max_files=None, cache=None, min_tokens=None, tolerance=None):
    """Totals split by scope, each one stamped, over Claude Code transcripts.

    Every total declares whether it includes descendant sessions and whether
    it is a delta or a cumulative counter.

    Repeated writes of one `message.id` can change the answer, so records are
    merged by id while the raw sum is printed beside the merged total.

    A message seen in both scopes counts as root (a copy in a subagent file is
    not evidence that the work happened there) and the disagreement is counted
    rather than silently resolved.
    """
    scan = scan_usage(root, max_files=max_files)
    files = scan["files"]
    raw_records = scan["records"]
    raw_tokens = scan["naive_tokens"]
    skipped = scan["files_skipped"] + scan["skipped_lines"]
    timed = scan["records_timed"]
    untimed = scan["records_without_timestamp"]
    order = scan["order"]
    merged = scan["entries"]
    conflicted = {key for key in order if len(merged[key]["scopes"]) > 1}

    scopes = {
        "all": "root+sidechain",
        "root": "root",
        "sidechain": "sidechain",
    }
    totals = {}
    for scope, includes in scopes.items():
        tokens = {bucket: 0 for bucket in CLAUDE_TOKEN_BUCKETS}
        messages = 0
        days = set()
        for key in order:
            entry = merged[key]
            is_sidechain = entry["scopes"] == {"sidechain"}
            if scope == "root" and is_sidechain:
                continue
            if scope == "sidechain" and not is_sidechain:
                continue
            messages += 1
            days |= entry["days"]
            for bucket in CLAUDE_TOKEN_BUCKETS:
                tokens[bucket] += entry["tokens"][bucket]
        totals[scope] = {
            "includes_descendants": includes,
            "accounting": "delta",
            "dedupe": "message_id_max",
            "messages": messages,
            "tokens": tokens,
            "total_tokens": sum(tokens.values()),
            "window": _window(days, "record_timestamp"),
        }
        if scope == "all":
            transcript_days = days

    deduped_tokens = totals["all"]["total_tokens"]
    observation = _observation(files, raw_records)
    sidechain_messages = totals["sidechain"]["messages"]
    share_denominator = sum(totals["all"]["tokens"][b] for b in NON_CACHE_BUCKETS)
    share_numerator = sum(totals["sidechain"]["tokens"][b] for b in NON_CACHE_BUCKETS)
    messages = totals["all"]["messages"]

    if deduped_tokens == 0:
        share = {
            "available": False,
            "reason": "no usage-bearing message was read, so no denominator exists",
            "basis": "non_cache_read_tokens",
            "numerator": 0,
            "denominator": 0,
            "pct": None,
            "sidechain_messages": 0,
            "messages": 0,
            "messages_pct": None,
        }
        verdict = "unobserved" if observation["verdict"] == "store_empty" else "no_usage"
    else:
        share = {
            "available": True,
            "reason": None,
            "basis": "non_cache_read_tokens",
            "numerator": share_numerator,
            "denominator": share_denominator,
            "pct": round(100 * share_numerator / share_denominator, 1) if share_denominator else None,
            "sidechain_messages": sidechain_messages,
            "messages": messages,
            "messages_pct": round(100 * sidechain_messages / messages, 1) if messages else None,
        }
        verdict = "nested_confirmed" if sidechain_messages else "no_sidechain"

    cache_tokens = totals["all"]["tokens"]["cache_read_input_tokens"]
    inflation_pct = (
        round(100 * (raw_tokens - deduped_tokens) / deduped_tokens, 1) if deduped_tokens else None
    )
    notes = [
        "cache_read_input_tokens is excluded from the share basis: it is "
        f"{round(100 * cache_tokens / deduped_tokens, 1) if deduped_tokens else 0}% of the deduped total "
        "and would drown every other bucket",
        "records are merged by message id keeping the larger value per bucket "
        "(streaming partial writes repeat a usage); the raw record sum is printed "
        "beside the deduped total rather than discarded",
    ]
    if conflicted:
        notes.append(
            f"{len(conflicted)} message id(s) appear in both scopes; each is counted once, as root, "
            "and the disagreement is printed instead of being resolved silently"
        )

    coverage = {
        "records_timed": timed,
        "records_without_timestamp": untimed,
        "note": _coverage_note(raw_records, untimed),
    }
    cache_days = set()
    cache_day_totals = {}
    cache_window = None
    if cache is not None:
        _cache_by_model, cache_day_totals = _daily_totals(cache)
        cache_days = set(cache_day_totals)
        cache_window = _window(cache_days, "declared_attribution")
    if cache_window is None:
        comparability = {
            "same_window": "unknown",
            "note": (
                "the cache's day table was not read, so only one of the two windows on this machine "
                "is known; pass --cache to place them side by side"
            ),
        }
        days_cache_only = []
    else:
        comparability = _comparability({"window": totals["all"]["window"]}, {"window": cache_window})
        days_cache_only = sorted(cache_days - transcript_days)

    floor, tol = _floor(min_tokens, tolerance)
    if cache is None:
        arbitration = _undecided_arbitration(
            floor,
            tol,
            "no cache day table was read, so nothing on this machine says which of the two totals "
            "is the bill; pass --cache PATH and the verdict is computed instead of assumed",
        )
    else:
        arbitration = crosscheck_days(
            cache_day_totals, {"days": scan["days"]}, min_tokens=floor, tolerance=tol
        )

    windows = {
        "transcript": _window(transcript_days, "record_timestamp"),
        "cache": cache_window,
        "comparability": comparability,
        "days_transcript_only": sorted(transcript_days - cache_days),
        "days_cache_only": days_cache_only,
    }

    return {
        "files": len(files),
        "records_raw": raw_records,
        "observation": observation,
        "coverage": coverage,
        "windows": windows,
        "records_deduped": len(order),
        "dedupe": "message_id_max",
        "accounting": "delta",
        "scope_conflict": len(conflicted),
        "inflation": {
            "raw_tokens": raw_tokens,
            "deduped_tokens": deduped_tokens,
            "duplicate_tokens": raw_tokens - deduped_tokens,
            "pct": inflation_pct,
            "days_with_duplicates": sum(
                1 for day in scan["days"].values() if day["duplicate_tokens"] > 0
            ),
            "worst_day": _worst_duplicate_day(scan["days"]),
            "arbitration": arbitration,
        },
        "totals": totals,
        "share": share,
        "cache_read": {
            "tokens": cache_tokens,
            "share_of_total_pct": (
                round(100 * cache_tokens / deduped_tokens, 1) if deduped_tokens else None
            ),
        },
        "verdict": verdict,
        "notes": notes,
        "skipped_lines": skipped,
        "rule": (
            "a total must declare includes_descendants (root / sidechain / root+sidechain), "
            "accounting (delta / cumulative) and a window with its basis (record_timestamp / "
            "declared_attribution); a share must print the count it was taken over"
        ),
    }


def _worst_duplicate_day(days):
    """The day where summing records would have overstated the bill most.

    A corpus-wide percentage says the repeats exist; naming the day is what lets
    someone open the file and check.
    """
    if not days:
        return None
    day = max(days, key=lambda name: days[name]["duplicate_tokens"])
    table = days[day]
    if table["duplicate_tokens"] <= 0:
        return None
    return {
        "day": day,
        "naive_tokens": table["naive_tokens"],
        "merged_tokens": table["total_tokens"],
        "duplicate_tokens": table["duplicate_tokens"],
        "records": table["records"],
    }


def _messages(count):
    return f"{count:,} message" if count == 1 else f"{count:,} messages"


def _window_text(window):
    """A window on one line, basis included: a range without its provenance is half a fact."""
    span = f"{window['first']}..{window['last']}" if window["first"] else "undeclared"
    return f"{span} ({window['days']} day(s), basis={window['basis']})"


def render_nested(payload):
    lines = []
    lines.append("NESTED SPEND  (a total states what it includes and how it was counted)")
    lines.append(
        f"  transcripts   : {payload['files']} file(s), {payload['records_raw']:,} usage record(s) "
        f"-> {payload['records_deduped']:,} after dedupe by message id"
    )
    observation = payload["observation"]
    lines.append(_observation_line(observation, NOT_READ))
    lines.append("  totals")
    for scope in ("all", "root", "sidechain"):
        total = payload["totals"][scope]
        lines.append(
            f"    {total['includes_descendants']:<15}: {total['total_tokens']:,} tokens over "
            f"{_messages(total['messages'])}   [{_stamp_text(total)}]"
        )
    share = payload["share"]
    if share["available"] is False:
        lines.append(f"  sidechain share: not printed - {share['reason']}")
    else:
        lines.append(
            f"  sidechain share: {share['pct']}% of {share['denominator']:,} "
            f"{share['basis']} over {_messages(share['messages'])} "
            f"({share['sidechain_messages']:,} of {share['messages']:,} are sidechain messages, "
            f"{share['messages_pct']}%)"
        )
    inflation = payload["inflation"]
    if inflation["pct"]:
        lines.append(
            f"  raw record sum: {inflation['raw_tokens']:,} tokens, "
            f"{inflation['pct']}% above the deduped total = "
            f"{round(1 + inflation['pct'] / 100, 2)}x it; the two numbers differ only by repeated "
            "writes of one message, so neither is interchangeable with the other"
        )
    arbitration = inflation.get("arbitration") or {}
    if arbitration:
        if arbitration.get("verdict") == "undecided":
            lines.append(f"  which total is the bill: unsettled -- {arbitration['reason']}")
        else:
            lines.append(
                f"  which total is the bill: {arbitration['verdict']} over "
                f"{arbitration['days_compared']} comparable day(s) of at least "
                f"{arbitration['min_tokens']:,} tokens (within "
                f"{round(arbitration['tolerance'] * 100)}% counts as a match) -- "
                f"{arbitration['reason']}"
            )
    worst = inflation.get("worst_day")
    if worst:
        lines.append(
            f"  most duplicate records on one day: {worst['day']} - "
            f"{worst['duplicate_tokens']:,} tokens of a {worst['naive_tokens']:,} record sum "
            f"({worst['merged_tokens']:,} merged)"
        )
    lines.append(
        f"  cache_read    : {payload['cache_read']['tokens']:,} tokens = "
        f"{payload['cache_read']['share_of_total_pct']}% of the deduped total, outside the share basis"
    )
    if payload["skipped_lines"]:
        lines.append(f"  {payload['skipped_lines']} unparseable line(s) counted, not fatal")
    windows = payload["windows"]
    lines.append("")
    lines.append("WINDOWS  (one quantity, two windows on this machine)")
    lines.append(f"  nested  (measured from record timestamps): {_window_text(windows['transcript'])}")
    if windows["cache"] is None:
        lines.append("  compare (declared by the cache's day table): not read -- pass --cache PATH to place both on one line")
        lines.append(f"  -> {windows['comparability']['note']}")
    else:
        lines.append(f"  compare (declared by the cache's day table): {_window_text(windows['cache'])}")
        lines.append(f"  -> {windows['comparability']['note']}")
        if windows["days_transcript_only"]:
            lines.append(
                f"     {len(windows['days_transcript_only'])} day(s) carry transcript usage the cache's day "
                f"table does not list: {_sample(windows['days_transcript_only'], 8)}"
            )
        if windows["days_cache_only"]:
            lines.append(
                f"     {len(windows['days_cache_only'])} day(s) are listed by the cache with no transcript here: "
                f"{_sample(windows['days_cache_only'], 8)}"
            )
        lines.append(
            "     `quotamirror compare` prints the second of these two windows; this command measures the "
            "first, so neither is an unexplained \"claude total\""
        )
    lines.append("")
    lines.append("COVERAGE")
    lines.append(f"  {payload['coverage']['note']}")
    lines.append("")
    lines.append("")
    lines.append("RULE")
    lines.append(f"  {payload['rule']}")
    for note in payload["notes"]:
        lines.append(f"  {note}")
    lines.append(f"  verdict: {payload['verdict']}")
    return "\n".join(lines) + "\n"


def load_cache(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    if not raw.strip():
        raise ValueError(
            f"{path} is empty ({len(raw)} bytes) - a receipt that was never written, "
            "or one that is being rewritten right now"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path} is not UTF-8 text (byte 0x{raw[exc.start]:02x} at offset {exc.start}, "
            f"{len(raw)} bytes read)"
        ) from None
    try:
        cache = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} is not valid JSON ({exc.msg} at line {exc.lineno} column {exc.colno}, "
            f"{len(raw)} bytes read) - a receipt caught mid-write looks like this, and "
            "nothing is claimed about the tokens it would have held"
        ) from None
    if not isinstance(cache, dict) or not any(k in cache for k in KNOWN_KEYS):
        raise ValueError(f"{path} does not look like a usage cache (no {', '.join(KNOWN_KEYS)})")
    models = cache.get("modelUsage")
    if models is not None:
        if not isinstance(models, dict):
            raise ValueError(f"{path}: modelUsage is not an object")
        for name, entry in models.items():
            if not isinstance(entry, dict):
                raise ValueError(f"{path}: modelUsage[{name!r}] is not an object")
    return cache


DEFAULT_CACHE = Path.home() / ".claude" / "stats-cache.json"


def start_report(path):
    """What this machine has, before any command is chosen.

    A bare invocation used to be a parser error: seven command names and no
    statement about the receipt sitting at the default path. This reads that
    path once and reports only what the file itself says - an absent file is
    "nothing", never a zero.
    """
    path = Path(path)
    payload = {
        "receipt": str(path),
        "readable": False,
        "models": None,
        "days": None,
        "as_of": None,
        "reason": None,
    }
    try:
        cache = load_cache(str(path))
    except OSError as exc:
        payload["reason"] = exc.strerror or str(exc)
        return payload
    except (json.JSONDecodeError, ValueError) as exc:
        reason = str(exc)
        for prefix in (f"{path}: ", f"{path} "):
            if reason.startswith(prefix):
                reason = reason[len(prefix):]
                break
        payload["reason"] = reason
        return payload
    payload["readable"] = True
    models = cache.get("modelUsage")
    payload["models"] = len(models) if isinstance(models, dict) else 0
    days = cache.get("dailyActivity")
    payload["days"] = len(days) if isinstance(days, list) else None
    payload["as_of"] = cache.get("lastComputedDate") or None
    return payload


START_COMMANDS = (
    ("scan", "lifetime tokens per model, and every place a dollar figure is a placeholder"),
    ("units", "fields that are zero on every model while tokens are not"),
    ("recent", "tokens in the last N days, with the days that carry no record named"),
    ("quiet", "which days are silent on both sides, and which only look silent"),
    ("nested", "the same totals split by scope (reads ~/.claude/projects)"),
    ("compare", "two counters side by side (also needs Codex rollouts)"),
)


def render_start(payload):
    lines = [
        "quotamirror - what is on this machine",
        f"  receipt : {_tilde(payload['receipt'])}",
    ]
    if not payload["readable"]:
        lines.append(f"  found   : nothing - {payload['reason']}")
        lines.append(
            "  scope   : this is the only path a bare invocation opens; every command "
            "takes --path FILE"
        )
        return "\n".join(lines) + "\n"
    if not payload["models"]:
        lines.append(
            "  found   : readable, but this receipt carries no modelUsage entries - "
            "no model count to report"
        )
    else:
        parts = [f"{payload['models']} model(s)"]
        if payload["days"] is not None:
            parts.append(f"{payload['days']} day(s) of dailyActivity")
        if payload["as_of"]:
            parts.append(f"as of {payload['as_of']}")
        lines.append(f"  found   : {', '.join(parts)}")
    lines.append(
        "  scope   : the receipt commands open this one path; a transcript command "
        "reads ~/.claude/projects instead"
    )
    lines.append("")
    lines.append("START WITH")
    width = max(len(name) for name, _ in START_COMMANDS)
    for name, blurb in START_COMMANDS:
        lines.append(f"  {name.ljust(width)}  {blurb}")
    lines.append(f"  {'--help'.ljust(width)}  every command, every flag")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quotamirror")
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="read a usage cache and print a receipt")
    _add_path_arg(
        scan,
        Path.home() / ".claude" / "stats-cache.json",
        "the usage cache to read",
    )
    scan.add_argument("--json", action="store_true", help="machine-readable payload")
    scan.add_argument("--today", default=None, help="YYYY-MM-DD; enables staleness measurement")
    units = sub.add_parser("units", help="magnitude audit: zero capacities, inferred units, percent scales")
    _add_path_arg(
        units,
        Path.home() / ".claude" / "stats-cache.json",
        "the usage cache whose fields are audited",
    )
    units.add_argument("--json", action="store_true", help="machine-readable payload")
    units.add_argument(
        "--cumulative",
        action="store_true",
        help="add the CUMULATIVE verdict: a running total summed as a daily increment",
    )
    loop = sub.add_parser("loop", help="repeat detection over session transcripts (arguments hashed, never printed)")
    _add_path_arg(
        loop,
        Path.home() / ".claude" / "projects",
        "the transcript directory to scan",
    )
    loop.add_argument("--json", action="store_true", help="machine-readable payload")
    loop.add_argument("--max-files", type=int, default=None, help="only the newest N transcripts")
    drift = sub.add_parser(
        "drift",
        help="receipt schema drift: diff this receipt's field set against the last other version recorded",
    )
    _add_path_arg(
        drift,
        Path.home() / ".claude" / "stats-cache.json",
        "the receipt whose field set is diffed",
    )
    drift_shapes = Path.home() / ".cache" / "quotamirror" / "shapes.json"
    drift.add_argument(
        "--store",
        default=str(drift_shapes),
        help=(
            "where field-set snapshots are kept, one per receipt version (paths and type names "
            f"only; default {_tilde(drift_shapes)})"
        ),
    )
    drift.add_argument("--json", action="store_true", help="machine-readable payload")
    drift.add_argument(
        "--no-record", action="store_true", help="compare without writing a snapshot"
    )
    revise = sub.add_parser(
        "revise",
        help=(
            "the same keys read twice: both readings and the signed difference, so a "
            "downward revision is not reported as less usage"
        ),
    )
    _add_path_arg(
        revise,
        Path.home() / ".claude" / "stats-cache.json",
        "the receipt whose counters are read twice",
    )
    revise.add_argument(
        "--store",
        default=str(READING_STORE_DEFAULT),
        help=(
            "where earlier readings are kept, newest 5 per receipt (token numbers under a path "
            f"digest, mode 0600; default {_tilde(READING_STORE_DEFAULT)})"
        ),
    )
    revise.add_argument("--json", action="store_true", help="machine-readable payload")
    revise.add_argument(
        "--no-record", action="store_true", help="compare without recording this reading"
    )
    recent = sub.add_parser(
        "recent",
        help=(
            "tokens inside a declared trailing window placed by a recorded day (last N days; "
            "a floor, not a full-window bill, when days are missing)"
        ),
        add_help=False,
    )
    recent_input = recent.add_argument_group(
        "input", "the transcript directory and how much of it is read"
    )
    recent_place = recent.add_argument_group("window", "where the trailing window sits")
    recent_output = recent.add_argument_group("output", "what the command prints")
    recent_general = recent.add_argument_group("options")
    recent_general.add_argument("-h", "--help", action="help", help="show this help message and exit")
    _add_path_arg(
        recent_input,
        Path.home() / ".claude" / "projects",
        "the transcript directory to measure",
    )
    recent_input.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="only the newest N transcripts (default: None -- every transcript is read)",
    )
    recent_place.add_argument(
        "--days",
        type=int,
        default=RECENT_DAYS_DEFAULT,
        help=f"window length in days, inclusive of the anchor day (default: {RECENT_DAYS_DEFAULT})",
    )
    recent_place.add_argument(
        "--until",
        default=None,
        help=(
            "YYYY-MM-DD anchor for the last day of the window (default: None -- the newest recorded "
            "day is used, so the clock never places the window)"
        ),
    )
    recent_output.add_argument("--json", action="store_true", help="machine-readable payload")
    recent_output.add_argument(
        "--by-day",
        action="store_true",
        help=(
            "print one row per day in the window; a day with no record prints 'no record', never 0 "
            "(the rows sum to the same total, they are not a second accounting)"
        ),
    )
    quiet = sub.add_parser(
        "quiet",
        help=(
            "which days are silent on both sides, and which only look silent: the receipt's "
            "day table against the transcripts' token days"
        ),
    )
    _add_path_arg(
        quiet,
        Path.home() / ".claude" / "stats-cache.json",
        "the receipt whose day table is compared",
    )
    quiet.add_argument(
        "--transcripts",
        default=str(Path.home() / ".claude" / "projects"),
        help="the transcript directory compared against that day table (default ~/.claude/projects)",
    )
    quiet.add_argument(
        "--max-files", type=int, default=None, help="only the newest N transcripts"
    )
    _add_floor_args(quiet)
    quiet.add_argument(
        "--compare-reading",
        metavar="PATH",
        action="append",
        default=None,
        help=(
            "a reading saved earlier (a `quiet --json` payload or its reading block) to compare "
            "this one with; repeat it oldest-first and every neighbouring pair is compared, not "
            "only the last; without it this reading stands alone"
        ),
    )
    quiet.add_argument(
        "--manifest",
        action="store_true",
        help=(
            "carry the name, size, mtime and content hash of every file read inside the reading, "
            f"so a comparison can name the file that moved (capped at {MANIFEST_LIMIT} files)"
        ),
    )
    quiet.add_argument("--json", action="store_true", help="machine-readable payload")

    nested = sub.add_parser(
        "nested",
        help=(
            "totals split by scope, each stamped includes_descendants + accounting + a measured "
            "window (Claude transcripts)"
        ),
        add_help=False,
    )
    nested_input = nested.add_argument_group(
        "input",
        "the transcript directory and how much of it is read",
    )
    nested_measure = nested.add_argument_group(
        "measurement",
        "an optional receipt placed beside this command's own measured window",
    )
    nested_thresholds = nested.add_argument_group(
        "day cross-check",
        "the two numbers that decide a day's verdict",
    )
    nested_output = nested.add_argument_group("output", "what the command prints")
    nested_general = nested.add_argument_group("options")
    nested_general.add_argument("-h", "--help", action="help", help="show this help message and exit")
    _add_path_arg(
        nested_input,
        Path.home() / ".claude" / "projects",
        "the transcript directory to measure",
    )
    nested_input.add_argument("--max-files", type=int, default=None, help="only the newest N transcripts")
    nested_measure.add_argument(
        "--cache",
        default=None,
        help=(
            "stats-cache.json whose declared day table is placed beside this command's measured "
            "window; omit and only one of the two windows is known"
        ),
    )
    _add_floor_args(nested_thresholds)
    nested_output.add_argument("--json", action="store_true", help="machine-readable payload")
    compare = sub.add_parser(
        "compare",
        help=(
            "two harnesses: every total stamped with includes_descendants + accounting + window, "
            "the three witnesses, and the day overlap"
        ),
        add_help=False,
    )
    compare_input = compare.add_argument_group(
        "input",
        "the two receipts and how much of them is read",
    )
    compare_measure = compare.add_argument_group(
        "measurement",
        "an optional second witness placed beside the cache's declared day table",
    )
    compare_thresholds = compare.add_argument_group(
        "day cross-check",
        "the two numbers that decide a day's verdict",
    )
    compare_verdicts = compare.add_argument_group(
        "verdicts",
        "what is computed on top of the two totals",
    )
    compare_output = compare.add_argument_group("output", "what the command prints")
    compare_general = compare.add_argument_group("options")
    compare_general.add_argument("-h", "--help", action="help", help="show this help message and exit")
    compare_input.add_argument(
        "--codex",
        default=str(Path.home() / ".codex" / "sessions"),
        help="rollout directory of the second harness (Codex CLI sessions)",
    )
    _add_path_arg(
        compare_input,
        Path.home() / ".claude" / "stats-cache.json",
        "the claude-side receipt compared with those rollouts",
    )
    compare_input.add_argument(
        "--max-sessions", type=int, default=None, help="only the newest N rollouts"
    )
    compare_measure.add_argument(
        "--claude-transcripts",
        default=None,
        help=(
            "Claude transcript directory; opt-in second measurement of the claude window from record "
            "timestamps, so the cache's declared day table and the records are printed side by side"
        ),
    )
    _add_floor_args(compare_thresholds)
    compare_verdicts.add_argument(
        "--poll-share",
        action="store_true",
        help="add COORDINATION SPEND: share of turns that only polled, always with its denominator",
    )
    compare_output.add_argument("--json", action="store_true", help="machine-readable payload")
    args = parser.parse_args(argv)

    if args.command is None:
        payload = start_report(DEFAULT_CACHE)
        sys.stdout.write(render_start(payload))
        return 0 if payload["readable"] else 2
    if args.command in {"nested", "compare", "quiet"}:
        try:
            _floor(args.min_tokens, args.tolerance)
        except ValueError as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2

    if args.command == "recent":
        root = Path(args.path)
        if not root.exists():
            print(f"quotamirror: no transcripts at {root}", file=sys.stderr)
            return 2
        if args.days < 1:
            print("quotamirror: --days must be at least 1", file=sys.stderr)
            return 2
        if args.until is not None:
            try:
                date.fromisoformat(args.until)
            except ValueError:
                print(f"quotamirror: --until must be YYYY-MM-DD, got {args.until!r}", file=sys.stderr)
                return 2
        payload = recent_window(root, days=args.days, until=args.until, max_files=args.max_files)
        payload["source"] = str(root)
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render_recent_window(payload, by_day=args.by_day))
        return 0

    if args.command == "quiet":
        try:
            cache = load_cache(args.path)
        except OSError as exc:
            print(f"quotamirror: cannot read {args.path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2
        root = Path(args.transcripts)
        if not root.exists():
            print(f"quotamirror: no transcripts at {root}", file=sys.stderr)
            return 2
        scan = scan_usage(root, max_files=args.max_files, manifest=args.manifest)
        priors = []
        for supplied in args.compare_reading or []:
            try:
                priors.append(read_prior_reading(supplied))
            except ValueError as exc:
                print(f"quotamirror: {exc}", file=sys.stderr)
                return 2
        prior = priors[-1] if priors else None
        payload = quiet_days(
            cache,
            scan,
            min_tokens=args.min_tokens,
            tolerance=args.tolerance,
            prior=prior,
            priors=priors,
        )
        payload["source"] = str(Path(args.path).resolve())
        payload["transcript_root"] = str(root.resolve())
        payload["files"] = len(scan["files"])
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_quiet(payload), end="")
        return 0

    if args.command == "nested":
        root = Path(args.path)
        if not root.exists():
            print(f"quotamirror: no transcripts at {root}", file=sys.stderr)
            return 2
        cache = None
        if args.cache:
            try:
                cache = load_cache(args.cache)
            except OSError as exc:
                print(f"quotamirror: cannot read {args.cache}: {exc.strerror or exc}", file=sys.stderr)
                return 2
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"quotamirror: {exc}", file=sys.stderr)
                return 2
        payload = nested_audit(
            root, max_files=args.max_files, cache=cache,
            min_tokens=args.min_tokens, tolerance=args.tolerance,
        )
        payload["source"] = str(root)
        payload["cache_source"] = args.cache
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render_nested(payload))
        return 0

    if args.command == "compare":
        try:
            cache = load_cache(args.path)
        except OSError as exc:
            print(f"quotamirror: cannot read {args.path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2
        root = Path(args.codex)
        if not root.is_dir():
            print(f"quotamirror: no rollouts at {root}", file=sys.stderr)
            return 2
        if args.claude_transcripts and not Path(args.claude_transcripts).exists():
            print(f"quotamirror: no transcripts at {args.claude_transcripts}", file=sys.stderr)
            return 2
        payload = compare_harnesses(
            root,
            cache,
            max_sessions=args.max_sessions,
            claude_transcripts=args.claude_transcripts,
            min_tokens=args.min_tokens,
            tolerance=args.tolerance,
        )
        payload["source"] = str(root.resolve())
        payload["claude_source"] = args.claude_transcripts
        if args.poll_share:
            payload["poll_share"] = poll_share(root, max_sessions=args.max_sessions)
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            text = render_compare(payload)
            if args.poll_share:
                text += "\n" + render_poll_share(payload["poll_share"])
            sys.stdout.write(text)
        return 0

    if args.command == "revise":
        cache_path = os.path.abspath(args.path)
        try:
            cache = load_cache(cache_path)
        except OSError as exc:
            print(f"quotamirror: cannot read {cache_path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2
        store_path = Path(args.store)
        try:
            # compare against what was already on disk, then append this reading:
            # one reading can only show the present, two make a revision visible
            store = load_readings(store_path)
            payload = revise_report(store, cache_path, cache)
            if not args.no_record:
                record_reading(store_path, cache_path, cache)
        except (OSError, ValueError) as exc:
            print(f"quotamirror: cannot use store {store_path}: {exc}", file=sys.stderr)
            return 2
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render_revise(payload, recorded=not args.no_record))
        return 0

    if args.command == "drift":
        cache_path = os.path.abspath(args.path)
        try:
            cache = load_cache(cache_path)
        except OSError as exc:
            print(f"quotamirror: cannot read {cache_path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2
        store_path = Path(args.store)
        try:
            # read before writing: the shape sitting in the store is the baseline
            # this receipt is diffed against for a change *inside* one version
            store = load_store(store_path)
            payload = drift_report(store, cache_path, cache)
            if not args.no_record:
                record_shape(store_path, cache_path, cache)
        except (OSError, ValueError) as exc:
            print(f"quotamirror: cannot use store {store_path}: {exc}", file=sys.stderr)
            return 2
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render_drift(payload))
        return 0


    if args.command == "loop":
        root = Path(args.path)
        if not root.exists():
            print(f"quotamirror: no transcripts at {root}", file=sys.stderr)
            return 2
        payload = loop_audit(root, max_files=args.max_files)
        payload["source"] = str(root)
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            sys.stdout.write(render_loop(payload))
        return 0

    if args.command == "units":
        try:
            cache = load_cache(args.path)
        except OSError as exc:
            print(f"quotamirror: cannot read {args.path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2
        payload = audit_units(cache)
        payload["source"] = args.path
        if args.cumulative:
            payload["cumulative"] = audit_cumulative(cache)
        payload = redact_home(payload)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            text = render_units(payload)
            if args.cumulative:
                text += "\n" + render_cumulative(payload["cumulative"])
            sys.stdout.write(text)
        return 0

    if args.today is not None:
        try:
            date.fromisoformat(args.today)
        except ValueError:
            print(
                f"quotamirror: --today must be YYYY-MM-DD, got {args.today!r}",
                file=sys.stderr,
            )
            return 2

    try:
        cache = load_cache(args.path)
    except OSError as exc:
        print(f"quotamirror: cannot read {args.path}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"quotamirror: {exc}", file=sys.stderr)
        return 2

    payload = summarize(cache, today=args.today)
    payload["source"] = args.path
    payload = redact_home(payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_text(payload, source=payload["source"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
