# quotamirror

A local, read-only auditor for AI-agent usage receipts.

Usage meters often expose several totals for the same activity: per-model totals, per-day rollups, running counters, deltas, and repeated transcript records. `quotamirror` compares those views, names their accounting basis, and refuses to turn missing prices or incompatible windows into a plausible-looking bill.

## Why

A receipt can be syntactically valid and still be misleading:

- non-zero tokens with `costUSD: 0.00` may mean “price unavailable,” not “free”;
- daily and model totals may disagree;
- a running counter may be summed as if it were a per-turn delta;
- repeated streamed writes may count one message more than once;
- root and subagent work may be mixed without saying so;
- two totals may cover different date windows;
- a cached receipt may be stale.

`quotamirror` prints the evidence and the denominator behind every verdict. It does not compute provider prices.

## Install

Requires Python 3.9+ and has no runtime dependencies.

```bash
git clone https://github.com/Michaelzhao9/quotamirror.git
cd quotamirror
python3 quotamirror.py --help
```

## Commands

### `scan` — reconcile one receipt

```bash
python3 quotamirror.py scan
python3 quotamirror.py scan --path /path/to/stats-cache.json
python3 quotamirror.py scan --today 2026-01-28
python3 quotamirror.py scan --json
```

Reports per-model and per-day totals, signed gaps, days with activity but no token records, price status, and snapshot age.

### `units` — audit magnitude and counter shape

```bash
python3 quotamirror.py units
python3 quotamirror.py units --cumulative
python3 quotamirror.py units --json
```

Checks zero capacities, series units inferred by exact arithmetic, suspicious percent scales, monotone cumulative counters, and fine-grained versus coarse totals.

### `loop` — repeated tool calls

```bash
python3 quotamirror.py loop
python3 quotamirror.py loop --max-files 50 --json
```

Reads session transcripts, hashes tool names plus canonical arguments, and reports exact repeats. Arguments, prompts, file contents, and shell commands are never emitted or stored.

### `drift` — receipt schema changes

```bash
python3 quotamirror.py drift
python3 quotamirror.py drift --json
python3 quotamirror.py drift --no-record
python3 quotamirror.py drift --store ./shapes.json --path ./fixture.json
```

Stores a format-version marker plus a normalized schema: dotted field paths and type names grouped by declared receipt version. Model dictionary keys are replaced with `<model>`, date-shaped dictionary keys with `<date>`, and the receipt is identified by a SHA-256 digest of its canonical path rather than by the path itself. Capture timestamps, counters, receipt values, and model names are not stored. The store is atomically replaced with mode `0600`; a symlink destination is refused. On the next recording, a legacy schema-1 store is replaced rather than migrated because its keys may contain the private data this format removes.

### `compare` — cross-harness accounting

```bash
python3 quotamirror.py compare
python3 quotamirror.py compare --json --max-sessions 50
python3 quotamirror.py compare --poll-share
python3 quotamirror.py compare \
  --claude-transcripts ~/.claude/projects
```

Compares Codex rollout deltas, running totals, and response records with Claude receipt views. Every total is stamped with scope, accounting mode, window, and basis. Totals from different windows are not subtracted.

`--poll-share` reports coordination-only turns at both request and user-turn granularity, with tokens and turn counts printed beside each percentage.

### `nested` — root and subagent scope

```bash
python3 quotamirror.py nested
python3 quotamirror.py nested --json
python3 quotamirror.py nested --cache ~/.claude/stats-cache.json
python3 quotamirror.py nested \
  --cache /path/to/cache.json \
  --min-tokens 1000000 \
  --tolerance 0.02
```

Separates root, sidechain, and combined usage; prints raw-record and message-id-merged totals; and, when a cache is supplied, checks which total the cache's daily cells support.

## Example receipt

The repository examples and tests are synthetic. A deliberately inconsistent fixture may contain:

```text
per-model total: 1,200,000 tokens
per-day total:   1,199,000 tokens
signed gap:      -1,000 tokens
costUSD:         0.00 for models with non-zero usage
snapshot date:   three days before the requested comparison date
```

The expected result is not a guessed bill. It is a receipt stating that the price is unknown, the daily view is short by 1,000 tokens, and the snapshot is stale.

## Verdict principles

1. **Unknown is not zero.** Missing or placeholder prices remain unknown.
2. **A ratio names both sides.** Numerator, denominator, counts, and tolerances are printed.
3. **A column is not truth.** When one file carries multiple counters, all witnesses are reported.
4. **A total declares scope.** Root, sidechain, combined, or unknown is explicit.
5. **A total declares accounting.** Delta and cumulative values are never blended.
6. **Windows must match.** Different date ranges are compared as coverage, not bills.
7. **No evidence means no verdict.** Missing transcripts or baselines produce unavailable/no-baseline states.
8. **Read-only toward harnesses.** The tool never changes vendor caches or transcripts.

## Privacy

- All processing happens locally.
- Transcript text and tool arguments are never returned in reports.
- `loop` hashes arguments in memory and stores no argument values.
- `drift` stores a format-version marker, normalized field paths, type names, declared version keys, and a non-reversible path digest. It does not store absolute receipt paths, model names, capture times, counters, or receipt values. Writes use atomic replacement with mode `0600`, and symlink store destinations are refused.
- Human and JSON reports replace the current home-directory prefix with `~` by default. Explicit paths outside the current home are left unchanged.
- Tests and documentation contain synthetic fixtures only.
- This repository contains no personal usage totals, captured local usernames, prompts, credentials, or transcript content.

The process still opens local files that may contain private text. Run it only against data you own.

## Exit codes

- `0`: report produced
- `2`: unreadable input, unsupported shape, missing required path, or invalid argument

Individual commands may expose additional refusal states in their JSON payloads; they do not silently convert unavailable data to zero.

## Tests

```bash
uv run --with pytest pytest -q
```

The suite covers receipt reconciliation, unit and cumulative checks, loop deduplication, schema drift, cross-harness witnesses, polling shares, nested scope, day-level cross-checks, and arbitration between record-sum and message-id-merged totals.

## Limitations

- Receipt field names and shapes are harness-specific and can change.
- Token values are trusted as written; this is not provider-side billing verification.
- No price table is bundled, by design.
- Exact-argument repeat detection is a floor, not proof that repeated work was wasteful.
- Message-id merging assumes one ID represents one response.
- Timestamp windows are bounded by records with usable timestamps.
- Scope depends on the harness exposing root/subagent metadata.
- A schema field that keeps the same name and type but changes meaning is invisible to `drift`.

## License

MIT — see [LICENSE](LICENSE).
