# Testing the metrics layer — the trust contract

The metrics layer computes governed numbers; its test harness is layered
so that correctness is *demonstrated*, not just asserted:

1. **Structural tests** (`tests/metrics/test_*.py`) — parse-tree SQL
   assertions, canary-literal lane isolation, and a regression museum of
   every past adversarial finding.
2. **Differential tests** (`test_differential.py`) — randomized slices
   checked cell-exact against an independent naive interpreter
   (`oracle.py`, which never imports `wh.*`).
3. **Metamorphic properties** (`test_properties.py`) — relations between
   the compiler's own answers (partition sums, window splitting,
   prior==direct) that hold regardless of implementation.
4. **Totality fuzzing** (`test_fuzz_loader.py`) — every input either
   loads or raises `SemanticsError`; generated SQL always parses.
5. **Mutation testing** (on demand) — measures whether the suite would
   catch seeded defects. Baseline scores below.
6. **Engine matrix** (`scripts/engine_matrix.sh`) — the suite against the
   pinned and latest DuckDB.

## Running

- Fast (default, every commit): `uv run pytest`
- Deep generative run: `HYPOTHESIS_PROFILE=deep uv run pytest tests/metrics -m fuzz`
- Mutation: see the Mutation section below.
- Engine matrix: `bash scripts/engine_matrix.sh`

## Guarantee traceability

Every documented guarantee names the tests that hold it (all names
grep-verified against the suite):

| Guarantee (docs/metrics.md) | Tests |
|---|---|
| Two-lane isolation (intrinsic FILTER vs context WHERE) | `test_invariants.py` (canary literals), `test_differential.py::test_plain_slices_match_the_oracle` |
| As-at never moves under attribute context | `test_invariants.py` (attribute canary vs `__asat`), oracle `_asat_filter` in every snapshot differential |
| Day-inclusive time ranges, exact datetime bounds | `test_compiler.py::test_time_context_is_day_inclusive_on_both_ends`, `test_mutation_gaps.py::test_to_window_bounds_are_exact`, `test_properties.py::test_splitting_the_window_partitions_counts` |
| Compare: shifted windows, period-end mapping, gaps stay NULL | `test_compare.py`, `test_differential.py::test_compare_slices_match_the_oracle`, `test_properties.py::test_prior_equals_the_direct_value_of_the_previous_period`, `test_mutation_gaps.py` (day/week/datetime shifted-window cases) |
| fytd recomputed from base, FY-bounded, group-isolated, empty=gap | `test_compare.py::test_fytd_*` (6 tests), oracle `_fytd_cells` differential, `test_mutation_gaps.py::test_fytd_lane_with_ratio_context_and_shared_dim` |
| Suppression: cell rows under n go NULL; ratio den rule; per-lane comparison counts | `test_suppress.py`, `test_differential.py::test_suppress_and_complete_periods_match_the_oracle`, `test_mutation_gaps.py::test_suppress_guards_comparison_columns_by_their_own_lane` |
| Completeness: max-date rule, cadence rule, context truncation | `test_slice_behaviour.py` / `test_compare.py` completeness tests, the suppress/complete differential, `test_mutation_gaps.py` (window-truncated + fiscal-grain cadence cases) |
| Loader firewall: identifiers validated, strict keys, totality | `test_loader.py` (32 tests), `test_fuzz_loader.py` (arbitrary YAML + structure-aware mutation) |
| Output namespace: every column named exactly once | `test_compiler.py::test_duplicate_output_columns_error`, `result_map`'s exact-column assert in every differential, `test_fuzz_loader.py::test_context_values_never_break_the_generated_sql` |
| Context miss rule: skipped and recorded, never halts | `test_context.py`, `test_mutation_gaps.py::test_ignored_entry_does_not_swallow_later_entries` |
| Hash stability across engine upgrades | `test_provenance.py::test_measure_hash_ignores_serializer_and_formatting_noise` (+ 4 hash-relevance tests), `scripts/engine_matrix.sh` |

## Engine matrix

`bash scripts/engine_matrix.sh` runs `tests/metrics` against the pinned
DuckDB and the latest release. As of 2026-07-21 both legs resolve 1.5.4
(pin == latest) and pass; the second leg starts earning its keep on the
next DuckDB release. A latest-leg failure is the script working — note
it, don't pin around it.

## Mutation baseline

Recorded 2026-07-21, mutmut 3.6.0. Re-run with:

```bash
uv run mutmut run "wh.metrics.timegrain.*" "wh.metrics.context_ops.*"
uv run mutmut run "wh.metrics.compiler.*"       # hours — run unattended
uv run mutmut results --all true                # per-mutant ledger
```

Config in `pyproject.toml` `[tool.mutmut]` (3.x key names; the runner
excludes `-m fuzz` tests — the generative layers measure themselves by
construction, and mutation needs the fast suite). Mutants are named
`wh.metrics.<module>.*`; stage runs per module with those patterns.

| Module | Mutants | Killed | Survived | Triage |
|---|---|---|---|---|
| `timegrain.py` | 164 | 158 | 6 | 2 equivalent, 4 prose (below) |
| `context_ops.py` | 250 | 227 | 23 | 23 prose (below) |
| `compiler.py` | 948 | 839 | 109 | all ledgered below |

Not yet baselined: `loader.py`, `checks.py`, `provenance.py`,
`result.py` (~2000 mutants). Same procedure when wanted:
`uv run mutmut run "wh.metrics.loader.*"` etc.

First-pass survivors drove new killing tests (now in the suite):
`period_start` had no direct tests (now held cell-exact against
`grain_expr` over a 3-year × 3-fiscal-start grid), `fy_start` boundary,
the explicit-`Between` coercion path, 3-element time tuples, `wh.last`
year units, time-typed widgets, `to_dict` op encodings, `Context`
`__eq__`/`__repr__`/`__hash__`. The compiler pass drove
`tests/metrics/test_mutation_gaps.py`: unit pins on
`_lit`/`_predicate`/`_time_predicate`/`_to_window` (booleans, quote
doubling incl. inside the NUL path, exact half-open/microsecond bounds),
suppress-threshold boundaries, the miss rule not swallowing later
context entries, and six deterministic fixed-case differentials — the
fytd CTE internals (ratio num/den, multi-predicate context, shared-dim
join, fiscal grain, window truncation), day- and week-grain shifted
windows (the week one catches hi-side shift errors day grain can't
see), yoy over datetime bounds, per-lane comparison suppression,
window-truncated completeness under compare, and scoped `values()`
with a shared-dim context.

### Survivor ledger

- **Equivalent (2, timegrain):** `period_start__mutmut_62` and `_79`
  seed a day-2 date into `_month_add`/re-`date()` chains that rebuild
  from year+month only — the mutated day can never reach the result.
- **Error-prose class (27 context_ops/timegrain + ~44 compiler, incl.
  the 9 `_by_surface` "no tests"):** mutants that only reword
  `SemanticsError` message text, None-out the `key`/`entry` argument
  that feeds message interpolation, or mutate `_by_surface` /
  `_declared_surface` (pure error-message builders). Tests pin exception
  type and load-bearing phrases via `pytest.raises(match=...)`,
  deliberately not full prose — error wording must stay free to improve.
- **SQL-text case/format class (~27 compiler):** lowercased keywords
  (`select`, `where`, `group by all`…), `CHR(0)`, `P.PERIOD`,
  `__CELL_N` — DuckDB keywords and unquoted identifiers are
  case-insensitive; execution and the parse-tree structural tests are
  deliberately case-blind, and provenance hashes semantic projections,
  not SQL text.
- **Structurally equivalent (~38 compiler):**
  - `cell_n=`/`complete_periods=` default flips — every call site
    passes the argument explicitly.
  - `_sel(None, …)` — falsy prefix renders identically to `""`.
  - alias `rsplit(" AS ")` variants in by/values items — generated by=
    items contain exactly one ` AS ` (identifiers can't hold spaces), so
    split/rsplit/maxsplit variants agree; the one item with an inner
    ` AS ` (the CAST grain expr) is aliased outside this loop.
  - the `joined`-set detection variants (`removesuffix`, `FACT.`,
    split-maxsplit) — bogus additions like `"fact"` are filtered by the
    `model.dims` intersection, and `fact`/`period`/`time` are
    loader-reserved names; single-dot lhs makes the remaining variants
    agree.
  - output-namespace seed mutants (`out_cols` period entry, `.lower()`
    vs `.upper()` normalization, alias strip variants) — the loader
    reserves `period` and rejects dim/measure overlap, so the duplicate
    check only ever fires on duplicate by= entries, which these
    mutations cannot un-duplicate.
  - `_time_predicate(None, op)` on the fallthrough — every reachable
    non-window time op is `All()`, which ignores the lhs.
