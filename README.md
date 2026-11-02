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

A receipt can be recognisable and still hold no `modelUsage` entries — a half-written
cache, or one whose per-model field was renamed. The sum over an empty dict is not a
measurement, so the section prints `not measured` with the reason instead of
`TOTAL 0`, the attribution line refuses to compare one view, and a receipt with no
days at all no longer claims every activity day has a token record. When the day view
(`dailyModelTokens`) does hold tokens, `scan` prints that sum with its day count next
to the refusal, labelled as a lower bound rather than the lifetime total.

### `units` — audit magnitude and counter shape

```bash
python3 quotamirror.py units
python3 quotamirror.py units --cumulative
python3 quotamirror.py units --json
```

Checks zero capacities, series units inferred by exact arithmetic, suspicious percent scales, monotone cumulative counters, and fine-grained versus coarse totals.

The capacity all-clear states how many models it audited, and changes its sentence when
there were none: `0 modelUsage entries in this receipt - no capacity field can be audited,
and none is claimed to be absent`. An all-clear over zero models is a claim about a file
nobody read; models present with no tokens read `not audited`, because a zero capacity and
an unreported one look identical there.

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

Stores a format-version marker plus a normalized schema: dotted field paths and type names grouped by declared receipt version. Model dictionary keys are replaced with `<model>`, date-shaped dictionary keys with `<date>`, and the receipt is identified by a SHA-256 digest of its canonical path rather than by the path itself. Capture timestamps, counters, receipt values, and model names are not stored. The store is atomically replaced with mode `0600`; a symlink destination is refused. Without `--store` it lives at `~/.cache/quotamirror/shapes.json`, one snapshot per receipt version. On the next recording, a legacy schema-1 store is replaced rather than migrated because its keys may contain the private data this format removes.

### `revise` — the same keys, read twice

```bash
python3 quotamirror.py revise
python3 quotamirror.py revise --json
python3 quotamirror.py revise --no-record
python3 quotamirror.py revise --store ./readings.json --path ./fixture.json
```

A receipt describes the present, but it is not immutable. The same key read twice can carry a *smaller* number the second time, because the vendor recomputed history. `revise` records one reading — per-model lifetime token totals plus one cell per day and model — and the next run compares against it: both readings, the signed difference, and how many keys were comparable on each side.

Only the downward direction needs no further explanation: a monotone counter cannot un-spend tokens, so a key that reads lower was **revised**, and the report says so instead of calling it less usage. An upward change may be new work or a late write onto an older day, and two readings cannot tell those apart. A key present in only one reading is never counted as zero — it is reported as appeared or absent. A day that the earlier receipt had already declared computed is flagged `closed`, because a change there is the entry being rewritten rather than work landing on it.

The first run prints a first reading and records it; the second is what makes a revision visible. The store keeps the newest five readings per receipt under a SHA-256 digest of the receipt's path, atomically replaced with mode `0600` (a symlink destination is refused). It holds token numbers and model IDs — not paths, prompts, or transcript text. Without `--store` it lives at `~/.cache/quotamirror/readings.json`; both commands print that default in `--help`, because a file the tool writes and does not name is a file the user has to find in the source.

Two cases print a refusal instead of a number. A receipt with no monotone token counter
records `0 keys` and says so — read again and it will still be 0 keys, so no revision can
ever be stated from that pair. And when no key is present in both readings, the net line
reads `net: nothing to sum` rather than `net +0 tokens`: a sum over nothing is not a total.
An absent `lastComputedDate` prints `not recorded in this receipt` instead of `None`.

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

With `--claude-transcripts`, the CLAUDE section prints the same `observation` line as `nested`, and a scan that read nothing is reported as an unmeasured second window instead of being placed beside the cache's:

```text
  transcript   : not measured -- store_empty -- no transcript file was read under --claude-transcripts, so the second window was never measured (an empty scan is an absence of observation, not an observation of zero usage)
  observation   : store_empty -- 0 file(s) read, 0 usage record(s) read
  cache table  : 2026-05-03..2026-10-25 (38 day(s), basis=declared_attribution)
```

Without the flag the line reads `not_read` and names the flag that would measure it, so the three zeros (unread, empty store, no usage records) never look alike.

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

Every run also prints an `observation` line, because a zero over a directory that held no transcript is not a measured zero:

```text
  transcripts   : 0 file(s), 0 usage record(s) -> 0 after dedupe by message id
  observation   : store_empty -- 0 file(s) read, 0 usage record(s) read
  verdict: unobserved
```

`store_empty` (nothing was there to read), `no_usage_records` (files were read and none carried a usage block) and `usage_observed` are three different facts, and only the last one supports a statement about usage.

## Reading the help

Every option says what it reads or writes and which default it uses. `--path` names the default for the command you are running, because the default is not the same everywhere: the receipt commands open `~/.claude/stats-cache.json`, while `loop` and `nested` open the transcripts under `~/.claude/projects`.

`compare` prints its nine options under headings - `input`, `measurement`, `day cross-check`, `verdicts`, `output` - so a tolerance is not mistaken for an input directory in a flat list.

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
8. **A zero declares which zero it is.** `store_empty`, `no_usage_records` and `usage_observed` are separate states, and a coverage sentence is not printed about records that were never read. The day overlap obeys the same rule: `shared_days`, `disjoint_windows` and `no_shared_day` are different statements, so `in both : 0` carries the reason it cannot be anything else.
9. **Read-only toward harnesses.** The tool never changes vendor caches or transcripts.
10. **A decrease is a revision, not less usage.** A monotone counter read twice can only read lower if the record was rewritten.

## Privacy

- All processing happens locally.
- Transcript text and tool arguments are never returned in reports.
- `loop` hashes arguments in memory and stores no argument values.
- `drift` stores a format-version marker, normalized field paths, type names, declared version keys, and a non-reversible path digest. It does not store absolute receipt paths, model names, capture times, counters, or receipt values. Writes use atomic replacement with mode `0600`, and symlink store destinations are refused.
- `revise` stores per-reading token numbers and model IDs under a non-reversible path digest, keeps the newest five readings, and writes with mode `0600` through atomic replacement (symlink destinations are refused). It stores no absolute paths, prompts, or transcript text; `--no-record` writes nothing at all.
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

The suite covers receipt reconciliation, unit and cumulative checks, loop deduplication, schema drift, two-reading revisions, cross-harness witnesses, polling shares, nested scope, day-level cross-checks, arbitration between record-sum and message-id-merged totals, that every command can format its own `--help`, and that every option carries a description naming its own default.

## Limitations

- Receipt field names and shapes are harness-specific and can change.
- Token values are trusted as written; this is not provider-side billing verification.
- No price table is bundled, by design.
- Exact-argument repeat detection is a floor, not proof that repeated work was wasteful.
- Message-id merging assumes one ID represents one response.
- Timestamp windows are bounded by records with usable timestamps.
- Scope depends on the harness exposing root/subagent metadata.
- A schema field that keeps the same name and type but changes meaning is invisible to `drift`.
- `revise` only sees the readings it recorded: a revision made before the first reading, or after the latest, falls outside both snapshots. Two readings also cannot separate new work from a late write onto an older day.
- `observation` describes what this run read, not what the harness wrote: a transcript directory that was moved, or a `--max-files` cap that excluded everything, reads as `store_empty` or `no_usage_records` even when the harness holds usage elsewhere. The two are distinguished because one is "nothing here" and the other is "these files had no usage", but neither proves the harness never used a model.

## License

MIT — see [LICENSE](LICENSE).
