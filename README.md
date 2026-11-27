# quotamirror

A local, read-only auditor for AI-agent usage receipts.

Usage meters often expose several totals for the same activity: per-model totals, per-day rollups, running counters, deltas, and repeated transcript records. `quotamirror` compares those views, names their accounting basis, and refuses to turn missing prices or incompatible windows into a plausible-looking bill.

## What it reads

By default the receipt commands open Claude Code's local usage cache at `~/.claude/stats-cache.json`
(per-model totals, per-day rollups, counters and session/message counts).
`--claude-transcripts` adds the JSONL transcripts under `~/.claude/projects`, and `loop` / `nested`
read that transcript store instead of the cache. Nothing else is read. `drift` and `revise` write
their own small stores under `~/.cache/quotamirror`, and `quiet --store DIR` writes one when you
name a directory; every other command writes nothing.

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

## Start here

Run with no command and `quotamirror` says what is on this machine before asking you to choose:

```text
$ python3 quotamirror.py
quotamirror - what is on this machine
  receipt : ~/.claude/stats-cache.json
  found   : 9 model(s), 41 day(s) of dailyActivity, as of 2026-10-25
  scope   : the receipt commands open this one path; a transcript command reads ~/.claude/projects instead

START WITH
  scan     lifetime tokens per model, and every place a dollar figure is a placeholder
  units    fields that are zero on every model while tokens are not
  recent   tokens in the last N days, with the days that carry no record named
  quiet    which days are silent on both sides, and which only look silent
  nested   the same totals split by scope (reads ~/.claude/projects)
  compare  two counters side by side (also needs Codex rollouts)
  --help   every command, every flag
```

The counts come from the file, not from an estimate. If nothing is at that path the screen says
`found : nothing - No such file or directory` and exits `2` instead of printing zero models; a file
that is not a receipt names the reason it was rejected.

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

A signed gap between the two lifetime views is only readable if the per-day view covers
the same span. `scan` measures that span from the receipt's own two day tables and prints
it with the gap: when the token table starts after the first activity row, the per-day
figure is called a floor over the days it covers and the number of leading activity days
is named. Each gap model also prints how many day rows name it and what share of its total
those rows account for, so `300 of 200,000 (0.15%)` and a model the token table
never mentions read differently. That second model prints `no day row`, not `daily 0`: an
absent row is not a row that measured zero. JSON gains `attribution.span_verdict`
(`day_view_is_a_floor`, `same_span`, `not_comparable`), `days_before_day_table`,
`day_table_first`, `gap_models`, `gap_models_without_day_rows`, and per gap `presence`,
`day_rows` and `coverage` (`null` when no row names the model). Nothing outside the
receipt is consulted: a store that holds no per-day record before the table's first date
cannot make that period measurable, so the span is reported as covered or not covered,
never estimated.

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

Stores a format-version marker plus a normalized schema: dotted field paths and type names grouped by declared receipt version, with the time that shape was seen. Model dictionary keys are replaced with `<model>`, date-shaped dictionary keys with `<date>`, and the receipt is identified by a SHA-256 digest of its canonical path rather than by the path itself. Model names, receipt values, and the receipt path are not stored. The store is atomically replaced with mode `0600`; a symlink destination is refused. Without `--store` it lives at `~/.cache/quotamirror/shapes.json`, one snapshot per receipt version. Beside each path it stores how many members of the collapsed family carried it - `modelUsage.<model>.costUSD: 8 of 9` - because a field dropped from one model and kept on the other eight produces an identical path set. The same count is kept for list members, so `dailyActivity[].toolCallCount: 40 of 41` is a fact this command can report and a bare path set could not. Those counts are numbers of carriers, never values. A legacy schema-1 store is replaced rather than migrated because its keys may contain the private data this format removes; a schema-2 store is migrated, keeping its baselines and marking that their presence was not recorded.

Two axes, printed separately. **Across versions**: the current receipt against the most recent shape recorded at a *different* version - the opencode 2.2.0 shape, where a field carrying a price was deleted and every meter built on it showed nothing. **Within one version**: the current receipt against the shape already sitting in the store, read *before* this run writes. That second axis exists because a version number is the vendor's claim that the shape did not move, and a harness can drop a field without bumping it; the command used to overwrite the only snapshot it had before anything could be diffed against it. A field that carries a price and disappears at an unchanged version is named as `meter_field_removed` with the line `WITHOUT a version change`. A field that survives on some models and vanishes from others is a different verdict, `meter_field_narrowed`, printed as `NARROWED` with both counts (`8 of 9 model(s) (was 9 of 9)`); a model leaving the receipt entirely is not called a narrowing, because then every field loses one carrier and the shape really is unchanged. That naming needs the same counts read twice at this version (see below); one read prints `possible_narrowing` instead. When the store has nothing at that version the section reads `not measured` and says which of the two reasons applies; no count is printed, because a zero there would be a claim the run cannot make.

A shape recorded by an older build (no timestamp) is still comparable - it just reports `before timestamps were kept` as its baseline. Presence has two axes - keyed families (`<model>`, `<date>`) and list members - and a baseline can carry one without the other. A shape recorded before either existed, or a schema-3 baseline written before list members were counted, is comparable on the path set only: it reports `unchanged_shape_only` and names the axis it never measured (`model / day counts` or `list members`) instead of implying a clean bill, because a field dropped from one member of that family would look exactly the same. A store written by an older build is migrated in place and keeps its baselines.

**The read itself can be wrong.** A receipt that parses but has lost a whole subtree is what a read taken while the file was being written looks like - and it is also what a vendor deletion looks like. Until 0.27.0 the command took the second reading on sight: it named the vanished priced field as removed by the vendor, and then, because recording follows reporting, it replaced the baseline with the short read, so restoring the full file printed ten ADDED fields nobody had added. Both halves are refused now. A read that is a strict subset of the shape recorded at the same version reports `possible_partial_read`, lists the vanished fields with `(unconfirmed)`, and says `baseline kept`; nothing about the vendor is claimed. The same smaller shape read twice is what makes the removal real and lets it be recorded, and the full shape again makes the first read the partial one. The suspicion is remembered as a 16-character digest of the pending shape, not as a second copy of its paths.

**Neither axis can speak from one read.** The two guards above cover a read against a record at the *same* version. The upgrade path had neither: version A has a full shape recorded, the harness publishes version B, and the first read at B is shorter - and that one read was compared straight against A and printed `meter_field_removed`, a vendor deletion asserted from a read nobody had confirmed. A vendor that dropped a subtree and a read taken while the file was being rewritten are the same picture there, and version B has no earlier record to settle it. So a removal-only difference against another version's baseline, on the first read at the current version, is reported as `removal_unconfirmed`: the paths are listed with `(unconfirmed)`, both explanations are named, and no deletion is claimed. The read is still recorded - the same smaller shape read twice at that version is what makes it a `meter_field_removed`, and the fuller shape again means the first read was the short one. A read that also *added* a path is excluded from this guard, because a truncated copy of the baseline cannot have grown, so growth there is unaffected. A *narrowing* - a field gone from some members of a family, which leaves the path set intact - is subtraction too, and from one read it is indistinguishable from an incremental writer caught mid-update, so the first read at a new version reports `narrowing_unconfirmed` with both counts per row and names no narrowing; the same narrower shape read twice is what makes it `meter_field_narrowed`.

**The same ambiguity at an unchanged version.** A narrowing there leaves the path set - and therefore the shape digest - identical, so the guard that catches a shrinking path set had nothing to test, and until 0.31.0 that axis named a narrowing from a single read. It now reports `possible_narrowing`: the older counts stay the baseline, both counts are printed per row with `(unconfirmed)`, no `NARROWED` block appears, and neither does the "a meter that sums across models undercounts" sentence - both would assert the thing the verdict refuses. The challenge is remembered as a digest of the rows themselves (path plus both counts), because the shape cannot tell one narrowing reading from another. The same narrower counts read twice are what make it `meter_field_narrowed` and let them become this version's record; counts that keep moving (`9 of 9` then `8 of 9` then `7 of 9`) are a fresh suspicion every time and never confirm. A read that also grew a path is excluded from this guard for the same reason as on the upgrade path: a truncated copy cannot carry a path its source did not have.

**The record can be the wrong one.** The mirror case had no guard: a receipt that *grew* past the stored shape printed `schema_changed` / `ADDED (10)`, which is the tool asserting a vendor addition from a record nobody ever confirmed - and the first read at a brand-new version is exactly such a record. Each baseline now carries `reads`, the number of reads that produced that exact shape at that version, and one read is not a confirmation. Against a baseline with `reads: 1`, a read that only has more is reported as `possible_short_baseline`: the new fields are listed with `(unconfirmed)`, the two explanations are named (the vendor added them, or the single recorded read was short), and the fuller shape is what gets recorded - a truncated read cannot grow past the record it truncated, so it is strictly more information. The second identical read makes that baseline a record, and only then is a later growth reported as `schema_changed`. Growth against a twice-read baseline is unaffected, and a legacy baseline that never recorded a read count is not reinterpreted (it keeps `schema_changed`).

**The record can be contradicted afterwards.** Confirming a narrowing makes the narrow counts *the record*, and that record had no way to be wrong out loud. A later read in which the carrier is back compared as nothing: the narrowing check looks for counts that fell, the shape is byte-identical, so the verdict was `unchanged` and the sentence was "no priced field lost a carrier" - about the field the tool had itself recorded as lost on 8 of 9 models and which this read holds on 9 of 9. The store was still carrying `narrowing_confirmed_at`, the tool's own claim that it would not happen. Since 0.32.0 a presence gain at an unchanged version is its own verdict: `meter_field_regained` when the field carries a price, `field_regained` otherwise, printed as `REGAINED (n)` with both counts (`9 of 9 model(s) (was 8 of 9)`) and the timestamp of the narrowing it contradicts. A family that *grew* is not a returning carrier - a new model arriving with the field raises both numbers at once - so a row only counts while the family size is unchanged on both sides; the real-corpus probe rolls a 10th model into a narrowed record and the verdict stays `unchanged`. The sentence says what it can support: a stale copy of the older bytes and a record that was simply wrong look the same from here, so the return is not attributed to the vendor, and the fuller read becomes the record (a truncated read cannot grow past what it truncated). The regain is printed even when a bigger verdict takes the paragraph - a read that also loses a whole path reports `possible_partial_read` and still lists the `REGAINED` rows, because a presence gain is a measurement, not a suspicion.

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

### `recent` — tokens inside a declared trailing window

```bash
python3 quotamirror.py recent
python3 quotamirror.py recent --days 30
python3 quotamirror.py recent --days 7 --by-day
python3 quotamirror.py recent --until 2026-10-31 --days 7
python3 quotamirror.py recent --json
```

Answers the "last N days" question without the three ways it is usually answered wrongly.

**The window is placed by a record, not by the clock.** The anchor is the newest day the
transcripts carry; `--until` supplies one instead, and the printed line says which
(`measured from the records this run read` vs `given, not measured`). A store whose newest
record is three weeks old therefore reports three weeks ago, instead of a "last 7 days"
window computed from today that would be empty — and an empty window reads as zero usage.

**A day with no record makes the total a floor, and the floor is printed.** The count of
days inside the window that carry no record is on the same line as the total
(`5 of 7 day(s) in the window carry a record; the total is a floor for those 7 days`), with
the missing dates listed. `VERDICT: window_measured (floor)`. A quiet day and a day the
meter never wrote look identical here, and the command says so rather than picking one.

**`--by-day` prints the window one day per row, and a blank day prints `no record`.** The
total alone cannot be audited: a reader cannot see which days it covers, and a `0` in a
per-day table would be the exact reading this command exists to prevent, so no row ever
carries one. The rows are the window's own day tables, not a second accounting — their sum
is the printed total by construction, and on the corpus they were checked against
`transcript_window`'s day table to the token.

**A window no day falls into prints no total at all.** `--until` can place the window where
the store has nothing (`VERDICT: no_records_in_window`), and a `TOTAL 0` there would say
"zero usage measured" when what happened is that nothing was read. The window and the anchor
are still printed, so the reader can see where the blank sits, and the day rows still name
every day in it — none of them as a number.

**Records with no timestamp belong to no window.** Their tokens are counted as *outside*
the window, beside the tokens on days before and after it, so the total is never presented
as the whole store. On the corpus this was built against that line carries 46 earlier days.

Per-model rows share one basis — non-cache-read tokens, named on every row — and `cache_read`
is printed inside the window but outside the share, with its share of the window beside it.
The raw record sum is printed next to the deduped total when repeated writes of one message
inflate it. Merge rule and day attribution are `nested`'s, so this window and that lifetime
total cannot disagree about the same corpus (there is a test that reads both and subtracts).

### `quiet` — which days are silent, and which only look silent

```bash
python3 quotamirror.py quiet
python3 quotamirror.py quiet --json
python3 quotamirror.py quiet --transcripts ~/.claude/projects --max-files 200
python3 quotamirror.py quiet --json > reading-a.json
python3 quotamirror.py quiet --compare-reading reading-a.json   # what changed since?
python3 quotamirror.py quiet --compare-reading a.json --compare-reading b.json  # walk a chain, oldest first
python3 quotamirror.py quiet --manifest --json > reading-a.json  # ...and name the file
python3 quotamirror.py quiet --manifest --store ./readings       # keep it; the next run finds it
python3 quotamirror.py quiet --manifest --store ./readings       # ...and compares against it
```

`recent --by-day` can say a day carries no token record. It cannot say *why*, and inside a
single store it never will: a holiday and a day the meter failed to write leave the same shape.
`quiet` does not try to guess from one store. It reads every day table this machine has — the
receipt's `dailyActivity` (messages per day), the receipt's `dailyModelTokens` (tokens per day)
and the transcripts' token days — and reports which side holds a record.

**A day the receipt counts messages on is not quiet.** That is the base rule. The receipt is
a positive statement of its own — it says 900 messages happened — so a day it names while
the transcripts hold nothing is never printed as a blank.

**A day the receipt bills is not unmetered.** `dailyModelTokens` states a token total per day.
When it has a row for a day the transcripts hold nothing for, "no token record" would hide a
number the receipt in hand carries, so the row says whose number it is:

```
  DAYS: measured 24 | receipt bills, transcripts silent 3 | activity without tokens 1 | tokens without activity 0 | silent 40
  totals      receipt 5,040,000 | transcripts 1,260,000 -- the two sides are not the same measure
  meter check  24 day(s) stated by both token meters (>= 1,000,000, tolerance 2%)
    receipt                 4,080,000
    transcripts as written  2,800,000  (every usage record)
    transcripts deduped     1,260,000  (one message counted once)
    receipt / as written 1.46x | receipt / deduped 3.24x
    evidence     14 of 24 day(s) exact, and on those the ratio is 1.00x | the 10 short day(s) carry the gap; the amount they are short by is the missing amount the witness line counts
    per day      0 of 24 day(s) agree with their own pair (tolerance 2%) | 24 receipt-heavy, 0 transcript-heavy | widest 4.20x (2026-09-03), narrowest 2.10x (2026-08-21) -- no_agreement
    by model     0 of 3 model(s) agree | 3 receipt-heavy, 0 transcript-heavy | ratio 3.00x..8.00x | largest gap claude-alpha (71% of the excess) -- every_model_receipt_heavy
    witness      14 day(s) exact | 10 short (2,780,000 billed, 1,280,000 missing) -- checked 31.9% of 4,080,000 billed tokens; exact where evidence survives
  meter check: neither -- neither transcript figure is within tolerance of the receipt, so repeated writes do not account for the gap on their own and the remainder is not explained here

  one side holds a record, the other does not:
    2026-09-03    900 message(s)  receipt bills 4,200 token(s)  transcripts hold none
```

Every figure above is invented to show the shape; a real run prints what its own two stores
hold. The billed tokens are counted separately (`receipt_tokens_billed`) and are never added
into the transcript side; the verdict is `billed_without_transcripts`, because a transcript
total inside such a window is a floor and not a measurement of it. A row that sums to zero, and
a receipt with no such table, do not upgrade a day: a zero states nothing.

**The verdict does not blame an edge it has not measured.** The window judged here is the
intersection of the two spans, so a day it reports on always lies between the first and the
last day the transcripts reach. Until 0.57.0 the reason sentence still said the corpus "does
not reach those days" — a cause contradicted by the `transcripts` span printed above it, since
the days it named sat inside that span. The sentence now counts the billed days that fall
inside the span (`receipt_billed_days_inside_span`) and says what the count supports: all of
them inside, the corpus reaches those dates and holds nothing for them, a hole inside the span
and not its edge; any of them outside, it prints the split instead of the claim.

**A window that ends before the corpus does says where it ends.** The window is the
intersection of the two spans, so a transcript day *after* the receipt's last stated day is
judged by nothing. On a real machine whose cache stopped a month before the transcripts did,
that left a month of activity off the end of the window with no line of the screen saying so:
the two spans above it were the only evidence, and a reader who saw a large silent count had
no reason to compare them. 0.60.0 counts those days, names their span and their token total,
and says the window ends before them:

```
  window      2026-05-03 .. 2026-05-25 (23 day(s))
  tail        2 day(s) after the receipt's last day (2026-05-28 .. 2026-05-31,
              1,200 token(s)), not judged: the window ends there
```

`days_after_receipt`, `transcript_tail` and `transcript_tail_tokens` carry the same three
facts in `--json`. They are not added to `days_in_window` and do not enter any verdict: the
receipt states nothing about those days, so nothing about them is claimed — they are outside
what this comparison can see. A cache that stopped a month early is exactly what a window
with no end noted looks like, and this tool exists to stop that reading.

**Two totals printed side by side invite a subtraction, so the difference is measured.** The
transcript corpus can differ from the receipt in one mechanical way a reader can check: a
streamed response may write its usage record more than once, so the same days hold a *record
sum* (every write) and a *deduped* total (one message, largest value per bucket). The
`meter check` block adds the receipt's own day table to both figures over the days both meters
state a number for, prints all three and the two factors, and returns one of
`record_sum_matches` / `deduped_matches` / `both_match` / `neither`. `neither` is a real
answer: it says repeated writes do not account for the gap on their own and leaves the remainder
unexplained rather than picking the nearest cause. Days below `--min-tokens` (default 1,000,000) are counted and do not
vote; `--tolerance` (default 0.02) is the width that decides a match, and both numbers in force
are printed with the verdict so the run can be repeated.

**The aggregate ratio states what it divides by.** `receipt / as written 1.46x` divides the whole
bill by the records that survive, so on a corpus with holes it is not a factor between two meters
— it is the bill over evidence. The `evidence` line splits the deciding days by the witness
verdicts printed below it (`14 of 24 day(s) exact, and on those the ratio is 1.00x`) and says where
the gap sits. The short-day gap is not printed twice: when the days that split counts are the same
days the witness line counts, the line points at that figure instead of repeating it, and it prints
its own number only when the two day sets differ — the witness axis is deliberately not
window-limited and the meter check is, so the sets have to be compared, not assumed.

**A matching total is not a matching day.** Two sums over the same days can agree while no
single day does — one day where the receipt is heavy and another where the transcripts are
cancel exactly. So the same block compares every deciding day with its own pair and prints
`N of M day(s) agree with their own pair`, the number of receipt-heavy and transcript-heavy
days, and the widest and narrowest factor with the date each came from. `every_day_agrees`
means the two meters count the same thing daily; `aggregate_only` means the totals match but the
days do not, so the match is arithmetic rather than agreement; `no_agreement` means the
aggregate did not match either figure and the labels are printed as counts only; `not_measured`
means no aggregate was available. The per-day axis never changes the aggregate verdict; it
stops a cancelled-out total from being read as one.

**A gap in one model can hide behind a gap in another.** Both axes above sum every model
together, so a model the receipt counts twice as heavily and a model it counts half as heavily
cancel and print as agreement. The same deciding days are therefore split by model name, with
each side's total, the factor between them, and the same tolerance: `N of M model(s) agree`,
the receipt-heavy and transcript-heavy counts, the range of factors, and which model holds the
largest single gap with that gap's share of the total excess. `every_model_agrees`,
`every_model_receipt_heavy`, `mixed` and `no_shared_model` name the shape; `largest_gap_share`
is what separates a residual sitting in one model from one spread across all of them, and a
factor range (rather than one factor) rules out a single scaling mistake on its own. A model
only one side names is listed as `receipt only` or `transcripts only` and counted by neither —
a renamed model and a model the other store does not meter leave the same hole, so the line
reports the hole instead of choosing a cause. On the corpus this was built against no model
agrees and every one is receipt-heavy, with the factor varying by model: the residual is not
one model's artifact, and it is still not attributed here.

**The ratio divides by the records that survive, so it says which days still have evidence.**
A factor computed against a corpus that lost records is not a meter factor. The `witness` line
therefore asks a different question of every day the receipt bills: does the corpus still hold
the usage records behind that day's total? Three answers are counted and kept apart —
`exact` (the receipt's day total equals the sum of the records, so the day is witnessed and the
two stores agree on it), `short` (the corpus holds some records and fewer tokens than the
receipt states, so that day's evidence is incomplete), and `absent` (no record at all: the
receipt states a number nothing on this machine can check). A fourth, `over`, covers the
opposite direction — the corpus heavier than the receipt — and is never folded into the
evidence holes, because that is a real disagreement and not a missing measurement. The line
prints each count with its billed tokens, the missing amount behind the short days,
`checked X% of N billed tokens`, and the widest short day with its factor:

```
    witness      2 day(s) exact | 1 short (1,000,000 billed, 800,000 missing) | 1 absent (400,000 billed, no record) -- checked 41.7% of 2,400,000 billed tokens; exact where evidence survives; widest short day 2026-09-03 5.00x
```

The verdicts are `every_day_exact`, `exact_where_evidence_survives`, `no_exact_day`,
`corpus_heavier_on_a_witnessed_day` and `no_day_above_floor`. The scope is every billed day
above the floor, deliberately not the window where both stores reach: a day the corpus never
reached is exactly the `absent` case, and intersecting first would drop those days. The axis is
the one place in this command that can say the ratio is arithmetic on a lossy denominator —
`checked_share` is that share — and it still names no cause for the missing records.

**The witness total says which days it leaves out.** A floor keeps small days out of the axis,
so `N` above is not always the number printed in the header. Synthetic shapes illustrate it: a
header stating 2,400,000 tokens on 4 days in the window beside a witness axis weighing
2,350,000 on 3 — the 50,000 difference sits on one sub-floor day, and before 0.56.0 nothing
said so. The sentence now names its own denominator — `above the 1,000,000-token floor
(1 day(s) holding 50,000 token(s) are below it)` — and the payload carries `min_tokens`,
`days_below_floor` and `tokens_below_floor`, so the two totals on one screen reconcile by
addition. When no day sits below the floor the phrase is absent, because then the witness
total *is* the header's bill and there is nothing to reconcile.

**On a witnessed day, repeats can be counted instead of guessed.** A day being `exact` is what
earns the right to compare the record sum with the deduped total: on that day the record set is
demonstrably complete, so the difference cannot be records that went missing — it is the same
message written more than once, which is how a streamed response lands in a corpus. The `repeat`
line prints that difference over the witnessed days only, with the deduped total and the number
of days kept out of this share:

```
    repeat       300,000 of 1,000,000 token(s) written on 2 day(s) are the same message written again (30.0%); deduped 700,000, 1 day(s) kept out of this share
    repeat/short 90,000 of the 200,000 token(s) 3 short day(s) hold dedupe away (45.0%) -- a rate over the records present, not a share of the 1,400,000 they bill
```

Two refusals sit behind it. With no witnessed day the line reads `repeat share not measured` and
says why: a dedupe over incomplete records is a floor on the repeat count, not a share of it. And
short or absent days are counted as excluded rather than pooled into the denominator, so the
number describes the days whose evidence survives and nothing else. The verdicts are
`repeat_measured`, `no_repeat_observed` — a measured zero, printed with the number of days it was
measured over — and `no_exact_day`.

**A short day's records still dedupe, so their repeat rate is measured too — against a different
denominator.** Refusing the *share* is not the same as having nothing to say: on a short day the
corpus holds records, and those records still collapse to distinct messages. That rate divides by
the records present, not by the tokens the receipt bills, so it gets its own line and never enters
the number above:

```
    repeat/short 90,000 of the 200,000 token(s) 3 short day(s) hold dedupe away (45.0%) -- a rate over the records present, not a share of the 1,400,000 they bill
```

The two numbers answer different questions and are printed apart on purpose. On the corpus this was
built against both rates land in the same broad band — a majority of the written tokens on the exact
days, and a majority of the surviving ones on the short days — so the repeat rate measured where the
evidence is complete is not an artefact of picking the good days, and a repeat whose first write
landed on a short day is no longer outside every number. (The measured figures stay in the run
report; the examples here are synthetic.) What stays outside is the *bill*: the short days' billed
tokens are still unattributed, and the line says so by naming the amount it is not a share of.
`absent` days hold no records at all, are counted in the block (`absent_days`) and contribute
nothing.

**A witness verdict is a statement about a reading, so the reading is printed.** Two runs of the
same code over the same window can print a different witness split with nothing in the payload to
say whether the corpus changed or the tool read a different corpus: a rotated log and a file cut
by `--max-files` leave the same shape. The `reading` line therefore states what this run opened
and parsed, in two digests, because one of them cannot carry the claim alone:

```
    reading      12 file(s) read, 4,210 usage record(s), 2.26 MiB -- the corpus this verdict read
                 digest 5c1d0a9b7e3f (path+size+mtime_ns)
                 content 9e02b41c77ad (file bytes)
```

`content` is a SHA-256 over each file's bytes. Comparing two payloads, equal `content` means the
same bytes stood on disk, so a moved total is a parsing change rather than a corpus change; a
different `content` means the corpus differed. `digest` over (name, size, mtime) is kept because it
is free and a moved one is still a reason to look, but it is **not** a statement about the bytes:
it stays equal when a file is edited and its mtime put back with the size unchanged, and it moves
when a file is only touched. On 40 real transcripts a two-hour mtime touch moved `digest` and left
`content` identical, and a one-byte token edit with the mtime restored left `digest` at its old
value while `content` moved. The `per_day` map in `--json`
then says how many files and records stood behind each witnessed day, which is what turns "that
day's record sum fell" into "three files held it last run, one holds it now". Two record counts are
kept apart by name: `records_parsed` is every usage record read, while a day's `records` is what
the day table attributes to it (a message streamed across midnight is billed on its first day, but
the file holding the second write still appears under the second day's `files`). Three limits are
stated rather than blurred: both digests name a reading on this machine only (names are local
facts), a corpus that is being written while the scan runs changes them by design, and with no
scan in hand the block is `no_reading` and says so instead of printing zeros.

**Two readings can be compared, and the pair is classified.** A single reading cannot say whether
the corpus moved, and two digests in one payload only answer it for a reader willing to do the
comparison by hand. `--compare-reading PATH` takes a saved reading (a `quiet --json` payload or
its reading block) and the run prints one verdict instead:

```
                 prior    12 file(s), 4,210 record(s), 2.26 MiB
                 change   files +0, bytes +0, records +0 (this run minus the supplied one)
                 verdict  metadata_only -- no byte moved; only (name, size, mtime) did --
                          a touch, not an edit
```

The five classes are decided by the pair, never by one side: `same_reading` (both digests equal),
`metadata_only` (content equal, path digest not - no byte in the corpus moved, so the corpus was
not edited and what moved is (name, size, mtime)), `bytes_differ` (same file count and same byte
total, different content), `size_differs` (same file count, a different total) and
`file_set_differs` (a different number of files). Three refusals are verdicts too: `no_prior` when
nothing was supplied, `no_current_reading` when this run holds no reading, and `not_comparable`
when the supplied block has no byte axis (a payload from before 0.48.0) or when the two
contradict each other - two readings produced under the same rule cannot hold the same content
digest with a different file or record count, so such a pair is one of them edited or taken under
another rule. Without the flag the line reads `compare not measured -- --compare-reading was not
given`; a comparison that did not happen is never printed as agreement.

**More than two readings are walked, not just paired.** `--compare-reading` may be given more
than once, and the readings are then taken oldest first: each neighbouring pair gets the same
five words, and the chain says which pair is the one that moved. A chain can say what a pair
cannot - a hole in the middle is named by position instead of being skipped, so three readings
never quietly compare as two, and a return to bytes already seen is counted (`repeated_pairs`)
rather than attributed, because an edit that was undone and another tree with the same bytes
leave the same evidence. What it cannot say is when: no reading carries a timestamp, so the
order is the order handed in and every payload prints that sentence.

```
    chain        3 reading(s) walked oldest first, 2 neighbouring pair(s)
    order         no reading stamps when it was taken, so the order below is the order
                  handed in (oldest first) and is the caller's claim, not a measured sequence
    1 -> 2  file_set_differs (files -20, bytes -52447423, records -2994)
    2 -> this run  size_differs (files +0, bytes +143830, records +20)
    chain verdict bytes_moved -- at least one neighbouring pair holds different
                  bytes; 2 of 2 pair(s) moved bytes
```

**A reading can be kept, so the next run has a prior without a file to save.** `--store DIR`
writes this reading into `DIR` and, on a later run, uses the newest stored reading for the same
corpus as the prior — the chain no longer depends on remembering where the last JSON went:

```
    store        2 reading(s) held for this corpus in ~/readings
    saved        this one written as 6f1a2c9d7e30-20261127T072231Z.json
    stored       the prior above is 6f1a2c9d7e30-20261120T061455Z.json,
                 taken 2026-11-20T06:14:55Z
```

The corpus key is a SHA-256 digest of the resolved transcript root, so `~/x` and an absolute
path to the same directory share one store, and two different corpora do not. One file per
reading, named `<key>-<UTC stamp>.json`, written at mode `0600` through a temporary file, with
the newest five kept per corpus and older ones pruned — a short recent history is what a chain
needs, and an unbounded store would be a corpus of its own. What goes to disk is the reading,
never its verdicts: the comparison block embeds the prior's whole manifest, so storing it would
make each file record its own comparisons and double in size. A stored file that cannot be read
as a reading is skipped and counted (`1 stored file(s) could not be read and were skipped`); it
is never deleted, never treated as the newest reading, and never fails the run — a convenience
that breaks is not a reason to refuse the measurement in hand. A store directory too long to fit
the line is shortened from the left with `...` and says `(full path in --json)`, because a path
has no spaces to fold on. `--store` requires `--manifest`,
because a reading that cannot name a file cannot be chained; `--compare-reading` wins over the
stored prior when both are given, and the payload says the store was not used.

**A verdict says the corpus moved; a manifest says which file.** The two digests are one-way, so
naming a file needs the per-file facts both readings must carry. `--manifest` puts them in the
reading - name (relative to the transcript root), size, mtime_ns and a sha256[:16] of the bytes,
for every file read - and the comparison then prints a `named` line beside the verdict:

```
                 verdict  metadata_only -- no byte moved; only (name, size, mtime) did --
                          a touch, not an edit
                 named    touched 1: <project>/<session>.jsonl
```

Five lists come out of the pair: `touched` (same bytes, different size-or-mtime), `edited` (same
name, different bytes), `moved`, `added` and `removed`. That is the row the verdict alone cannot
produce - a rename and an in-place edit of the same length both read as `bytes_differ`, and only
the name lists tell them apart. A rename is the case that needed its own list: it arrives as one
`added` plus one `removed`, which is the same shape a file added beside a deleted one takes, so
the manifest's size and content hash pair the two names and the line reads
`moved 1: <old>.jsonl -> <new>.jsonl`. The pairing is only made when it is forced - two files
with identical content on either side make every pairing a guess, and those names stay in
`added`/`removed` with a second line saying how many were left unpaired. A move is never also
counted as a file that appeared: the three lists partition the changed names, so a name is
printed once. Naming refuses in six cases rather than name part of a corpus: neither
reading has a manifest, this run has none, the supplied one has none, either manifest hit the
2,000-file cap, a manifest disagrees with its own file count, or a manifest's reading never
said how many files it read at all. That last one is not pedantry: a missing file total used to
be filled in from the length of the list, so a reading taken by an older release - or passed
through anything that dropped the field - was named from it as if its list were the whole corpus.
An absent statement is not a whole one, so it now refuses instead. Each prints its own
sentence. The cost is stated rather than hidden: on a 940-file corpus (2.35 GiB) the payload grows
from 29.7 KB to 235.9 KB, about 219 bytes per file, and the digests are unchanged by the flag - a
manifest adds names, not identity.

**Five states, five names, no zeros.** `measured` (both sides count), `receipt bills,
transcripts silent` (the receipt's token table states the day, the transcripts do not reach it),
`activity without tokens` (messages counted, no table states a token number), `tokens without
activity` (transcripts yes, the message table has no row — it lags) and `silent` (no side). The
fifth term is printed only when it is non-zero, so the terms still add up to the window; a
silent day is printed as an absence and carries no number.

**The two totals are printed and not reconciled.** `measured` says both stores hold a record on
a day — not that they agree on the amount, and on the corpus this was built against the two
totals differ by a multiple on the very days both reach. Both are printed side by side with
that sentence, so neither can be read as the window's usage; matching a day's two numbers is
`compare`'s and `crosscheck`'s job, not this command's.

**The window is the intersection of the two spans.** A day only one store reaches cannot be
called silent on both, so it is counted outside the window rather than judged; a day the
receipt's token table states belongs to the receipt's span even when it has no message row. The
count of excluded days is its own line — `outside     9 day(s) are reached by only one store
- not judged here` (synthetic figure) — because the state counts add up to the window only, and
the printed arithmetic would otherwise look complete while those days had been set aside.

**Nothing to compare is not a clean bill.** A receipt with no `dailyActivity`, a transcript
directory with no usage record, or two spans that do not meet print `not measured` with the
reason and no counts (`no_day_table`, `no_transcripts`, `no_overlap`). A receipt that carries
per-day token totals and no message table says so rather than claiming it states nothing. A
zero there would be a claim the run cannot make.

Measured totals are read from each side's own table — `measured_tokens` is the sum of the
scan's day entries and `transcript_tokens_in_window` is the same number by construction, so
this command cannot become a second accounting of the same corpus.

**No line is wider than the screen.** Since 0.55.0 every line `quiet` prints fits in 80
columns. Before that only the chain's rows were wrapped: the rest of the screen was assembled
as whole lines, and on a real corpus 22 of the 45 lines a run printed were wider than a
terminal, the widest 254 — a figure off the right edge is a figure the reader cannot check.
A row that has to fold keeps its label column and continues under the column where its first
value started, a break moves back to a nearby `|` or `--` rather than landing mid-sentence,
and a break is never placed between a token carrying a figure and its neighbour, so `silent
138` and `2.44 GiB` stay on one line. Only whitespace moves: the words are the same words in
the same order, and the payload — and therefore `--json` — keeps the full sentence, because
folding is a rendering step and not a measurement. Folding is the fallback, not the plan: a row
that only fits by being broken leaves its date on one line and its absence on the next, so the
per-day billed row states its own figure in one line (`991,588,155 token(s) billed, no
transcript`, 76 columns on the corpus that used to be 91). What it cannot do: a row longer than
the screen still occupies more than one line, and a token longer than the screen is printed whole
rather than truncated.

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
8. **A zero declares which zero it is.** `store_empty`, `no_usage_records` and `usage_observed` are separate states, and a coverage sentence is not printed about records that were never read. Every overlap obeys the same rule — the two harnesses' day tables and Claude's two counters alike: `shared_days`, `disjoint_windows` and `no_shared_day` are different statements, so `in both : 0` carries the reason it cannot be anything else. `DAY CROSS-CHECK` follows it too: its verdict `no_overlap` means only *this check could not judge*, and a `coverage` field says which case it was — `all_below_floor` (shared days exist but none reaches `--min-tokens`), `disjoint_windows` / `no_shared_day` (the two ranges share no day), `no_days` (one side produced no day at all, printed as `not measured` with no one-sided count) or `not_read` (the flag was never passed). A section that has two possible tables also names the one it read: with `--claude-transcripts`, `compare` prints DAY OVERLAP twice — `claude side: cache day table` and `claude side: transcript record dates` — because the cache's declared days and the measured record days can land on opposite sides of the Codex window, and one block labelled plain `claude` would speak for both.
9. **Read-only toward harnesses.** The tool never changes vendor caches or transcripts.
10. **A decrease is a revision, not less usage.** A monotone counter read twice can only read lower if the record was rewritten.
11. **A list says how many items it did not print.** Every printed list has a count on the same line, so a list that simply stops reads as the whole list of a count it contradicts. One sentence covers every site — the first N items and, only when it truncated, `+N more`: `38 day(s) are listed by the cache with no transcript here: ... +30 more`, `claude only : 42 (... +36 more)`, `appeared in this reading only (6): ... +2 more`. Before this, five sites used three conventions — a bare `...`, no marker at all, or eight dates under the word `38` — and the last two read as complete.
12. **A record can be contradicted by the next read.** Confirming a narrowing makes the narrow counts the record; a read that then has the carrier back is a measurement about that record, not a no-op. It prints `REGAINED (n)` with both counts, and says only what it can support - the field is here, not who put it back.
13. **An unreadable receipt is one sentence, and there are three ways to be unreadable.** A file the loader cannot parse used to surface the decoder's own words — `Expecting value: line 1 column 1 (char 0)` — which names neither the file nor its size, and cannot distinguish a receipt that was never written from one caught mid-write. Now: `is empty (N bytes) - a receipt that was never written, or one that is being rewritten right now`; `is not valid JSON (… at line L column C, N bytes read) - a receipt caught mid-write looks like this, and nothing is claimed about the tokens it would have held`; `is not UTF-8 text (byte 0xNN at offset K, N bytes read)`. All three exit 2 and print no count, because a file that could not be parsed is not a receipt that held nothing.

## Privacy

- All processing happens locally.
- Transcript text and tool arguments are never returned in reports.
- `loop` hashes arguments in memory and stores no argument values.
- `drift` stores a format-version marker, normalized field paths, type names, per-family presence counts (numbers of models, days or list members, never values), how many reads produced each shape, declared version keys, the wall-clock time each shape was seen, and a non-reversible path digest. It does not store absolute receipt paths, model names, counters, or receipt values. Writes use atomic replacement with mode `0600`, and symlink store destinations are refused.
- `revise` stores per-reading token numbers and model IDs under a non-reversible path digest, keeps the newest five readings, and writes with mode `0600` through atomic replacement (symlink destinations are refused). It stores no absolute paths, prompts, or transcript text; `--no-record` writes nothing at all.
- `quiet --store` stores one file per reading, holding the reading block and the counts a comparison needs — file, record and byte totals, both digests, per-day file names, and the `--manifest` (name, size, mtime, short hash) when one was asked for. It stores no prompt, tool argument, or message text; the only paths it keeps are the transcript root (with the home prefix replaced by `~`) and the file names relative to it. Readings are keyed by a SHA-256 digest of the transcribed directory, kept at five per corpus, and written at mode `0600` through atomic replacement (a symlink store is refused). Without `--store` `quiet` writes nothing.
- Human and JSON reports replace the current home-directory prefix with `~` by default. Explicit paths outside the current home are left unchanged.
- Tests and documentation contain synthetic fixtures only.
- This repository contains no personal usage totals, captured local usernames, prompts, credentials, or transcript content.

The process still opens local files that may contain private text. Run it only against data you own.

## Exit codes

- `0`: report produced
- `2`: unreadable input, unsupported shape, missing required path, or invalid argument. A bare
  `quotamirror` with no receipt at the default path also exits `2`: it found nothing to describe.

Individual commands may expose additional refusal states in their JSON payloads; they do not silently convert unavailable data to zero.

## Tests

```bash
uv run --with pytest pytest -q
```

The suite covers receipt reconciliation, unit and cumulative checks, loop deduplication, schema drift, two-reading revisions, cross-harness witnesses, polling shares, nested scope, trailing-window tokens (including agreement with `nested` on the same corpus), day-level cross-checks, arbitration between record-sum and message-id-merged totals, that every command can format its own `--help`, and that every option carries a description naming its own default.

## Limitations

- `recent` places its window by the newest recorded day, so it measures the last N days *of the store*, not the last N days of wall-clock time; a store that stopped being written three weeks ago reports three weeks ago. Use `--until` to place the window where you meant it, and read the anchor line to see which was used.
- `recent` cannot tell a quiet day from a day the meter failed to write: both are days with no record, both make the total a floor, and both are printed as `no record`. `--by-day` makes each one visible as a row instead of a date in a comma list, and it makes a run of blank days obvious, but it cannot label them - a store only holds what it wrote, so a holiday and a broken meter leave the same shape. A truly empty window prints no total at all rather than `0`. `quiet` is the one place this is separated, and only where two independent stores both reach: a day the receipt counts messages on is not quiet, whatever the transcripts hold. Outside that intersection nothing is claimed.
- `quiet` does not say *why* a day carries messages and no tokens. It separates the days the receipt's own token table bills — the number is stated, the transcripts simply do not reach those days — from the days no table states a number for, and refuses to call either quiet. The cause itself (pruned transcripts, a client that writes none, an unmetered day) is still not decidable from the stores, and a transcript total over a window containing billed days is a floor. The `meter check` block does measure the *shape* of the gap on days both meters speak (record sum, deduped, or neither), but it can only rule the repeated-write explanation in or out — when it returns `neither`, the remainder is not a meter factor but the records the corpus no longer holds — the `evidence` line splits the ratio by the days whose records survive — and this command still names no cause for it. The `witness` line narrows that remainder without attributing it: on this machine the receipt's day total equals the sum of that day's surviving usage records exactly on every day the corpus still holds them (verified by a recount outside the tool), while the corpus as a whole holds far fewer written tokens inside the receipt's span than the receipt states — so the factor is arithmetic over a lossy denominator, the missing amount is measured, and why those records are gone (pruning, a rotated log, another machine) is not decidable here. The per-day axis says whether agreement holds day by day, not why it fails: a day whose transcript total is far smaller than the receipt's own number is printed, but the reason for that one day (pruned transcripts, a rotated log, a partial write) is still outside what the two stores state. The per-model axis has the same limit in another direction: it can say the gap is spread across every model and that no single factor explains it, but it cannot say what moves the number. A model renamed between receipt versions splits into a receipt-only and a transcript-only row, which is listed rather than merged — merging on a similarity guess would invent an identity nobody wrote. A witness verdict is also a statement about one *reading*: the corpus is mutable, and a day that measures `exact` can measure `short` on a later run of the same binary over the same window. The `reading` line and its `per_day` map are what make those two runs comparable — equal `content` digests mean the same bytes were read, so a moved split is a parsing change rather than a corpus change; the (name, size, mtime) `digest` beside it is cheaper but cannot carry that claim, since an edit that keeps both size and mtime leaves it equal and a touch moves it. What the line cannot do: both digests are local (names are facts about this machine), they change while the corpus is still being written, and `per_day` says *which* files hold a day, not why a record that used to be there is gone. The cause of such a drop (files removed, a rotated log, a read that reached less) is still not decidable from the output.
- `quiet --compare-reading` may be repeated, and a sequence is walked rather than only its last pair: the readings are taken in the order handed in (oldest first), each neighbouring pair is classified with the same five words, and the chain names the pair that moved. Nothing inside a reading stamps when it was taken, so the order is the caller's claim — every payload prints that sentence, and a sequence handed in out of order yields a chain that is true of that order and not of the corpus's history. A reading with no byte axis is a named hole instead of a skipped row: the pairs beside it are reported as not compared and the chain verdict is `hole`, because a three-reading chain that quietly compares as two would misplace the step that moved. `repeated_pairs` counts positions whose bytes match with a different pair in between — a change that was undone, or another tree holding the same bytes, which the readings cannot separate; a pair whose counts contradict its equal digest is counted as a contradiction rather than as a return. Without `--store` the command writes nothing at all, and the caller keeps the JSON — so a fourth reading nobody hands in stays outside every verdict. With `--store` the newest five readings per corpus are kept and the previous one is used as the prior automatically; a stored file cannot say whether it was taken before or after another, so a store returns the newest by its own name and within one store the sequence is real rather than claimed. `--manifest` names the files behind a change, but only when *both* readings carry one and neither hit the 2,000-file cap; a comparison without manifests reports counts and refuses to name, and the cap makes a name possibly missing, so a capped manifest refuses rather than name a subset. A rename is named as a move only when the pairing is forced by (size, sha256[:16]); two files with identical content on either side leave those names in `added`/`removed` and print how many could not be paired, so a corpus that holds the same bytes under several names - copied or resumed transcripts - is counted but never guessed at. A manifest is also a second copy of facts the digests already cover, which is why it is opt-in and roughly 219 bytes per file in the payload. The names are relative to the transcript root, so the digests still compare equal for a corpus copied from another machine with the same relative layout — identity of location is never tested — and a manifest moved to a machine with different mtimes names every file as `touched` until the two readings come from one place.

**A chain says which step a file entered or left.** The pair rows say whether bytes moved between two readings; they cannot say which file did, and a 2000-entry manifest does not fit on a line. When every reading in the chain was taken with `--manifest`, the chain also walks the names: each file is counted as `in every reading`, `entered` (first appears on reading *k*), `left` (last seen on reading *k*), or `blink` (gone and back, which is not the same as gone). The text prints up to four changed names with their step numbers, shortens a name that cannot fit while saying that the full name is in `--json`, and refuses to name anything at all when a reading carries no manifest, when one is truncated at the 2,000-file cap, or when a manifest disagrees with its own file count or when its reading never stated one at all - a partial list read as a whole one would report a file that never left as `left`. It follows names *and* pairs them: at each neighbouring step a name that vanished is paired with one that appeared carrying exactly the same bytes, using the same (size, sha256[:16]) rule the pair rows use, so a rename is counted once instead of as one departure plus one arrival. The pair is only made when the match is forced one-to-one and only between neighbouring readings that both carry a manifest — two files with identical content print `the match is not one-to-one` and are counted in `renamed_ambiguous_total`, and a name with no content hash is never paired. A rename that also edits its bytes stays unpaired and reads as one name leaving while another entering, which the limit line now says in those words. The per-name counts are left alone so they keep summing, each row also carries `renamed_to`/`renamed_from`, and the text prints up to four `renamed a.jsonl -> b.jsonl on reading 2, same size and content hash` lines (names shortened for the screen, full names in `--json`). For example, with one file renamed between readings 2 and 3 of a three-reading chain:

```
    names         15 file name(s) across 3 reading(s): 13 in every reading, 1
                 entered, 1 left, 0 blinked
    renamed       0d6af2d3-…-8be988.jsonl -> renamed-0d6a.jsonl on reading
                 3, same size and content hash -- the name that left and the
                 one that entered are this one file
    left          0d6af2d3-…-8be988.jsonl -- last on reading 2, absent on
                 reading 3, so this file is now named renamed-0d6a.jsonl
    entered       renamed-0d6a.jsonl -- entered on reading 3, the last
                 reading, renamed from 0d6af2d3-…-8be988.jsonl
```

The two rows a rename leaves behind name each other in the same words the renamed line uses: read alone, `left` beside `entered` is exactly the departure-plus-arrival reading the renamed line exists to prevent, and a reader who stops at those rows would take it. Both sentences come from `renamed_to`/`renamed_from` already in `--json`, so the screen and the payload cannot disagree about which two names are one file.

Where a name *stayed*, though, the name is its own identity, so the manifest's content hash is compared step by step: a file edited in place is named with the step its bytes changed (`edited_at`), the summary line counts it (`N edited in place`), and a chain whose names all held while bytes moved answers `names_held_content_moved` instead of `names_stable`, which would be an all-clear printed beside a pair row that found different bytes. A name whose hash is missing on either side of a step is counted in `content_unmeasured` and never called edited — an absent hash is not a hash that matched. The names are relative to the transcript root, as the manifest already is.

- `recent` measures tokens, never a share of a plan allowance: no quota ceiling is in the store, and none is assumed.
- `quiet`'s `repeat/short` rate is measured over the records a short day still holds, and those records may not be a representative sample of what the day wrote: if the missing records were themselves the repeats, this rate understates the day's repeat share, and if they were unique it overstates it. The command prints the amount it divides by and the bill it is not a share of, and it does not correct for the unknown mix — a correction would be a guess wearing a number.

- Receipt field names and shapes are harness-specific and can change.
- Token values are trusted as written; this is not provider-side billing verification.
- No price table is bundled, by design.
- Exact-argument repeat detection is a floor, not proof that repeated work was wasteful.
- Message-id merging assumes one ID represents one response.
- Timestamp windows are bounded by records with usable timestamps.
- Scope depends on the harness exposing root/subagent metadata.
- A schema field that keeps the same name and type but changes meaning is invisible to `drift`.
- `drift`'s within-version axis sees only the shape sitting in the store, so a change made and reverted between two runs is missed, and the first run at a version has nothing to compare against.
- Confirmation takes two runs in every direction now: a deletion seen once at one version, a deletion or a narrowing seen once across a version change, a narrowing seen once at an unchanged version, and a baseline recorded from one read that a later read outgrew are all reported as suspicions, never as verdicts. The cost is that the first run after a real vendor upgrade prints `removal_unconfirmed` or `narrowing_unconfirmed`, and the first run after a same-version narrowing prints `possible_narrowing`, each asking to be run again - that is the tool refusing to guess, not a missing measurement.
- The same-version narrowing guard is confirmed on the counts, not on the shape: the path set is byte-identical before and after, so the challenge is a digest of the rows. A vendor that narrows in two steps (`9 of 9`, then `8 of 9`, then `7 of 9`) therefore needs two agreeing reads at *each* step, and a receipt whose counts move on every read is never confirmed. Two identical readings are also what a cached stale copy of an earlier read would produce, so repetition alone cannot distinguish a vendor from a reader stuck on old bytes.
- Presence now speaks in both directions at an unchanged version, but on one axis only. A record can legitimately be a narrow shape, and a later read that regains the carrier prints `REGAINED (n)` with both counts instead of `unchanged` (until 0.32.0 it printed the all-clear). Three limits remain: the return is *described*, not explained - a stale copy of the older bytes and a record that was wrong look identical, so nothing says the vendor restored the field; the family size must be unchanged for a row to count, so a carrier that arrives with a new model is not called a return.
- The cross-version axis now has a *regain* policy, and it is deliberately a weaker claim than the same-version one: a field the other version's record held on 8 of 9 models and this read holds on 9 of 9 prints `REGAINED ACROSS VERSIONS (n)` with both counts and the baseline's read count, instead of the ordinary difference it used to report. It is a different word because it is a different statement - the older record only had fewer carriers, and a record written while the file was being rewritten has the same two counts as a narrowed one. So nothing here says the vendor restored the field, and a baseline with no read count says so out loud. The family-size guard still applies: a carrier arriving with a new model is not a return. Like the rest of the drift axis this is only exercised by fixtures on this machine, because a second receipt version has never appeared here.
- `reads` counts reads of one exact shape at one version, and a *short* baseline that every later read agrees with stays short: if the tool never sees the fuller shape, nothing distinguishes a short record from a vendor deletion. A baseline written by an older build has no read count and is not reinterpreted.
- Presence counts members, not values: a field that all 41 days carry and 40 leave at 0 reads exactly like a field every day fills in.
- A presence count cannot tell a dropped field from a member that left the window: `dailyActivity[]` keeps a rolling set of days, so a day falling out of it lowers the count the same way a vendor removing the field would. Two lists under different keys also fold into one count, so `2 of 8` can mean 1 of 3 plus 1 of 5.
- `revise` only sees the readings it recorded: a revision made before the first reading, or after the latest, falls outside both snapshots. Two readings also cannot separate new work from a late write onto an older day.
- `observation` describes what this run read, not what the harness wrote: a transcript directory that was moved, or a `--max-files` cap that excluded everything, reads as `store_empty` or `no_usage_records` even when the harness holds usage elsewhere. The two are distinguished because one is "nothing here" and the other is "these files had no usage", but neither proves the harness never used a model.

## License

MIT — see [LICENSE](LICENSE).
