# Metrics Layer Phase 2 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. NO worktree — work on `greenfield-metrics` in the main checkout.

**Goal:** Rollout step 2 of `docs/plans/2026-07-20-metrics-design.md`: `compare=` (prior / yoy / fytd with scan widening and per-period as-at on snapshot models), `complete_periods=`, and `.suppress(n)`.

**Architecture:** Comparisons are separate CTEs, never lag: `__out` is the existing inner query untouched (original time predicate — mid-window truncation semantics preserved), and each compare gets its own CTE whose time window is *shifted*, self-joined back on shifted period + group columns (`IS NOT DISTINCT FROM`, so NULL/orphan groups align). On snapshot models each CTE carries its own `__asat` subquery over its own window — "each comparison period's own as-at" falls out structurally. `fytd` recomputes from base rows (`fact.time >= fy_start(period)`), never window-sums period aggregates — compute-from-base holds even for distinct counts. Suppression and completeness are outer-select concerns: a hidden `__cell_n = count(*)` per CTE and a period-end vs data-max predicate.

**Tech Stack:** unchanged (DuckDB, pure-function compiler, parse-tree tests).

**Decisions pinned here (flag to Dru in the wrap-up):** compare vocabulary v1 is `prior`, `yoy`, `fytd`; comparison columns hold the comparison *value* (deltas are notebook arithmetic), named `{measure}_{cmp}` (collision with a declared measure → slice-time error); `compare=` requires `grain=`; `yoy` errors at `week` grain (a year shift misaligns week starts); completeness reads `max(time_column)` from the fact itself, not `_mirror.meta` (meta records `extracted_at`, refresh time — not data max; correct beats fast at our scale).

---

## Task 1: compare validity + shift helpers (pure)

**Files:** Modify `src/wh/metrics/compiler.py`, `src/wh/metrics/timegrain.py`; Test `tests/metrics/test_compare.py` (new)

- `timegrain.py`: `PERIOD_INTERVAL = {"day": "1 DAY", "week": "7 DAY", "month": "1 MONTH", "quarter": "3 MONTH", "year": "1 YEAR", "fy": "1 YEAR", "fy_quarter": "3 MONTH"}` (SQL interval per grain — also used by complete_periods) and `fy_start(d: date, fiscal_year_start: int) -> date` (pure Python, mirrors the shift-trick).
- `compiler.py`: `COMPARES = ("prior", "yoy", "fytd")`; `_compare_offset(cmp, grain) -> (months, days)` — prior = one grain interval (month→(1,0), week→(0,7), fy→(12,0)…), yoy = (12,0); `_shift_between(op, months, days)` shifts both endpoints in Python (reuse `context_ops._months_back`; dates and datetimes both).
- Validity (`_check_compare(model, measures, compare, grain)`): unknown compare name → error listing the three; no grain → "compare= needs grain="; per requested measure by `time_agg`: `sum` → all; `last`/`avg` → prior/yoy only ("fytd is cumulative; '<m>' is a point-in-time stock — see time_agg"); `none` → any compare errors naming the rule; `yoy` at week grain → error; suffix collision `{m}_{cmp}` already a measure → error.

**Failing tests:** each validity rule (fytd + `patients_waiting` errors; `median_wait` + anything errors; week+yoy errors; no-grain errors; collision errors); `fy_start(date(2026,3,10), 7) == date(2025,7,1)`; offsets table. Then implement → pass → commit `feat(metrics): compare validity and shift helpers`.

## Task 2: prior/yoy compilation

**Files:** Modify `src/wh/metrics/compiler.py`; Tests `tests/metrics/test_compare.py`, `tests/metrics/test_invariants.py`

`compile_slice(..., compare=())`. When compare non-empty, restructure as CTEs — the existing single-query shape must stay byte-identical when `compare=()` (all current structural tests keep passing unchanged):

```sql
WITH __out AS ( <existing inner query, original time predicate> ),
__cmp_yoy AS ( <same query, time predicate shifted -1 year> )
SELECT b.period, b."facility.region", b.removals,
       y.removals AS removals_yoy
FROM __out AS b
LEFT JOIN __cmp_yoy AS y
       ON y.period = b.period - INTERVAL 12 MONTH
      AND y."facility.region" IS NOT DISTINCT FROM b."facility.region"
ORDER BY period
```

- Shifted CTEs reuse the full inner builder (joins, lanes, `__asat` with the *shifted* window) — refactor the inner-query construction into `_inner_query(model, measures, by, ctx_attrs, time_op, grain) -> (sql, select_aliases)` first, as a pure refactor commit with the suite green.
- Non-time context entries apply unchanged in every CTE (North compares against North-prior-year). Ratio measures: CTEs carry `__num`/`__den`; the outermost select divides for base AND compare columns (`CAST(y.__pct_num AS DOUBLE)/NULLIF(y.__pct_den,0) AS pct_yoy`).
- Store widening metadata on `Compiled`: `scan_lo: dict[cmp, date|None]` (shifted window start; None when unbounded) — provenance (step 3) renders it; nothing else consumes it yet.

**Failing tests:** structural (the exact shape above via `assert_sql_equiv`); behavioural on `removals`: `prior` at month — July's `removals_prior` == June's value per region, June's is NULL (no May data: off-data edge yields NULL, `scan_lo` recorded); **gap-period fixture**: seed a group with no June rows — its July `prior` is NULL, and other groups' values do NOT slip into it (the self-join-not-lag guarantee); yoy with our one-year data span → all NULL, no crash. Invariants: add compare cases to `CASES` — attr canaries must appear in *every* CTE's WHERE and nowhere else (`in_where == total` still), time canary rule unchanged. Commit `feat(metrics): prior/yoy comparisons as shifted CTEs`.

## Task 3: snapshot compare — own as-at per period

**Files:** Test `tests/metrics/test_compare.py` (behavioural only — the code falls out of Task 2)

`waitlist` (snapshot) at month grain, `compare=["prior"]`, June+July window: July's `patients_waiting_prior` == 4 == June's own value (June evaluated at its OWN as-at 06-26, July at 07-10); with `facility.region` by, South's prior == 1 (C3 lagging rule still holds inside the compare CTE). Assert the attr-canary/as-at invariant across compare CTEs (extend the Task 2 invariant run to a snapshot+compare case). Commit `test(metrics): snapshot comparisons carry their own as-at`.

## Task 4: fytd compilation

**Files:** Modify `src/wh/metrics/compiler.py`; Test `tests/metrics/test_compare.py`

`__cmp_fytd` recomputes from base with a period-join, never window-sums:

```sql
__cmp_fytd AS (
    SELECT p.period, p."facility.region"…,
           count(*) FILTER (WHERE removal_reason <> 'ADMIN') AS removals
    FROM (SELECT DISTINCT period, "facility.region"… FROM __out) AS p
    JOIN main.waitlist_removals AS fact
      ON fact.removal_date >= CAST(date_trunc('year', p.period - INTERVAL 6 MONTH) + INTERVAL 6 MONTH AS DATE)
     AND CAST(date_trunc('month', fact.removal_date) AS DATE) <= p.period
    LEFT JOIN <dim joins as usual>
    WHERE <non-time context predicates>
    GROUP BY ALL
)
```

joined back on plain period + group equality. The fiscal-start expression is `grain_expr("fy", "p.period", fys)` reused verbatim.

**Failing tests:** behavioural on `removals` (fiscal_year_start=7): June 2026 `removals_fytd` == 2 (FY2025-26 has only June's events), **July 2026 `removals_fytd` == 2, not 4** — the FY reset across Jun→Jul is the money assertion; distinct-count fytd correctness: add a tiny event fact where one `ur` appears in two months — fytd distinct count at month 2 counts it once (proves compute-from-base, would fail under window-summing). Structural: division-outermost still holds with ratio+fytd. Commit `feat(metrics): fytd recomputed from base rows`.

## Task 5: complete_periods

**Files:** Modify `src/wh/metrics/compiler.py`, `src/wh/metrics/loader.py` (cadence vocabulary); Test `tests/metrics/test_compare.py` or `test_slice_behaviour.py`

- Loader: `time.cadence` validated to `daily|weekly|monthly` (was free text) → `_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 31}`.
- `compile_slice(..., complete_periods=False)`; requires grain (error otherwise). Adds to the outermost query:
  - flow models: `WHERE b.period + INTERVAL <grain> - INTERVAL 1 DAY <= (SELECT max(tc) FROM fact)`
  - snapshot + cadence: `WHERE (SELECT max(tc) FROM fact WHERE <grain_expr(tc)> = b.period) > b.period + INTERVAL <grain> - INTERVAL 1 DAY - INTERVAL <cadence_days> DAY` — "the period's final *expected* snapshot landed"
  - snapshot without cadence: the flow (global max-date) rule; the docstring calls it weaker.
- Data max comes from the fact, not `_mirror.meta` (meta records refresh time, not data max — decision pinned above).

**Failing tests:** removals monthly June+July: `complete_periods=True` drops July (max 07-08 < 07-31), keeps June; waitlist (weekly cadence) custom fixture where the final month's last snapshot is 07-28 of a 31-day month → **complete under cadence, dropped under the max-date rule** (build the same model with cadence stripped to prove the two rules differ); `complete_periods` without grain errors. Commit `feat(metrics): complete_periods with cadence-aware snapshot completeness`.

## Task 6: .suppress(n)

**Files:** Modify `src/wh/metrics/compiler.py`, `src/wh/metrics/result.py`; Test `tests/metrics/test_suppress.py` (new)

- `compile_slice(..., suppress=None)`. When set: every CTE (and the plain inner query) gains `count(*) AS __cell_n`; the outermost select exists always in this mode and projects each measure/compare column through `CASE WHEN <cte>.__cell_n < {n} THEN NULL ELSE <col> END`; ratio columns additionally NULL when their `__den < {n}` (a big cell can still have a tiny denominator). `__cell_n` is never projected outward.
- `Slice.suppress(n=5)` returns a NEW Slice (immutable — recompiles with `suppress=n`); the design's method-on-result surface.

**Failing tests:** `patients_waiting` by region June, `.suppress(5)`: North (3) and South (1) both NULL; `.suppress(2)`: North 3 survives, South NULL; ratio: group with `__den` 1 → NULL even when cell_n ≥ n; suppression applies to compare columns (July's `removals_prior` NULLed when June's cell was small); no-suppress SQL byte-identical to today (existing structural tests untouched). Commit `feat(metrics): small-cell suppression incl. dependent ratios`.

## Task 7: API threading, invariants, docs

**Files:** Modify `src/wh/metrics/result.py`, `src/wh/workspace.py` (kwargs pass-through), `README.md`, `CLAUDE.md`; Tests `tests/metrics/test_wiring.py`

- `Slice.__init__`/`BoundModel.slice`/`Workspace.slice` accept `compare=`, `complete_periods=`; `.suppress()` end-to-end through `wh.slice(...)`.
- Wiring test: `wh.slice("removals", measures=["removals"], grain="month", compare=["prior"], complete_periods=True).frame()` on the metric_project fixture.
- README: compare/complete_periods/suppress examples in the metrics section; "coming next" line now says provenance. CLAUDE.md: phase 2 status bullet + any new gotchas learned. Full suite green. Commit `feat(metrics): step 2 API surface + docs`.

---

## Execution notes

Strict TDD; `set -o pipefail`; parse-tree structural tests, never SQL text; every error one sentence + the fix. The `compare=()` / `suppress=None` paths must not change existing emitted SQL — phase 1's structural tests are the regression net proving it.
