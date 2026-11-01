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

from datetime import date

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
        dollars = {
            "status": "unknown",
            "usd": None,
            "models_with_zero_unpriced": len(unpriced),
            "reason": (
                f"{len(unpriced)} of {len(by_model)} models record costUSD = 0.00 while "
                f"{total:,} tokens were seen; 0.00 is the meter's placeholder, not a price"
            ),
        }

    _, daily_per_day = _daily_totals(cache)
    day_series = [{"date": name, "tokens": daily_per_day[name]} for name in sorted(daily_per_day)]
    busiest_day = max(day_series, key=lambda d: d["tokens"]) if day_series else None

    return {
        "tokens": {"total": total, "by_model": by_model},
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
    return {"zeros": zeros, "series": _series(cache), "percents": _percent_audit(cache)}


def render_units(payload):
    lines = ["UNITS - magnitude audit of the meter's own fields", ""]
    lines.append("CAPACITY (a field that is present where zero is not a possible value)")
    zeros = payload.get("zeros") or []
    if not zeros:
        lines.append("  no capacity field is zero for every model")
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
    ranked = sorted(tokens["by_model"].items(), key=lambda kv: kv[1]["tokens"], reverse=True)
    for model, entry in ranked:
        lines.append(f"  {model:<26}{_fmt(entry['tokens']):>16}  {entry['share'] * 100:5.1f}%")
    lines.append(f"  {'TOTAL':<26}{_fmt(tokens['total']):>16}")
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
    lines.append(
        f"  per-day view {_fmt(attribution['daily_total'])} - "
        f"per-model view {_fmt(attribution['model_total'])} - "
        f"gap {_fmt(attribution['gap'])} ({attribution['gap_share'] * 100:.2f}%)"
    )
    lines.append(
        f"  {attribution['reconciled_models']} of {attribution['models_compared']} models reconcile"
    )
    for gap in attribution["gaps"]:
        lines.append(
            f"    {gap['model']:<26} daily {_fmt(gap['daily']):>12}  "
            f"model {_fmt(gap['model_total']):>14}  gap {_fmt(gap['gap']):>14}"
        )
    lines.append("")

    coverage = payload["coverage"]
    lines.append("COVERAGE")
    lines.append(
        f"  activity {coverage['activity_days']} days "
        f"({coverage['activity_first']} .. {coverage['activity_last']}) - "
        f"token records {coverage['token_days']} days "
        f"({coverage['tokens_first']} .. {coverage['tokens_last']})"
    )
    missing = coverage["activity_without_tokens"]
    if missing:
        lines.append(
            f"  {len(missing)} day(s) with messages but no token record: {', '.join(missing)}"
        )
    else:
        lines.append("  every activity day has a token record")
    return "\n".join(lines) + "\n"


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


def shape_of(receipt, prefix=""):
    """Map every path in a receipt to its type name. Values are never kept."""
    shape = {}
    if isinstance(receipt, dict):
        for key, value in receipt.items():
            parent = prefix.rstrip(".").rsplit(".", 1)[-1].rstrip("[]")
            if parent in _MODEL_KEYED_FIELDS:
                segment = "<model>"
            else:
                segment = "<date>" if _DATE_KEY.match(str(key)) else str(key)
            child = f"{prefix}{segment}"
            shape[child] = _type_name(value)
            shape.update(shape_of(value, prefix=f"{child}."))
    elif isinstance(receipt, list):
        for item in receipt:
            shape.update(shape_of(item, prefix=f"{prefix[:-1]}[]."))
    return shape


STORE_SCHEMA = 2


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


def record_shape(store_path, cache_path, receipt, captured_at=None):
    """Persist a receipt's shape under its declared version. Values never reach disk."""
    if store_path.is_symlink():
        raise ValueError(f"refusing symlink shape store: {store_path}")
    store = load_store(store_path)
    if store.get("schema") != STORE_SCHEMA:
        store = {"schema": STORE_SCHEMA, "caches": {}}
    key, _ = _version_of(receipt)
    versions = store["caches"].setdefault(path_identity(cache_path), {}).setdefault("versions", {})
    versions.pop(key, None)
    versions[key] = {"shape": shape_of(receipt)}
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


def drift_report(store, cache_path, receipt):
    """Compare a receipt's current shape with the last shape recorded at another version."""
    caches = store.get("caches", {})
    versions = caches.get(path_identity(cache_path), {}).get("versions", {})
    if not versions:
        versions = caches.get(str(cache_path), {}).get("versions", {})
    current_key, disagree = _version_of(receipt)
    current = shape_of(receipt)
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
    }
    if not others:
        return payload

    baseline_key = next(reversed(others))
    baseline = others[baseline_key]
    base = baseline.get("shape") or {}
    removed = sorted(set(base) - set(current))
    added = sorted(set(current) - set(base))
    type_changed = [
        {"path": path, "was": base[path], "now": current[path]}
        for path in sorted(set(base) & set(current))
        if base[path] != current[path]
    ]
    meter_removed = [path for path in removed if _is_meter_path(path)]
    meter_altered = [row["path"] for row in type_changed if _is_meter_path(row["path"])]
    if meter_removed:
        verdict = "meter_field_removed"
    elif meter_altered:
        verdict = "meter_field_altered"
    elif removed or added or type_changed:
        verdict = "schema_changed"
    else:
        verdict = "unchanged"
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
            "fields_baseline": len(base),
        }
    )
    return payload


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
        lines.append("  baseline: none")
        lines.append("")
        lines.append("VERDICT: no_baseline")
        lines.append(
            "  This is the first shape recorded for this receipt version, so there is nothing"
        )
        lines.append(
            "  to compare it against. Drift is unmeasurable on this machine until the harness"
        )
        lines.append(
            "  publishes a different version. The shape just recorded is what the next one"
        )
        lines.append("  will be diffed against - this run cannot tell you whether it changed.")
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
        if payload["meter_fields_removed"]:
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
        if payload["meter_fields_altered"]:
            lines.append("  A field that carries a price changed type:")
            for row in payload["type_changed"]:
                if row["path"] in payload["meter_fields_altered"]:
                    lines.append(f"    {row['path']}: {row['was']} -> {row['now']}")
        if payload["removed"]:
            lines.append("")
            lines.append(f"REMOVED ({len(payload['removed'])})")
            for path in payload["removed"]:
                marker = "!" if path in payload["meter_fields_removed"] else " "
                lines.append(f"  {marker} {path}")
        if payload["added"]:
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
    lines.append(
        "LIMIT: names and types only. A field that keeps its name and type but changes"
    )
    lines.append(
        "meaning (a unit, a currency), or one that stays present and always 0, is invisible here."
    )
    lines.append("LIMIT: no values are stored - only paths and type names.")
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
    }
    if claude_transcripts is None:
        return unavailable

    scan = transcript_window(claude_transcripts)
    transcript_days = set(scan["day_totals"])
    cache_days = set(claude_days)
    only_transcript = sorted(transcript_days - cache_days)
    only_cache = sorted(cache_days - transcript_days)
    both = sorted(transcript_days & cache_days)
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
        "days_transcript_only": only_transcript,
        "days_cache_only": only_cache,
        "days_both": both,
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
    both = sorted(set(codex_days) & set(claude_days))

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
    crosscheck = crosscheck_days(
        claude_days,
        {"days": transcripts["days"]} if transcripts is not None else None,
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
        "overlap": {
            "days_both": both,
            "days_codex_only": sorted(set(codex_days) - set(claude_days)),
            "days_claude_only": sorted(set(claude_days) - set(codex_days)),
        },
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
    lines.append("DAY OVERLAP")
    lines.append(f"  in both     : {len(payload['overlap']['days_both'])}")
    lines.append(f"  codex only  : {len(payload['overlap']['days_codex_only'])}"
                 + (f" ({', '.join(payload['overlap']['days_codex_only'][:6])})" if payload['overlap']['days_codex_only'] else ""))
    lines.append(f"  claude only : {len(payload['overlap']['days_claude_only'])}"
                 + (f" ({', '.join(payload['overlap']['days_claude_only'][:6])})" if payload['overlap']['days_claude_only'] else ""))
    lines.append("")
    lines.append("CLAUDE, TWO COUNTERS (one quantity, two windows)")
    pair = payload["claude_window"]
    if not pair["available"]:
        lines.append(f"  transcript   : not measured -- {pair['reason']}")
        lines.append(f"  cache table  : {_window_text(pair['cache'])}")
    else:
        lines.append(f"  transcript   : {_window_text(pair['transcript'])}"
                     f"  [{_stamp_text(stamps['claude.transcripts'])}]")
        lines.append(f"  cache table  : {_window_text(pair['cache'])}  [{_stamp_text(stamps['claude.daily'])}]")
        lines.append(f"  days in both : {len(pair['days_both'])}"
                     f" | transcript only: {len(pair['days_transcript_only'])}"
                     f" | cache only: {len(pair['days_cache_only'])}")
        if pair["days_transcript_only"]:
            only = pair["days_transcript_only"]
            shown = ", ".join(only[:6]) + (" ..." if len(only) > 6 else "")
            pct = pair["pct_transcript_only"]
            lines.append(
                f"    in the transcript, not in the cache table: {shown} -- "
                f"{pair['tokens_transcript_only']:,} token(s)"
                + (f" = {pct}% of the transcript total" if pct is not None else "")
            )
        if pair["days_cache_only"]:
            only = pair["days_cache_only"]
            lines.append("    in the cache table, not in the transcript: "
                         + ", ".join(only[:6]) + (" ..." if len(only) > 6 else ""))
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

    shared = sorted(set(cache_days) & set(transcript_days))
    rows = []
    below_floor = 0
    for date in shared:
        table = transcript_days[date] or {}
        merged = int(table.get("total_tokens") or 0)
        naive = int(table.get("naive_tokens") or 0)
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
    result["days_cache_only"] = len(set(cache_days) - set(transcript_days))
    result["days_transcript_only"] = len(set(transcript_days) - set(cache_days))
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
        result["reason"] = (
            "no day is both above the token floor and present in both sources, so the two "
            "day tables cannot be compared here"
        )
    elif record_sum_days > merged_days:
        result["verdict"] = "record_sum_confirmed"
        result["reason"] = (
            f"{record_sum_days} of {len(rows)} comparable day(s) match the record sum and "
            f"{merged_days if merged_days else 'none'} match the merged total: this cache counts "
            f"repeated writes of the same "
            "message, so its day table is a record sum and not a bill"
        )
    elif merged_days > record_sum_days:
        result["verdict"] = "merged_confirmed"
        result["reason"] = (
            f"{merged_days} of {len(rows)} comparable day(s) match the merged total and "
            f"{record_sum_days if record_sum_days else 'none'} match the record sum: on this corpus "
            f"the cache's day table is "
            "one count per message"
        )
    else:
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
    lines.append(
        f"  compared {result['days_compared']} day(s) of at least {result['min_tokens']:,} tokens "
        f"(within {int(result['tolerance'] * 100)}% counts as a match); "
        f"{result['days_below_floor']} shared day(s) below that floor excluded, "
        f"{result['days_cache_only']} cache-only, {result['days_transcript_only']} transcript-only"
    )
    if not result["days"]:
        lines.append(f"  {result['reason']}")
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
        "coverage": {
            "records_timed": scan["records_timed"],
            "records_without_timestamp": untimed,
            "note": (
                "every usage record carried a timestamp, so the printed window bounds all records read"
                if not untimed
                else (
                    f"{untimed} usage record(s) carry no timestamp, so the printed window does not bound "
                    "their records; tokens from them are still in the total"
                )
            ),
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
            f"(default {DEFAULT_TOLERANCE} = {round(DEFAULT_TOLERANCE * 100)}%)"
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
        verdict = "no_usage"
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
        "note": (
            "every usage record carried a timestamp, so the printed window bounds all records read"
            if not untimed
            else (
                f"{untimed} usage record(s) carry no timestamp: the printed window bounds only the "
                "timed ones, so a record outside that range would not move it"
            )
        ),
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
                f"{arbitration['days_compared']} shared day(s) of at least "
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
                f"table does not list: {', '.join(windows['days_transcript_only'][:8])}"
            )
        if windows["days_cache_only"]:
            lines.append(
                f"     {len(windows['days_cache_only'])} day(s) are listed by the cache with no transcript here: "
                f"{', '.join(windows['days_cache_only'][:8])}"
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
    scan.add_argument("--path", default=str(Path.home() / ".claude" / "stats-cache.json"))
    scan.add_argument("--json", action="store_true", help="machine-readable payload")
    scan.add_argument("--today", default=None, help="YYYY-MM-DD; enables staleness measurement")
    units = sub.add_parser("units", help="magnitude audit: zero capacities, inferred units, percent scales")
    units.add_argument("--path", default=str(Path.home() / ".claude" / "stats-cache.json"))
    units.add_argument("--json", action="store_true", help="machine-readable payload")
    units.add_argument(
        "--cumulative",
        action="store_true",
        help="add the CUMULATIVE verdict: a running total summed as a daily increment",
    )
    loop = sub.add_parser("loop", help="repeat detection over session transcripts (arguments hashed, never printed)")
    loop.add_argument("--path", default=str(Path.home() / ".claude" / "projects"))
    loop.add_argument("--json", action="store_true", help="machine-readable payload")
    loop.add_argument("--max-files", type=int, default=None, help="only the newest N transcripts")
    drift = sub.add_parser(
        "drift",
        help="receipt schema drift: diff this receipt's field set against the last other version recorded",
    )
    drift.add_argument("--path", default=str(Path.home() / ".claude" / "stats-cache.json"))
    drift.add_argument(
        "--store",
        default=str(Path.home() / ".cache" / "quotamirror" / "shapes.json"),
        help="where field-set snapshots are kept (paths and type names only)",
    )
    drift.add_argument("--json", action="store_true", help="machine-readable payload")
    drift.add_argument(
        "--no-record", action="store_true", help="compare without writing a snapshot"
    )
    nested = sub.add_parser(
        "nested",
        help=(
            "totals split by scope, each stamped includes_descendants + accounting + a measured "
            "window (Claude transcripts)"
        ),
    )
    nested.add_argument("--path", default=str(Path.home() / ".claude" / "projects"))
    nested.add_argument("--json", action="store_true", help="machine-readable payload")
    nested.add_argument("--max-files", type=int, default=None, help="only the newest N transcripts")
    nested.add_argument(
        "--cache",
        default=None,
        help=(
            "stats-cache.json whose declared day table is placed beside this command's measured "
            "window; omit and only one of the two windows is known"
        ),
    )
    _add_floor_args(nested)
    compare = sub.add_parser(
        "compare",
        help=(
            "two harnesses: every total stamped with includes_descendants + accounting + window, "
            "the three witnesses, and the day overlap"
        ),
    )
    compare.add_argument(
        "--codex",
        default=str(Path.home() / ".codex" / "sessions"),
        help="rollout directory of the second harness (Codex CLI sessions)",
    )
    compare.add_argument("--path", default=str(Path.home() / ".claude" / "stats-cache.json"))
    compare.add_argument("--json", action="store_true", help="machine-readable payload")
    compare.add_argument("--max-sessions", type=int, default=None, help="only the newest N rollouts")
    compare.add_argument(
        "--claude-transcripts",
        default=None,
        help=(
            "Claude transcript directory; opt-in second measurement of the claude window from record "
            "timestamps, so the cache's declared day table and the records are printed side by side"
        ),
    )
    _add_floor_args(compare)
    compare.add_argument(
        "--poll-share",
        action="store_true",
        help="add COORDINATION SPEND: share of turns that only polled, always with its denominator",
    )
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
            if args.no_record:
                store = load_store(store_path)
            else:
                store, _ = record_shape(store_path, cache_path, cache)
        except (OSError, ValueError) as exc:
            print(f"quotamirror: cannot use store {store_path}: {exc}", file=sys.stderr)
            return 2
        payload = drift_report(store, cache_path, cache)
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
