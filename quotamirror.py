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

from datetime import date, datetime, timezone

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
        gaps.append(
            {
                "model": model,
                "daily": seen_by_day,
                "model_total": seen_by_model,
                "gap": seen_by_model - seen_by_day,
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
        "gaps": gaps,
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
    else:
        lines.append(
            "  not comparable - the receipt carries no modelUsage entries, so there is "
            "only one view here; nothing can be said about a gap"
        )
    for gap in attribution["gaps"]:
        lines.append(
            f"    {gap['model']:<26} daily {_fmt(gap['daily']):>12}  "
            f"model {_fmt(gap['model_total']):>14}  gap {_fmt(gap['gap']):>14}"
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


def _shape_diff(
    base,
    current,
    base_presence=None,
    current_presence=None,
    presence_compared=False,
    list_presence_compared=None,
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
    if meter_removed:
        verdict = "meter_field_removed"
    elif meter_altered:
        verdict = "meter_field_altered"
    elif meter_narrowed:
        verdict = "meter_field_narrowed"
    elif narrowed:
        verdict = "field_narrowed"
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
        "presence_compared": False,
        "list_presence_compared": False,
        "presence_axes_unmeasured": [],
        "same_as_recorded_at": None,
        "partial_read": None,
        "short_baseline": None,
        "baseline_reads": None,
        "confirmed_removal": False,
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
    if suspected or confirmed:
        payload["partial_read"] = {
            "confirmed": confirmed,
            "shape_digest": digest,
            "fields_baseline": len(base),
            "fields_now": len(current),
            "fields_unconfirmed": diff["removed"],
            "priced_unconfirmed": diff["meter_fields_removed"],
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
            "presence_compared": diff["presence_compared"],
            "list_presence_compared": diff["list_presence_compared"],
            "presence_axes_unmeasured": diff["presence_axes_unmeasured"],
            "fields_baseline": len(base),
        }
    )
    within_verdict = payload["within_version"]["verdict"]
    if within_verdict in ("possible_partial_read", "possible_short_baseline") and (
        added or removed or type_changed or diff["narrowed"]
    ):
        # The same read fed this axis, and this axis has a difference of its own that
        # the suspect record produced. Naming a vendor change from it would be the
        # r89 defect stated across versions. A clean cross-version comparison is left
        # alone: it rests on the other version's record, not on this one.
        payload["verdict"] = within_verdict
    # The upgrade case: version A has a record, version B was just published, and
    # this is the first read at B. A removal-only difference here cannot be told
    # from a read taken while the file was being rewritten - and the within-version
    # axis has nothing at B to check it against. Naming a vendor deletion from that
    # one read would be the r89 defect stated across versions. The paths are
    # reported unconfirmed, and the read is still recorded: the same smaller shape
    # read twice at B is what makes it a removal (test_cross_version_removal).
    if (
        within_verdict not in ("possible_partial_read", "possible_short_baseline")
        and removed
        and not added
        and not type_changed
        and not diff["narrowed"]
    ):
        entry_here = versions.get(current_key) or {}
        seen_here = bool(entry_here.get("shape")) and shape_digest(
            entry_here["shape"]
        ) == shape_digest(current)
        if not seen_here:
            payload["verdict"] = "removal_unconfirmed"
            payload["cross_version_unconfirmed"] = {
                "version_key": current_key,
                "baseline_version_key": baseline_key,
                "baseline_at": baseline.get("at"),
                "fields_baseline": len(base),
                "fields_now": len(current),
                "fields_unconfirmed": removed,
                "priced_unconfirmed": meter_removed,
                "reads_at_current": entry_here.get("reads"),
            }
    return payload


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
            lines.append(
                "  Run drift again on the settled file: the same smaller shape twice at this"
            )
            lines.append(
                "  version is a removal; the fuller shape again means this read was the short one."
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
        if payload["meter_fields_narrowed"]:
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
        if payload["narrowed"]:
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
        if within.get("confirmed_removal"):
            lines.append("")
            lines.append(
                "  This smaller shape has now been read twice at this version, so it is recorded:"
            )
            lines.append("  two agreeing reads are what a half-written file cannot produce.")
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
        "LIMIT: a removal seen once across a version change is removal_unconfirmed, never a"
    )
    lines.append(
        "verdict - but a field that drops from SOME members of a family (a narrowing) at a"
    )
    lines.append(
        "brand-new version is still named on the first read: this guard tests the path set,"
    )
    lines.append("and a narrowing leaves that path set intact.")
    lines.append(
        "LIMIT: a list that rolls over (a window of days) can lose a carrier because an old"
    )
    lines.append(
        "member left the window, not because the vendor dropped the field, and the count"
    )
    lines.append("cannot tell those two apart. Two lists under different keys also fold into")
    lines.append("one count, so '2 of 8' can mean 1 of 3 plus 1 of 5.")
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


def scan_usage(root, max_files=None):
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
    skipped_lines = 0
    records = 0
    naive_tokens = 0
    timed = 0
    untimed = 0
    order = []
    entries = {}

    for path in files:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            files_skipped += 1
            continue
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

    return {
        "root": str(root_path),
        "files": files,
        "files_skipped": files_skipped,
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
    with open(path, "r", encoding="utf-8") as handle:
        cache = json.load(handle)
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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quotamirror")
    sub = parser.add_subparsers(dest="command", required=True)
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
    if args.command in {"nested", "compare"}:
        try:
            _floor(args.min_tokens, args.tolerance)
        except ValueError as exc:
            print(f"quotamirror: {exc}", file=sys.stderr)
            return 2

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
