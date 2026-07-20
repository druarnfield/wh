# Metrics Layer Phase 3 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. NO worktree — `greenfield-metrics` in the main checkout.

**Goal:** Rollout step 3 of `docs/plans/2026-07-20-metrics-design.md`: the provenance object + `render()`, tree-based measure hashing over a semantic projection, model hashes, and schema fingerprints — every number explains itself.

**Architecture:** New module `src/wh/metrics/provenance.py` (no verb collision). Measure hashes are sha256 over a *semantic projection* of `json_serialize_sql` trees — a recursive extraction keeping only stable semantic keys (`class`, `type`, `function_name`, `column_names` lowercased, constant `value` subtrees, `distinct`) and recursing through everything except `*location*` keys; unknown scalar keys are DROPPED, so serializer additions in a DuckDB upgrade cannot shift hashes (structural renames still could — the running DuckDB version is stamped so any shift is explainable). Hash inputs per design: expr + intrinsic where + ratio parts + time_agg + fact reference. Model hash is pure Python over the normalised config (snapshot, time column + cadence, dim key mappings, fiscal_year_start). The compiler stores its as-at subqueries on `Compiled` so provenance can report each period's (and each comparison's) actual evaluation moment; scan-widening coverage is checked against the fact's `min(time_column)`.

**Data sources, degrade gracefully:** `_mirror.meta` (schema_name/table_name/extracted_at) supplies refresh timestamps when present; in-memory test mirrors have none and provenance says so rather than failing. Bind-check orphan warnings flow BoundModel → Slice → provenance.

---

## Task 1: semantic projection + measure/model hashes

**Files:** Create `src/wh/metrics/provenance.py`, Test `tests/metrics/test_provenance.py`

- `_project(tree) -> dict` per Architecture; `measure_hash(con, model, measure) -> str` (sha256 hex over canonical `json.dumps(sort_keys=True)` of `{parts, time_agg, fact}`); `model_hash(model) -> str`.
- **Failing tests:** hash stable across serializer noise (`count( * )`, `COUNT(*)`, odd whitespace → identical); hash changes iff definition changes (expr / where / time_agg / ratio part / fact each change it; *description does not*); `snapshot` flip changes the model hash but not the measure hash; `fiscal_year_start` changes the model hash; hashes are 64-char hex. Commit `feat(metrics): semantic-projection measure hashes + model hashes`.

## Task 2: as-at + truncation plumbing

**Files:** Modify `src/wh/metrics/compiler.py`, `src/wh/metrics/timegrain.py`; Test `tests/metrics/test_provenance.py`

- `timegrain.period_end(d, grain, fys) -> date` (pure Python; month/quarter/year/fy/fy_quarter via month arithmetic, week → Sunday, day → d). Tests against the fiscal cases.
- Compiler: extract `_asat_subquery(model, grain, time_op) -> str`; `Compiled.asat_queries: dict|None` — `{"base": sql, "<cmp>": shifted sql}` on snapshot models (fytd never; prior/yoy per CTE). Existing structural tests must stay green (pure refactor of `_asat_join`).
- Commit `feat(metrics): as-at subqueries exposed for provenance; period_end helper`.

## Task 3: the Provenance object + render + to_dict

**Files:** Modify `src/wh/metrics/provenance.py`, `src/wh/metrics/result.py` (Slice.provenance(), warnings threading from BoundModel); Test `tests/metrics/test_provenance.py`

Fields per design: `.measures` `[(name, definition text incl. where + "at snapshot", hash)]`; `.context` (applied resolved entries; ignored; `All()` entries reported "empty selection → unfiltered" — the third bucket); `.shape` (by/grain/compare/complete_periods/suppress, `non_additive` names, `strictness_weakened` when per-slice False overrides model-level true); `.data` (per-table schema fingerprints via DESCRIBE, refresh timestamps from `_mirror.meta` or "unavailable", DuckDB version, orphan warnings, per-compare scan_lo + coverage vs `min(time_column)` — "fact data begins X, prior window partially uncovered", as-at rows from `asat_queries`, truncation note when the context's hi sits mid-period); `.sql`; `.render()` (compact block shaped like the design's example sentence); `.to_dict()` (json.dumps-able; dates ISO).

**Failing tests:** field contents on a governed slice (June context, by region, compare prior, snapshot model); empty-selection bucket; strictness-weakened flag; yoy-off-data coverage says "begins"; truncation note for a window ending 07-05; `json.dumps(p.to_dict())` round-trips; render contains name + short hash + "as-at"; refresh timestamps read from a seeded `_mirror.meta` and degrade without one. Commit `feat(metrics): provenance object — numbers explain themselves`.

## Task 4: docs + wrap

README (provenance example in metrics section, "coming next" → marimo widgets); CLAUDE.md phase-3 status + gotchas. Full suite green. Commit `feat(metrics): step 3 docs`.

**Out of scope (design says later):** curated-schema git hash stamp, `wh.compose()`, context YAML serialisation.
