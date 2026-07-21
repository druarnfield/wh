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

(filled in as tasks land; final table in Task 10)

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
| `compiler.py` | (run in progress — recorded when it lands) | | | |

First-pass survivors drove new killing tests (now in the suite):
`period_start` had no direct tests (now held cell-exact against
`grain_expr` over a 3-year × 3-fiscal-start grid), `fy_start` boundary,
the explicit-`Between` coercion path, 3-element time tuples, `wh.last`
year units, time-typed widgets, `to_dict` op encodings, `Context`
`__eq__`/`__repr__`/`__hash__`.

### Survivor ledger

- **Equivalent (2):** `period_start__mutmut_62` and `_79` seed a day-2
  date into `_month_add`/re-`date()` chains that rebuild from year+month
  only — the mutated day can never reach the result.
- **Error-prose class (27):** mutants that only reword `SemanticsError`
  message text (or None-out the `key` argument that feeds message
  interpolation). Tests pin exception type and load-bearing phrases via
  `pytest.raises(match=...)`, deliberately not full prose — error wording
  must stay free to improve. Any mutant whose only effect is message text
  is an accepted survivor by policy.
