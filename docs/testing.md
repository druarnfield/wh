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

(recorded in Task 9)
