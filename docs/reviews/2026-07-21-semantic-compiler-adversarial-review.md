# Adversarial review: semantic compiler — 2026-07-21

Scope: `src/wh/metrics/` (compiler, context_ops, loader, checks, timegrain,
result, provenance) at `d2733be`. Baseline: 363 passed, 3 skipped. Every
finding below was **confirmed by executing a repro** against in-memory DuckDB
(same version the suite runs); nothing here is speculative. Findings are
ordered by severity. Part 2 covers architectural improvements with reasoning.

Classes already fixed in the 2026-07-20 adversarial rounds (fytd
contamination, month-end clamping of *date* bounds, datetime-vs-date `_lit`
ordering, YAML identifier injection, hash projection) were re-checked and
remain fixed — the findings below are new.

---

## Part 1 — Findings

### F1 (HIGH): dim-key uniqueness is checked for only the FIRST dim on a shared table

`bind_checks` dedupes on `dim.table` alone (`checks.py:27-30`), but two
shared dims may role-play the **same table through different key columns**.
The second key column's uniqueness is never checked, and a duplicated key
silently fans out the join — the exact silent-inflation failure this check
exists to prevent, on the exact models (role-playing dims) most likely to
hit it.

Repro:

```python
# dim_fac: fac_sk UNIQUE, prov_code DUPLICATED (two rows with prov_code=9)
# dims: facility -> dim_fac.fac_sk, provider -> dim_fac.prov_code
con.execute("CREATE TABLE dim_fac (fac_sk INT, prov_code INT, fac_name VARCHAR)")
con.execute("INSERT INTO dim_fac VALUES (1, 9, 'a'), (2, 9, 'b')")
con.execute("CREATE TABLE f (d DATE, fac_sk INT, prov_code INT)")
con.execute("INSERT INTO f VALUES (DATE '2026-01-05', 1, 9)")
bind_checks(con, model)               # -> [] : passes, no warning
# slice(["n"], by=["provider.pname"]) on ONE fact row:
# [('a', 1), ('b', 1)]  — count(*) doubled by the unchecked join
```

Fix: key the `seen` set on `(dim.table, dim.key_column)`, not the table.
One-line change; the check itself already does the right thing.

### F2 (HIGH): unknown YAML keys are silently ignored — typos silently change semantics

The loader validates the keys it knows and ignores everything else. A typo
therefore doesn't fail — it silently produces a *different model*:

```yaml
census:
  fact: f
  snapsot: true                       # typo for snapshot:
  time: {column: d, cadense: daily}   # typo for cadence:
  measures:
    beds: {description: occupied beds, expr: "sum(1)", wear: "1=1"}  # where:
```

Loads cleanly as: `snapshot=False, cadence=None, measures['beds'].where=None`.
The stock model silently becomes an event model (no as-at join — every
snapshot day double-counts), cadence-aware completeness silently degrades to
the weaker max-date rule, and an intrinsic predicate silently vanishes from
a measure's identity. All three produce *plausible wrong numbers* with no
error anywhere — `wh validate` passes. This is the worst failure class the
layer has (the design's own "silent-NULL failure class" reasoning applies:
governed numbers must fail loudly or be right).

Fix: after parsing each mapping (model, `time:`, measure, dimension), reject
unconsumed keys with the usual one-sentence error naming the nearest valid
key (`difflib.get_close_matches` makes "did you mean 'snapshot'" one line).
See Part 2A.

### F3 (MED-HIGH): datetime upper bounds resurrect the month-end clamp bug in compare shifts

`_shift_op` (compiler.py:51-63) applies the exclusive-bound trick — the
round-2 fix for month-end day-clamping — **only when `op.hi` is a plain
date**. A datetime hi is shifted directly through `_months_back`, which
day-clamps:

```python
_shift_op(Between(datetime(2026,6,1), datetime(2026,6,30,23,59,59)), 1, 0)
# -> Between(2026-05-01 00:00, 2026-05-30 23:59:59)   May 31 DROPPED
_shift_op(Between(date(2026,6,1), date(2026,6,30)), 1, 0)
# -> Between(2026-05-01, 2026-05-31)                  correct
```

Every `prior`/`yoy` value for the prior period is then missing its last
day — precisely the silent under-count the round-2 fix documented and
closed for dates. Datetime bounds are reachable: `time=(dt_lo, dt_hi)`
passes coercion untouched, and `wh.last` on a TIMESTAMP time column
*produces* datetime bounds (F5).

Fix: shift the exclusive instant for datetimes too — `hi_excl = hi + 1
microsecond` is wrong (arbitrary); the clean form is to carry the bound as
exclusive internally (Part 2D). A local fix: when `hi` is a datetime, shift
`(hi_date + 1 day)` and recombine `hi.time()` onto the clamped result only
if the date didn't clamp — but that's exactly the fiddliness Part 2D removes.

### F4 (MEDIUM): no output-column uniqueness — three silent-duplicate paths

The compiler never checks that its output column names are distinct, and
DuckDB happily returns duplicate names. Confirmed paths:

1. **Measure name == local dim name.** Loader validates dims and measures
   separately; `_explain_representative_query` doesn't catch it (EXPLAIN
   accepts duplicate aliases).
   ```python
   # dims: {n: cat}, measures: {n: count(*)}; slice(["n"], by=["n"])
   # SELECT ..., fact.cat AS n, count(*) AS n ...  -> columns: [period, n, n]
   ```
2. **by= entry colliding with a generated comparison column.**
   `_check_compare` guards `{measure}_{cmp}` against *declared measures*
   only. A local dim named `n_prior` with `measures=["n"],
   compare=["prior"]` yields `columns: [period, n_prior, n, n_prior]` —
   one is the dim, one is the prior value. (Confirmed.)
3. **Duplicate entries in `measures=` / `by=`** (same class, no dedup).

Downstream, duplicate names poison the Arrow table: polars raises, pandas
mangles, and a notebook user gets whichever column their tool picks.

Fix: one place, not three — collect the final projection's aliases in
`compile_slice` and raise on duplicates (Part 2B). That closes all current
paths and any future one for free.

### F5 (MEDIUM): `wh.last(n, "day"/"week")` on a TIMESTAMP time column silently drops the first day's early rows

`Context.resolve` promises anchoring "against the mirror's max **date**",
but the anchor is `max(time_column)` verbatim — a datetime on TIMESTAMP
columns. The day/week units then do raw timedelta arithmetic, producing a
**mid-day lower bound**, while month/year units go through `_months_back`
and produce a date:

```python
ctx = wh.context(time=wh.last(7, "day"))
ctx.resolve(anchor=datetime(2026, 7, 15, 9, 30)).entries["time"]
# Between(2026-07-09 09:30, 2026-07-15 09:30)  <- rows before 09:30 on
#                                                 Jul 9 silently excluded
wh.context(time=wh.last(1, "month")).resolve(anchor=datetime(2026,7,15,9,30))
# Between(date(2026-06-16), datetime(2026-07-15 09:30))  <- full first day
```

So "last 7 days" and "last 1 month" disagree about whether day one is whole,
and both disagree with the docstring. It also feeds datetime bounds into
compare shifts, arming F3.

Fix: floor the anchor to a date once at the top of `resolve()`
(`anchor.date()` if datetime). `hi = anchor_date` then compiles through the
existing `< hi + 1 day` day-inclusive path, which is the documented
contract. One line plus a test.

### F6 (LOW): `wh.last(0, ...)` and negative n accepted — silently empty or future windows

`last()` coerces `int(n)` with no bound check:

```python
wh.last(0, "day")    # resolves to Between(anchor+1, anchor)  — always empty
wh.last(-3, "month") # resolves to Between(anchor+3mo, anchor) — "future" window
```

Both compile to a silently-empty result. `n=0` is a plausible off-by-one in
notebook code (`last(n_periods)` from a widget). Fix: `n >= 1` check in
`last()` with the usual one-sentence error.

### F7 (LOW): measure exprs can depend on non-fact tables through scalar subqueries

`_check_fact_only_refs` walks `column_names` nodes, so a subquery that
references another table **by count or by columns that happen to share a
name with fact columns** passes; the aggregate check passes too (the outer
expr is still an aggregate):

```python
Measure(expr="sum(x) + (SELECT count(*) FROM other)")
bind_checks(con, model)   # -> []  (accepted)
```

This breaks the stated invariant ("identity lives in git; dimension state
does not" — a definition now mutates when `other` changes, under a stable
measure hash). YAML is curated so exploitation isn't the concern;
hash-integrity drift is. Fix: in the same parse-tree walk, collect base
*table* references (`BASE_TABLE` nodes) and reject any relation other than
the fact — cheaper and stricter than chasing column provenance.

### F8 (LOW): the "unfiltered" options list depends on how you became unfiltered

`compile_values` reads the **dimension table** when `ctx.entries` is empty,
but a context whose entries are all `All()` — which is exactly what an
untouched (empty-selection) widget resolves to — takes the fact-scan lane:

```python
values("fac.name")                                  # ['never_in_fact', 'used']
values("fac.name", ctx_of_one_empty_widget)         # ['used']
```

So a cascaded `filter_dim` downstream of an *untouched* upstream widget
shows a different option list than the same widget standalone, and the
difference (dim values with no fact rows) appears/disappears as the user
toggles an unrelated filter in and out of its empty state. Not wrong so
much as unstable. Fix: drop `All()` entries (and entries that only carry
`All`) before the emptiness test in `compile_values`, so "effectively
unfiltered" and "unfiltered" take the same lane.

---

## Part 2 — Architectural / design improvements

### 2A. Strict-schema YAML loading (closes F2 structurally)

Add a tiny helper used by every `_parse_*`: pop known keys from a copy of
the mapping, and if anything remains, raise naming the leftover and the
close match. ~20 lines total.

**Pros:** eliminates the layer's worst silent-wrong-number class (F2) at
the source; error quality matches the house style; makes every *future*
key addition typo-safe for free; zero runtime cost after load.
**Cons:** YAML files gain no forward compatibility — a file written for a
newer wh fails on an older one. That's the right trade for a *governed*
definitions file: the alternative is an old binary silently computing
different numbers from definitions it half-understands, which is the exact
failure governance exists to prevent.

### 2B. A single output-namespace check in `compile_slice` (closes F4 structurally)

The projection is assembled in three places (plain, wrapper, compare), and
collisions are currently guarded by scattered spot checks (`_RESERVED`,
`_check_compare`'s measure-vs-measure test). Instead: build the list of
final output aliases (period, by aliases, measures, `{measure}_{cmp}`) in
one place and raise on any duplicate, then delete the now-redundant
spot check in `_check_compare`.

**Pros:** closes all three confirmed duplicate paths plus every future
one (new generated-column features — `suppress` flags, compare deltas —
inherit the guard); *removes* code on net; the check runs on names already
in hand, no schema access needed.
**Cons:** none of substance — a user with a dim genuinely named `n_prior`
gets an error and renames one side, which is exactly the existing
`_check_compare` policy applied consistently.

### 2C. Canonical half-open time windows internally (closes F3/F5's whole class)

This round's F3 and F5, last round's month-end clamp, and the original
`BETWEEN ... DATE 'hi'` day-cut are all the same bug: **inclusive upper
bounds are the wrong internal representation for time**. Every operation —
shifting, clamping, compiling for DATE vs TIMESTAMP columns, completeness
end-arithmetic — needs a special case for "and the end means the whole
day/instant". Proposal: keep `Between` inclusive at the *user surface*
(it matches `time=(lo, hi)` intuition and the docs), but normalise to an
internal `TimeWindow(lo, hi_exclusive)` at `split_context` time; shifting
becomes plain arithmetic on two instants (no exclusive-bound trick, no
day-clamp asymmetry), and `_time_predicate` becomes unconditionally
`>= lo AND < hi_exclusive`.

**Pros:** deletes the two subtlest functions' special cases (`_shift_op`'s
date-only trick, `_time_predicate`'s type dispatch) and retires a bug class
with a track record of four escapes; period math (`period_end + 1 day`)
already thinks in exclusive bounds, so completeness code simplifies too.
**Cons:** a real migration — `Between` on the time key currently flows into
provenance `to_dict`/hashes, so the resolved-context serialisation must
keep emitting the inclusive form to avoid shifting context hashes; and
tests asserting on emitted predicates need updating. Worth it: this is the
only area where the same bug has now recurred across independent reviews,
which is the signature of a representation problem, not a diligence problem.

### 2D. Capture provenance at execution time, not at `.provenance()` time

`Provenance` re-runs the as-at queries, refresh-stamp lookups, and schema
DESCRIBEs when called. `.frame()` and `.provenance()` can therefore
describe *different data*: `ws.mirror()` between the two calls closes the
session connection, `ws.con` lazily reopens onto the refreshed file, and
the provenance now stamps as-at moments/refresh times the number was never
computed under. The same drift applies to two `.frame()` calls, but that's
visible; a provenance that silently mismatches its number undermines the
"a number explains itself" contract.

Proposal: have `.frame()` capture the data-side facts (as-at rows, refresh
stamps, fact min/max) on the connection it executed on, memoised on the
Slice; `.provenance()` reuses the capture when present and computes fresh
only if the slice never executed (documenting that case).

**Pros:** the provenance is guaranteed to describe the number it accompanies
— the core promise of the feature; also makes `.provenance()` cheap after
`.frame()` (no second fact scan for as-at lanes).
**Cons:** slightly more state on Slice and a defined-but-two-case behavior.
The alternative (document the drift) leaves the flagship guarantee soft.

### 2E. Reject `time_agg: avg` until it computes something

On a snapshot model, `avg` compiles identically to `last` — the as-at join
always picks the final snapshot; no averaging-over-snapshots exists
anywhere. The docs do say `time_agg` "governs which comparisons are valid,
never plain-grain computability", but a measure *labelled* "avg" that
reports last-snapshot values is a mislabel in the governed-definition file
itself — the one artifact that's supposed to be unambiguous, and the label
is folded into the measure hash as if it meant something.

**Pros of rejecting now:** `sum`-on-snapshot already gets exactly this
treatment for exactly this reason (silent mislabel); rejecting is 3 lines
and reversible the day averaging is implemented; nobody can have depended
on a semantics that doesn't exist.
**Cons:** anyone using `avg` as pure documentation must relabel to `last`.
That's the point.

### 2F. Quote generated aliases (error-quality, minor)

Names are validated as plain identifiers (the injection firewall — keep
it), but they're emitted unquoted, so a measure legitimately named `filter`,
`order`, or `select` passes the loader and dies at bind-time EXPLAIN with a
raw DuckDB parser error — violating the "one clear sentence + the fix"
convention. Quoting every emitted alias (`AS "wait_days"`) makes reserved
words simply work. **Pros:** removes a whole error class; mechanical
change; validation still guards injection so quoting is belt-and-braces,
not a substitute. **Cons:** churn in SQL-text-ish assertions — but the
suite deliberately asserts on parse trees, which don't see quoting; the
canary/tree tests should pass unchanged.

### Deliberate design choices reviewed and endorsed (no change recommended)

- **Shifted-CTE compares (one fact scan per lane)** over window functions:
  the per-lane as-at and gap-period-NULL guarantees genuinely need it;
  DuckDB shares scans well enough that correctness wins.
- **String-built SQL over an AST/relational API**: with the identifier
  firewall plus parse-tree tests, the simplicity is paying for itself; an
  AST layer would be a large rewrite chasing bugs the tests already catch.
- **The `_project` hash allowlist**: re-examined; still the right stability
  mechanism (per the CLAUDE.md contract — don't "improve" it).
- **Self-keying caches on connection identity + YAML mtimes**: correct
  across the reopen paths; found no stale-cache hole (`_bind_warnings`
  correctly re-keys on both connection and defs).

---

## Suggested triage order

1. F1 (one-line, silent inflation) and F5+F6 (few lines, silent row loss).
2. F2 via 2A — biggest silent-wrong-number surface.
3. F4 via 2B, F3 via 2C (or a targeted datetime patch if 2C is deferred).
4. F7, F8, 2D, 2E, 2F as convenient.
