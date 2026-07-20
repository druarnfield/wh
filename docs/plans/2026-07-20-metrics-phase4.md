# Metrics Layer Phase 4 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. NO worktree — `greenfield-metrics` in the main checkout.

**Goal:** Rollout step 4 of `docs/plans/2026-07-20-metrics-design.md`: marimo widget helpers — `m.filter_dim()` populated from the declared surface, `m.filter_date()` with real bounds, and the possible-values helper that makes associative/cascading filtering user-space composition.

**Architecture:** The queryable core is a marimo-free helper: `compile_values(model, attr, ctx)` in the compiler (structurally testable) and `BoundModel.values(attr, context=)` executing it. Unscoped shared-dim options come from the **dimension table only** — cheap, no fact scan (structurally asserted: the fact never appears in the SQL). With a context, options re-derive from context-scoped fact rows through the normal lanes (no as-at row-selection — options mean "ever in scope", documented). Widgets are thin wrappers: `filter_dim` → `mo.ui.multiselect` (list value → `In`; empty widget → unfiltered — the existing context semantics fit exactly), `filter_date` → `mo.ui.date_range` over the fact's min/max. marimo is imported lazily with an install-hint error and added to dev deps for tests; exclude-your-own-field stays a visible argument (`context=ctx.without("facility__clinic")`), never magic.

---

## Task 1: `compile_values` + `BoundModel.values()`

**Files:** Modify `src/wh/metrics/compiler.py`, `src/wh/metrics/result.py`; Test `tests/metrics/test_values.py` (new)

- `compile_values(model, attr, ctx=EMPTY) -> str`: attr addressed like `by=` (`facility.region` / local `urgency`; same errors). No context + shared dim → `SELECT DISTINCT {col} AS value FROM {dim.table} WHERE {col} IS NOT NULL ORDER BY 1`. Otherwise → `SELECT DISTINCT {lhs} AS value FROM {fact} AS fact {joins for attr dim + context dims} WHERE {lanes} AND {lhs} IS NOT NULL ORDER BY 1`. NULL is never an option (membership tests exclude it anyway).
- `BoundModel.values(attr, context=EMPTY) -> list`: resolves unresolved contexts against the fact max (same anchor rule as slicing), executes, returns the column as a list.
- **Failing tests:** structural — unscoped shared-dim SQL touches the dim table only (assert exact SQL AND `"waitlist" not in sql`); scoped SQL carries the context lanes; local dims read the fact column both ways. Behavioural — `values("facility.region")` == `["North","South"]`; cascade: region-widget context narrows `values("facility.clinic", context=ctx)` to that region's clinics; exclude-your-own-field via `ctx.without(...)` returns the full set again; unknown attr errors name the surface. Commit `feat(metrics): possible-values helper (compile_values + BoundModel.values)`.

## Task 2: `filter_dim` / `filter_date` widgets

**Files:** Modify `src/wh/metrics/result.py`, `pyproject.toml` (marimo in dev deps); Test `tests/metrics/test_widgets.py` (new)

- `_mo()` lazy import → `SemanticsError("marimo isn't installed — pip/uv add marimo (widgets are notebook-only)")`.
- `filter_dim(attr, context=EMPTY, label=None)` → `mo.ui.multiselect(options={str(v): v for v in self.values(attr, context)}, label=label or attr)`.
- `filter_date(label=None)` → min/max of the time column CAST to DATE → `mo.ui.date_range(start=lo, stop=hi, value=(lo, hi), label=label or time column)`.
- **Failing tests** (pytest.importorskip("marimo")): filter_dim options match values() and the widget's empty `.value` flows through `wh.context(... )` → unfiltered (reuse the existing resolution tests' pattern with the real widget); filter_date bounds == fact min/max; missing-marimo error path (monkeypatch import) names the fix. End-to-end: `wh.context(facility__region=m.filter_dim("facility.region"))` sliced after selecting a value in the widget (`widget._value` set via marimo API or reconstructed widget) filters correctly — if programmatic value-setting is awkward, assert via a FakeWidget carrying the same `.value` contract instead and keep the real-widget test to construction+options. Commit `feat(metrics): marimo filter widgets`.

## Task 3: docs + wrap

README (widgets example in the metrics section, cascading composition shown; "coming next" → design's Later list); CLAUDE.md phase-4 status + any gotchas. Full suite green. Commit `feat(metrics): step 4 docs — design fully delivered through step 4`.

**Out of scope:** the design's Later list (curated-schema git hash, `wh.compose()`, complementary suppression, context YAML round-trip).
