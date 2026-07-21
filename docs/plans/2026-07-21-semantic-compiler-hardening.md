# Semantic Compiler Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 8 confirmed findings of
`docs/reviews/2026-07-21-semantic-compiler-adversarial-review.md`, make
local-vs-shared dimension linking explicit in model YAML, and land the four
agreed architectural items (half-open time windows, execution-time
provenance capture, rejecting `time_agg: avg`, quoted aliases).

**Architecture:** All work is inside `src/wh/metrics/` plus docs and tests.
Loader changes land first (strict keys, explicit `shared:` linking, avg
rejection) so every later task writes test YAML in the final syntax; then
bind-check fixes, context-op fixes, compiler fixes, and finally the
provenance-capture restructure. The time-lane rework replaces inclusive
`Between` bounds with an internal half-open `TimeWindow(lo, hi_exc)` created
once in `split_context` — the surface `Between` (and therefore context
hashing/serialisation) is untouched, so no hash migration.

**Tech Stack:** Python 3.12, DuckDB, pytest. House rules apply: TDD
(failing test → verify → implement → verify → commit), parse-tree SQL
assertions via `tests/metrics/treecheck.py` (never SQL text), docs updated
in the same commit as the surface change, never mention Claude in commits.

**Decisions locked in (agreed with Dru 2026-07-21):**
- Bare `name: column` in model `dimensions:` ALWAYS means a local
  (degenerate) dim — it never auto-links to a shared dim. Shared references
  are explicit: `name: {shared: fact_key_column}`. A bare name that matches
  a declared shared dim is a load error (forces the author to pick). An
  explicit `local:` key is unnecessary under this rule and is NOT added
  (YAGNI) — bare IS local.
- Scope includes review items 2C, 2D, 2E, 2F.
- `_check_compare`'s declared-measure suffix check is KEPT (it guards
  declaration-level ambiguity even when the colliding measure isn't
  selected); the new output-namespace check is added alongside it.

**Conventions used below:**
- All commands run from the repo root.
- `tests/metrics/conftest.py` provides `con` (in-memory DuckDB seeded with
  `SEED_SQL`), `make_defs(*yamls)` (writes YAML files to tmp and loads
  them), and `defs` (the design-doc trio DIMS/WAITLIST/REMOVALS).
- Run a single test: `uv run pytest tests/metrics/test_x.py::test_name -v`
- Full suite gate before every commit: `set -o pipefail; uv run pytest -q`
  (363 passed, 3 skipped at plan time; the count grows as tasks land).

---

## Task 1: Strict YAML keys (finding F2 / review 2A)

Unknown keys in any metrics-YAML mapping must be a load error with a
did-you-mean hint. Today `snapsot:`, `cadense:`, `wear:` are silently
dropped and change semantics.

**Files:**
- Modify: `src/wh/metrics/loader.py`
- Test: `tests/metrics/test_loader.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/metrics/test_loader.py`:

```python
def test_unknown_model_key_errors_with_hint(make_defs):
    with pytest.raises(SemanticsError, match="snapshot"):
        make_defs("""\
census:
  fact: main.f
  snapsot: true
  time: {column: d}
  measures:
    n: {description: n, expr: "count(*)"}
""")


def test_unknown_measure_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="unknown key"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d}
  measures:
    n: {description: n, expr: "count(*)", wear: "1=1"}
""")


def test_unknown_time_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="cadence"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d, cadense: daily}
  measures:
    n: {description: n, expr: "count(*)"}
""")


def test_unknown_shared_dim_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="unknown key"):
        make_defs("""\
dimensions:
  facility:
    table: main.dim
    key_colunm: code
    attributes: {name: label}
""")


def test_unknown_ratio_key_errors(make_defs):
    with pytest.raises(SemanticsError, match="unknown key"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d}
  measures:
    pct:
      description: p
      ratio: {num: "count(*)", denum: "count(*)"}
""")
```

(`pytest` and `SemanticsError` are already imported at the top of
`test_loader.py`; check and add `from wh.errors import SemanticsError` if
missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/metrics/test_loader.py -v -k unknown`
Expected: 5 FAILs. The model/time/measure/ratio ones fail because loading
*succeeds*; the shared-dim one fails because the typo currently surfaces as
the "needs 'table' and 'key_column' keys" presence error, whose message
doesn't contain "unknown key".

- [ ] **Step 3: Implement `_reject_unknown` and wire it into every parser**

In `src/wh/metrics/loader.py`, add `import difflib` to the imports, then
add below `_check_table`:

```python
def _reject_unknown(fname: str, where: str, mapping: dict, known: tuple) -> None:
    unknown = [str(k) for k in mapping if k not in known]
    if not unknown:
        return
    hint = difflib.get_close_matches(unknown[0], known, n=1)
    did = f" — did you mean '{hint[0]}'?" if hint else ""
    raise SemanticsError(
        f"{fname}: {where}: unknown key(s) {', '.join(map(repr, unknown))} "
        f"(valid: {', '.join(known)}){did}"
    )
```

Wire in the call sites:

In `_parse_model`, right after the `fact:` isinstance check raises:

```python
    _reject_unknown(
        fname, f"model '{name}'", spec,
        ("fact", "description", "time", "snapshot", "dimensions",
         "measures", "strict_context"),
    )
```

and right after the `time:` isinstance check:

```python
    _reject_unknown(fname, f"model '{name}': time:", time, ("column", "cadence"))
```

In `_parse_measure`, right after the `must be a mapping` check:

```python
    _reject_unknown(
        fname, where, m,
        ("description", "expr", "where", "ratio", "time_agg", "additive"),
    )
```

and inside the `ratio is not None` branch, right after the num/den check:

```python
        _reject_unknown(fname, f"{where}: ratio:", ratio, ("num", "den"))
```

In `_parse_shared_dims`, BEFORE the `table`/`key_column` presence check
(otherwise a typo'd `key_colunm:` surfaces as the presence error, not the
unknown-key error the test demands), guarded on the mapping shape:

```python
        if isinstance(d, dict):
            _reject_unknown(
                fname, f"dimension '{name}'", d,
                ("table", "key_column", "attributes", "hierarchy"),
            )
```

(Place it immediately after the duplicate-definition check; the existing
presence check still handles the non-dict / missing-key cases right after.)

- [ ] **Step 4: Run the loader tests, then the full suite**

Run: `uv run pytest tests/metrics/test_loader.py -v` — all pass.
Run: `set -o pipefail; uv run pytest -q` — all pass (no existing YAML uses
unknown keys; a failure here means a fixture has a stray key — fix the
fixture, not the check).

- [ ] **Step 5: Update docs and commit**

In `docs/metrics.md`, add one sentence to the "Models" section intro (just
above the model key table): `Unknown keys anywhere in a definition are a
load error — typos never silently change a model's semantics.`

```bash
git add src/wh/metrics/loader.py tests/metrics/test_loader.py docs/metrics.md
git commit -m "Reject unknown YAML keys in metric definitions

A typo like snapsot:/cadense:/wear: silently loaded as a different model
(review finding F2): snapshot stocks became event facts, intrinsic
predicates vanished. Every mapping now rejects unconsumed keys with a
did-you-mean hint."
```

---

## Task 2: Explicit local/shared dimension linking

Bare `name: column` becomes always-local; shared references become
`name: {shared: fact_key_column}`; a bare name colliding with a shared dim
errors. Kills two silent flips: a later-added shared dim capturing existing
local dims, and a typo'd shared reference degrading to a local column.

**Files:**
- Modify: `src/wh/metrics/loader.py`
- Modify: `tests/metrics/fixtures_data.py` (WAITLIST_YAML, REMOVALS_YAML)
- Modify: any test YAML co-loaded with `DIMS_YAML` (grep step below)
- Modify: `docs/metrics.md`
- Test: `tests/metrics/test_loader.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/metrics/test_loader.py` (DIMS_YAML is importable:
`from fixtures_data import DIMS_YAML` — add the import if the file doesn't
have it):

```python
LOCAL_VS_SHARED_MODEL = """\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    facility: {shared: clinic_code}
    urgency: urgency_code
  measures:
    n: {description: n, expr: "count(*)"}
"""


def test_bare_dimension_is_always_local(make_defs):
    defs = make_defs(DIMS_YAML, LOCAL_VS_SHARED_MODEL)
    assert defs["events"].dims["urgency"].shared is None
    assert defs["events"].dims["urgency"].fact_column == "urgency_code"


def test_shared_reference_is_explicit(make_defs):
    defs = make_defs(DIMS_YAML, LOCAL_VS_SHARED_MODEL)
    ref = defs["events"].dims["facility"]
    assert ref.shared is not None and ref.shared.table == "main.clinic_dim"
    assert ref.fact_column == "clinic_code"


def test_bare_name_colliding_with_shared_dim_errors(make_defs):
    with pytest.raises(SemanticsError, match="shared"):
        make_defs(DIMS_YAML, """\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    facility: clinic_code
  measures:
    n: {description: n, expr: "count(*)"}
""")


def test_shared_reference_without_declaration_errors(make_defs):
    with pytest.raises(SemanticsError, match="doesn't exist"):
        make_defs("""\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    facility: {shared: clinic_code}
  measures:
    n: {description: n, expr: "count(*)"}
""")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/metrics/test_loader.py -v -k "local or shared_reference or colliding"`
Expected: the two mapping-form tests FAIL ("dimension 'facility' must be a
fact column name" — dict values are rejected today), the collision test
FAILS (currently silently links), the always-local assertion FAILS only if
paired with DIMS naming urgency (it isn't) — it should PASS already; keep
it as a pin.

- [ ] **Step 3: Implement the new dim parse**

In `src/wh/metrics/loader.py`, replace the dims loop in `_parse_model`
(currently the `for dname, v in (spec.get("dimensions") or {}).items():`
block) with:

```python
    dims: dict[str, DimRef] = {}
    for dname, v in (spec.get("dimensions") or {}).items():
        _check_name(fname, "dimension", dname)
        if isinstance(v, dict):
            _reject_unknown(
                fname, f"model '{name}': dimension '{dname}'", v, ("shared",)
            )
            col = v.get("shared")
            if not isinstance(col, str):
                raise SemanticsError(
                    f"{fname}: model '{name}': dimension '{dname}': 'shared:' "
                    f"takes the fact-side key column, e.g. "
                    f"{dname}: {{shared: {dname}_code}}"
                )
            if dname not in shared_dims:
                raise SemanticsError(
                    f"{fname}: model '{name}': dimension '{dname}' references "
                    f"a shared dimension that doesn't exist — declare it under "
                    f"a top-level 'dimensions:' block (or drop 'shared:' for a "
                    f"local dim)"
                )
            _check_column(fname, f"fact column of '{dname}'", col)
            dims[dname] = DimRef(shared=shared_dims[dname], fact_column=col)
            continue
        if not isinstance(v, str):
            raise SemanticsError(
                f"{fname}: model '{name}': dimension '{dname}' must be a fact "
                f"column (local dim) or {{shared: <fact key column>}}"
            )
        if dname in shared_dims:
            raise SemanticsError(
                f"{fname}: model '{name}': dimension '{dname}' is also a "
                f"shared dimension — write {dname}: {{shared: {v}}} to "
                f"reference it, or rename the local dim"
            )
        _check_column(fname, f"fact column of '{dname}'", v)
        dims[dname] = DimRef(shared=None, fact_column=v)
```

- [ ] **Step 4: Migrate test fixtures that co-load DIMS_YAML**

In `tests/metrics/fixtures_data.py`:
- `WAITLIST_YAML`: change `facility: clinic_code` → `facility: {shared: clinic_code}`
  and `doctor: doctor_id` → `doctor: {shared: doctor_id}` (leave
  `urgency: urgency_category` bare — it is local).
- `REMOVALS_YAML`: same two changes.

Then find every other YAML literal loaded together with shared dims:

Run: `grep -rn "facility: clinic_code\|doctor: doctor_id" tests/ docs/`

For each hit inside a test file: if that YAML is passed to `make_defs`
**alongside `DIMS_YAML`** (or another document declaring a shared dim of
the same name), convert to `{shared: ...}` form. If it's loaded standalone
(no shared dim in scope — e.g. `EVENTS_YAML` in `test_compiler.py` if
loaded alone), leave it bare: it was local before and stays local. Check
each call site, don't batch-convert.

- [ ] **Step 5: Run the full suite**

Run: `set -o pipefail; uv run pytest -q`
Expected: PASS. Any remaining failure is a missed co-loaded YAML — the new
collision error names the file and dimension; fix that fixture.

Also run: `uv run wh validate` (structure check works without a mirror) —
`semantics/waitlist.yml` has only local dims and must load unchanged.

- [ ] **Step 6: Update docs and commit**

In `docs/metrics.md`:
- Replace the `dimensions:` row of the model key table with:
  `| \`dimensions:\` | no | \`name: fact_column\` declares a **local (degenerate) dim** — the fact column itself is the attribute. \`name: {shared: fact_key_column}\` references the shared dim \`name\` (a load error if no such shared dim exists). A bare name that matches a shared dim is a load error — linking is always explicit. |`
- Update the prose under "Shared dimensions" (currently "A model
  references a shared dim by name, supplying only its fact-side key column
  (`facility: clinic_code`)") to show the explicit form
  (`facility: {shared: clinic_code}`) and state that bare names never link.

```bash
git add src/wh/metrics/loader.py tests/metrics/ docs/metrics.md
git commit -m "Make shared-dimension linking explicit in model YAML

Bare name: column now always declares a local dim; shared references are
name: {shared: fact_key}. Previously a shared dim added later silently
captured every same-named local dim (join fan-in at a distance), and a
typo'd shared reference silently degraded to a local column. A bare name
colliding with a shared dim is now a load error naming both fixes."
```

---

## Task 3: Reject `time_agg: avg` (review 2E)

`avg` compiles identically to `last` (the as-at join always reads the final
snapshot) — a mislabel inside the governed definition file, folded into
measure hashes as if it meant something.

**Files:**
- Modify: `src/wh/metrics/loader.py`
- Modify: `src/wh/metrics/compiler.py` (`_check_compare`)
- Modify: `docs/metrics.md`
- Test: `tests/metrics/test_loader.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_loader.py`:

```python
def test_time_agg_avg_is_rejected_until_implemented(make_defs):
    with pytest.raises(SemanticsError, match="avg"):
        make_defs("""\
census:
  fact: main.f
  time: {column: d}
  snapshot: true
  measures:
    beds: {description: beds, expr: "max(beds)", time_agg: avg}
""")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_loader.py::test_time_agg_avg_is_rejected_until_implemented -v`
Expected: FAIL — the model loads fine today.

- [ ] **Step 3: Implement**

In `src/wh/metrics/loader.py`:
- Change `VALID_TIME_AGG = ("sum", "last", "avg", "none")` to
  `VALID_TIME_AGG = ("sum", "last", "none")`.
- In `_parse_measure`, immediately before the `if time_agg not in
  VALID_TIME_AGG:` check, add:

```python
    if time_agg == "avg":
        raise SemanticsError(
            f"{fname}: {where}: time_agg 'avg' isn't computed anywhere yet — "
            f"snapshot models always read the period's final snapshot; use "
            f"'last' until averaging-over-snapshots is implemented"
        )
```

In `src/wh/metrics/compiler.py` `_check_compare`, change
`if m.time_agg in ("last", "avg") and "fytd" in compare:` to
`if m.time_agg == "last" and "fytd" in compare:` (avg can no longer load).

- [ ] **Step 4: Run tests, fix any avg-using fixtures**

Run: `grep -rn "time_agg: avg\|time_agg=\"avg\"\|time_agg='avg'" tests/ src/ docs/ semantics/`
Update any hit in tests to `last` (or delete the case if it specifically
tested avg's acceptance). Then:
Run: `set -o pipefail; uv run pytest -q` — all pass.

- [ ] **Step 5: Update docs and commit**

In `docs/metrics.md`: in the measure-key table row for `time_agg:` (line
~120) remove `avg` from the vocabulary and note `avg` is reserved until
averaging-over-snapshots is implemented; in the compare matrix (~line 233)
change the `time_agg: last`/`avg` row label to `time_agg: last (stocks)`.

```bash
git add src/wh/metrics/loader.py src/wh/metrics/compiler.py tests/metrics/ docs/metrics.md
git commit -m "Reject time_agg avg until averaging is actually implemented

avg compiled identically to last (the as-at join always reads the final
snapshot), so the governed definition carried a label with no computational
meaning — and hashed it. Load error until real period averaging lands."
```

---

## Task 4: Check key uniqueness per (table, key_column) (finding F1)

**Files:**
- Modify: `src/wh/metrics/checks.py:22-30`
- Test: `tests/metrics/test_checks.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_checks.py` (it already imports `bind_checks`,
`SemanticsError`, and uses the `con`/`make_defs` fixtures — mirror the
imports of the existing tests there if any name is missing):

```python
ROLEPLAY_YAML = """\
dimensions:
  home_clinic:
    table: main.roleplay_dim
    key_column: code_a
    attributes: {label: label}
  treating_clinic:
    table: main.roleplay_dim
    key_column: code_b
    attributes: {tlabel: label}
roleplay:
  fact: main.roleplay_fact
  time: {column: d}
  dimensions:
    home_clinic: {shared: fk_a}
    treating_clinic: {shared: fk_b}
  measures:
    n: {description: count, expr: "count(*)"}
"""


def test_roleplaying_dims_check_each_key_column(con, make_defs):
    defs = make_defs(ROLEPLAY_YAML)
    con.execute("""
        CREATE TABLE main.roleplay_dim AS FROM (VALUES
            (1, 9, 'a'), (2, 9, 'b')       -- code_a unique, code_b duplicated
        ) t(code_a, code_b, label)
    """)
    con.execute(
        "CREATE TABLE main.roleplay_fact AS "
        "FROM (VALUES (DATE '2026-01-05', 1, 9)) t(d, fk_a, fk_b)"
    )
    with pytest.raises(SemanticsError, match="code_b"):
        bind_checks(con, defs["roleplay"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_checks.py::test_roleplaying_dims_check_each_key_column -v`
Expected: FAIL — `bind_checks` passes today because `main.roleplay_dim` is
dedup'd by table name after the first (unique) key is checked.

- [ ] **Step 3: Implement**

In `src/wh/metrics/checks.py` `bind_checks`, replace:

```python
    seen_tables = set()
    for dname, ref in model.dims.items():
        dim = ref.shared
        if dim is None:
            continue
        if dim.table not in seen_tables:
            seen_tables.add(dim.table)
            _check_dim_key_unique(con, dim)
```

with:

```python
    seen_keys: set[tuple] = set()
    for dname, ref in model.dims.items():
        dim = ref.shared
        if dim is None:
            continue
        # role-playing dims reuse a table through DIFFERENT key columns —
        # each (table, key) pair needs its own uniqueness check
        if (dim.table, dim.key_column) not in seen_keys:
            seen_keys.add((dim.table, dim.key_column))
            _check_dim_key_unique(con, dim)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/metrics/test_checks.py -v` then
`set -o pipefail; uv run pytest -q` — all pass.

- [ ] **Step 5: Commit**

```bash
git add src/wh/metrics/checks.py tests/metrics/test_checks.py
git commit -m "Check dim-key uniqueness per (table, key_column), not per table

Two shared dims role-playing the same table through different key columns
only had the first key checked; a duplicated second key silently fanned
out every join through it (review finding F1)."
```

---

## Task 5: Measures may only read the fact table (finding F7)

A scalar subquery (`sum(x) + (SELECT count(*) FROM other)`) slips past both
the fact-only-column check and the aggregate check, making a measure's
identity depend on non-fact state under a stable hash.

**Files:**
- Modify: `src/wh/metrics/checks.py`
- Test: `tests/metrics/test_checks.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_checks.py`:

```python
def test_measure_subquery_on_another_table_is_rejected(con, make_defs):
    defs = make_defs("""\
events:
  fact: main.waitlist_removals
  time: {column: removal_date}
  measures:
    n:
      description: leaky
      expr: "count(*) + (SELECT count(*) FROM main.clinic_dim)"
""")
    with pytest.raises(SemanticsError, match="clinic_dim"):
        bind_checks(con, defs["events"])


def test_measure_subquery_on_the_fact_itself_is_fine(con, make_defs):
    defs = make_defs("""\
events:
  fact: main.waitlist_removals
  time: {column: removal_date}
  measures:
    share:
      description: share of all removals
      expr: "count(*) / (SELECT count(*) FROM main.waitlist_removals)"
""")
    assert bind_checks(con, defs["events"]) == []
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `uv run pytest tests/metrics/test_checks.py -v -k subquery`
Expected: the rejection test FAILS (bind_checks returns `[]` today); the
fact-itself test may already pass — keep it as a pin for the new rule.

- [ ] **Step 3: Implement**

In `src/wh/metrics/checks.py`, extend `_check_fact_only_refs`. First add a
tree-walking helper next to `_column_refs` (note the parse tree shape:
`BASE_TABLE` nodes carry `catalog_name` / `schema_name` / `table_name`,
empty strings when unqualified — verified against the running DuckDB):

```python
def _table_refs(con, sql: str, described: str) -> list[tuple[str, str]]:
    (raw,) = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    tree = json.loads(raw)
    if tree.get("error"):
        raise SemanticsError(
            f"unparseable {described}: {tree.get('error_message')}"
        )
    refs: list[tuple[str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "BASE_TABLE":
                refs.append(
                    (node.get("schema_name", "").lower(),
                     node.get("table_name", "").lower())
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(tree)
    return refs
```

Then in `_check_fact_only_refs`, inside the per-part loop (after the
existing column-ref check), add:

```python
            parts = model.fact.lower().split(".")
            fschema = parts[-2] if len(parts) > 1 else ""
            ftable = parts[-1]
            for schema, table in _table_refs(con, sql, f"{label} of measure '{m.name}'"):
                if table != ftable or (schema and schema != fschema):
                    raise SemanticsError(
                        f"model '{model.name}': measure '{m.name}': {label} may "
                        f"read the fact table only — it references "
                        f"'{schema + '.' if schema else ''}{table}'"
                    )
```

(Hoist the `parts`/`fschema`/`ftable` computation above the measure loop —
it's per-model, not per-fragment.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/metrics/test_checks.py -v` then the full suite.
The wrapping template itself (`SELECT {x} FROM {fact}`) contributes one
BASE_TABLE ref — the fact — which passes the rule by construction; every
fixture measure must still bind clean.

- [ ] **Step 5: Commit**

```bash
git add src/wh/metrics/checks.py tests/metrics/test_checks.py
git commit -m "Reject measure fragments that read tables other than the fact

A scalar subquery on another table passed both bind checks, letting a
measure's identity depend on state outside the fact under a stable hash
(review finding F7). Base-table refs in the fragment parse tree must now
all resolve to the fact."
```

---

## Task 6: `wh.last` requires n >= 1 (finding F6)

**Files:**
- Modify: `src/wh/metrics/context_ops.py` (`last`)
- Test: `tests/metrics/test_context.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_context.py` (it already imports the verb
surface; use whatever alias the file imports — the module exposes `last`):

```python
def test_last_rejects_zero_and_negative_n():
    with pytest.raises(SemanticsError, match="n >= 1"):
        last(0, "day")
    with pytest.raises(SemanticsError, match="n >= 1"):
        last(-3, "month")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_context.py::test_last_rejects_zero_and_negative_n -v`
Expected: FAIL — both construct silently today.

- [ ] **Step 3: Implement**

In `src/wh/metrics/context_ops.py`, replace `last`:

```python
def last(n: int, unit: str) -> LastPeriods:
    if unit not in _UNITS:
        raise SemanticsError(f"wh.last unit must be one of {', '.join(_UNITS)}")
    n = int(n)
    if n < 1:
        raise SemanticsError(
            "wh.last needs n >= 1 — zero or negative windows can only be empty"
        )
    return LastPeriods(n, unit)
```

- [ ] **Step 4: Run tests** — target test, then full suite.

- [ ] **Step 5: Commit**

```bash
git add src/wh/metrics/context_ops.py tests/metrics/test_context.py
git commit -m "wh.last requires n >= 1

last(0, ...) resolved to an inverted (always-empty) window and negative n
to a window in the future — both silently returned nothing (review F6)."
```

---

## Task 7: Anchor relative time to the max DATE, not the max instant (finding F5)

On a TIMESTAMP time column, `wh.last(7, "day")` produced a mid-day lower
bound (dropping the first day's early rows) while `wh.last(1, "month")`
produced a whole-day bound — inconsistent with each other and with the
documented "max date" anchor.

**Files:**
- Modify: `src/wh/metrics/context_ops.py` (`Context.resolve`)
- Test: `tests/metrics/test_context.py`

- [ ] **Step 1: Write the failing test**

```python
def test_datetime_anchor_floors_to_its_date():
    c = context(time=last(7, "day"))
    r = c.resolve(anchor=datetime(2026, 7, 15, 9, 30)).entries["time"]
    assert r == Between(date(2026, 7, 9), date(2026, 7, 15))
    c = context(time=last(1, "month"))
    r = c.resolve(anchor=datetime(2026, 7, 15, 9, 30)).entries["time"]
    assert r == Between(date(2026, 6, 16), date(2026, 7, 15))
```

Add `from datetime import date, datetime` and `Between` to the test file's
imports if missing.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_context.py::test_datetime_anchor_floors_to_its_date -v`
Expected: FAIL — day unit currently yields
`Between(datetime(2026,7,9,9,30), datetime(2026,7,15,9,30))`.

- [ ] **Step 3: Implement**

In `src/wh/metrics/context_ops.py`:
- Add `datetime` to the datetime import: `from datetime import date,
  datetime, timedelta`.
- In `Context.resolve`, floor the anchor ONCE at the top and delete the
  in-loop `anchor = _as_date(anchor)` line:

```python
    def resolve(self, anchor: date) -> Context:
        """Plain-data snapshot: widgets read now, relative time anchored to
        `anchor` (the fact's max DATE — same context + same mirror = same
        rows). A datetime anchor floors to its date so every unit gets
        whole-day windows; day-inclusive compilation keeps the anchor day."""
        anchor = _as_date(anchor)
        if isinstance(anchor, datetime):   # BEFORE date — datetime is a date subclass
            anchor = anchor.date()
```

(The rest of the method body is unchanged apart from removing the now-dead
`anchor = _as_date(anchor)` inside the `LastPeriods` branch.)

- [ ] **Step 4: Run tests** — target test, then full suite. The resolved
`hi` is now a plain date, which compiles through the day-inclusive
`>= lo AND < hi + 1 day` lane — the max row is still included.

- [ ] **Step 5: Commit**

```bash
git add src/wh/metrics/context_ops.py tests/metrics/test_context.py
git commit -m "Floor datetime anchors to their date when resolving relative time

wh.last(n, 'day'/'week') on a TIMESTAMP column carried the max row's
time-of-day into the lower bound, silently dropping the first day's early
rows — and disagreeing with the month/year units, which already produced
whole-day windows (review finding F5)."
```

---

## Task 8: `values()` treats all-`All` contexts as unfiltered (finding F8)

An untouched widget resolves to `All()`; a context of only `All` entries
must take the same (dimension-table) lane as no context at all, so option
lists don't flicker as unrelated widgets pass through their empty state.

**Files:**
- Modify: `src/wh/metrics/compiler.py` (`compile_values`)
- Test: `tests/metrics/test_values.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_values.py` (fixture `defs` provides the
design models; `con` the seeded mirror — mirror the existing tests' use of
`compile_values`). The seeded `main.clinic_dim` has 4 clinics but only 3
appear in `main.waitlist_removals`:

```python
def test_all_only_context_reads_the_dim_table_like_no_context(con, defs):
    from wh.metrics.compiler import compile_values
    from wh.metrics.context_ops import All, Context

    bare = [r[0] for r in con.execute(
        compile_values(defs["removals"], "facility.clinic")
    ).fetchall()]
    allctx = [r[0] for r in con.execute(
        compile_values(
            defs["removals"], "facility.clinic",
            Context({"facility__region": All()}),
        )
    ).fetchall()]
    assert allctx == bare
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_values.py::test_all_only_context_reads_the_dim_table_like_no_context -v`
Expected: FAIL — the `All` context takes the fact-scan lane and misses the
clinic with no removals (`Range Clinic`).

- [ ] **Step 3: Implement**

In `src/wh/metrics/compiler.py` `compile_values`, replace the first lines
of the body after `item, dim = _by_item(...)` / `lhs = ...`:

```python
    # an untouched widget resolves to All() — "effectively unfiltered" must
    # take the same lane as "unfiltered", or option lists flicker as other
    # widgets pass through their empty state
    ctx = Context({k: v for k, v in ctx.entries.items() if not isinstance(v, All)})
    if not ctx.entries and dim is not None:
```

(`Context` needs importing in the compiler:
`from .context_ops import All, Between, Context, Eq, In, Not, EMPTY, _months_back`
— extend the existing import line.)

- [ ] **Step 4: Run tests** — target, then full suite.

- [ ] **Step 5: Commit**

```bash
git add src/wh/metrics/compiler.py tests/metrics/test_values.py
git commit -m "values(): an all-All context takes the dimension-table lane

A context holding only All() entries (what untouched widgets resolve to)
fell into the fact-scan lane, so 'effectively unfiltered' returned a
different option list than 'unfiltered' and cascaded widgets flickered
(review finding F8)."
```

---

## Task 9: One output-namespace check (finding F4 / review 2B)

Duplicate output columns (measure vs local dim, `by=` vs generated
`{measure}_{cmp}`, plain duplicates in `measures=`/`by=`) currently reach
DuckDB and come back as duplicate-named Arrow columns.

**Files:**
- Modify: `src/wh/metrics/loader.py` (dim/measure overlap at load)
- Modify: `src/wh/metrics/compiler.py` (`compile_slice`)
- Test: `tests/metrics/test_loader.py`, `tests/metrics/test_compiler.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/metrics/test_loader.py`:

```python
def test_dim_and_measure_sharing_a_name_is_a_load_error(make_defs):
    with pytest.raises(SemanticsError, match="both"):
        make_defs("""\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    n: category
  measures:
    n: {description: n, expr: "count(*)"}
""")
```

Append to `tests/metrics/test_compiler.py`:

```python
def test_duplicate_output_columns_error(defs):
    with pytest.raises(SemanticsError, match="produced twice"):
        compile_slice(defs["removals"], ["removals", "removals"])
    with pytest.raises(SemanticsError, match="produced twice"):
        compile_slice(
            defs["removals"], ["removals"],
            by=["facility.region", "facility.region"],
        )


def test_by_entry_colliding_with_comparison_column_errors(make_defs):
    defs = make_defs("""\
events:
  fact: main.events
  time: {column: d}
  dimensions:
    n_prior: category
  measures:
    n: {description: n, expr: "count(*)"}
""")
    with pytest.raises(SemanticsError, match="n_prior"):
        compile_slice(
            defs["events"], ["n"], by=["n_prior"], grain="month",
            compare=["prior"],
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/metrics/test_loader.py::test_dim_and_measure_sharing_a_name_is_a_load_error tests/metrics/test_compiler.py -v -k "duplicate_output or colliding_with_comparison"`
Expected: 3 FAILs — all compile/load silently today.

- [ ] **Step 3: Implement the load-time overlap check**

In `src/wh/metrics/loader.py` `_parse_model`, after `measures = {...}` is
built and before the `return Model(...)`:

```python
    overlap = dims.keys() & measures.keys()
    if overlap:
        raise SemanticsError(
            f"{fname}: model '{name}': {', '.join(sorted(overlap))} named as "
            f"both a dimension and a measure — every output column needs one "
            f"meaning; rename one of them"
        )
```

- [ ] **Step 4: Implement the compile-time namespace check**

In `src/wh/metrics/compiler.py` `compile_slice`, after the
`_check_compare(...)` call and before `split_context(...)`:

```python
    # one namespace: every output column named exactly once. by= aliases
    # come from _by_item (which also validates the entries), generated
    # comparison columns are {measure}_{cmp}
    out_cols = ["period"] if grain is not None else []
    for entry in by:
        item, _dim = _by_item(model, entry)
        out_cols.append(item.rsplit(" AS ", 1)[1].strip('"'))
    out_cols += list(measures)
    out_cols += [f"{m}_{c}" for m in measures for c in compare]
    seen: set[str] = set()
    for col in out_cols:
        low = col.lower()
        if low in seen:
            raise SemanticsError(
                f"output column '{col}' would be produced twice — rename a "
                f"measure or dimension, or drop the duplicate entry"
            )
        seen.add(low)
```

Note: `_check_compare`'s existing `{name}_{cmp}`-vs-declared-measures check
stays — it catches declaration-level ambiguity even when the colliding
measure isn't in this slice's output.

- [ ] **Step 5: Run tests** — the three new tests, then the full suite
(`test_comparison_suffix_collision_errors` must still pass unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/wh/metrics/loader.py src/wh/metrics/compiler.py tests/metrics/
git commit -m "Enforce a single output-column namespace per slice

Duplicate output names reached DuckDB silently: a measure and local dim
sharing a name, a by= entry colliding with a generated comparison column,
or plain duplicate measures=/by= entries all produced duplicate-named
Arrow columns that break dataframe backends (review finding F4). Dims and
measures also can't share a name at load."
```

---

## Task 10: Half-open time windows (findings F3 + review 2C)

Replace the compiler-internal inclusive `Between` time lane with
`TimeWindow(lo, hi_exc)` built once in `split_context`. The surface
`Between` — and context hashing/serialisation/provenance — is untouched.
This retires the inclusive-bound bug class: F3 (datetime month-end clamp in
compare shifts) falls out, `_time_predicate` loses its type dispatch,
`_fytd_lines` loses its duplicated hi branch, `_shift_op`'s asymmetric
exclusive-trick becomes uniform arithmetic.

**Files:**
- Modify: `src/wh/metrics/compiler.py`
- Test: `tests/metrics/test_compare.py`, plus mechanical expected-SQL
  updates found by grep

- [ ] **Step 1: Write the failing regression test for F3**

Append to `tests/metrics/test_compare.py` (model shape mirrors the
existing `test_shifted_windows_keep_month_end_days` at line ~150 — read it
first and reuse its YAML/DDL if it already builds a suitable event fact):

```python
DT_EVENTS_YAML = """\
dt_events:
  fact: main.dt_events
  time: {column: at}
  measures:
    n: {description: n, expr: "count(*)"}
"""


def test_shifted_windows_keep_month_end_days_for_datetime_bounds(con, make_defs):
    defs = make_defs(DT_EVENTS_YAML)
    con.execute("""
        CREATE TABLE main.dt_events AS FROM (VALUES
            (TIMESTAMP '2026-05-31 14:00:00'),
            (TIMESTAMP '2026-06-10 09:00:00')
        ) t(at)
    """)
    c = compile_slice(
        defs["dt_events"], ["n"], grain="month", compare=["prior"],
        ctx=context(time=(datetime(2026, 6, 1, 0, 0),
                          datetime(2026, 6, 30, 23, 59, 59))),
    )
    rows = con.execute(c.sql).fetchall()
    june = [r for r in rows if r[0] == date(2026, 6, 1)][0]
    assert june[2] == 1   # n_prior must include the May 31 afternoon row
```

Add `from datetime import date, datetime` to the imports if missing. Check
how `context(time=...)` accepts datetimes: the tuple form passes them
through `_as_date` (strings only) so datetime objects survive — this is
the reachable path from `time=(lo, hi)`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_compare.py::test_shifted_windows_keep_month_end_days_for_datetime_bounds -v`
Expected: FAIL — `n_prior` is None/0: the shifted window ends May 30
23:59:59 and the May 31 row is dropped.

- [ ] **Step 3: Implement `TimeWindow` and the conversions**

In `src/wh/metrics/compiler.py`:

**(a)** Add below the `Compiled` dataclass:

```python
@dataclass(frozen=True)
class TimeWindow:
    """Compiler-internal time lane: [lo, hi_exc) — lo inclusive instant,
    hi_exc exclusive. The surface Between (inclusive both ends, what
    contexts hash and serialise) converts here exactly once; every
    consumer does half-open arithmetic and no bound ever needs a
    day-clamp special case again."""
    lo: object
    hi_exc: object


def _to_window(op):
    if not isinstance(op, Between):
        return op
    # a date hi means "the whole day"; a datetime hi means "up to this
    # instant" (DuckDB timestamps are microsecond precision, so +1us is
    # the exact exclusive bound)
    hi_exc = (
        op.hi + timedelta(microseconds=1) if isinstance(op.hi, datetime)
        else op.hi + timedelta(days=1)
    )
    return TimeWindow(op.lo, hi_exc)
```

**(b)** In `split_context`, convert at the single entry point — change
`time_op = op` to `time_op = _to_window(op)` (inside the `if key ==
"time":` branch).

**(c)** Replace `_time_predicate` entirely:

```python
def _time_predicate(lhs: str, op) -> str | None:
    """Half-open: >= lo AND < hi_exc. Day-inclusive date bounds and exact
    datetime bounds both normalised into hi_exc by _to_window."""
    if not isinstance(op, TimeWindow):
        return _predicate(lhs, op)
    return f"{lhs} >= {_lit(op.lo)} AND {lhs} < {_lit(op.hi_exc)}"
```

**(d)** Replace `_shift_op` with `_shift_window` (keep `_shift_back` for
the inclusive lo):

```python
def _shift_exc(t, months: int, days: int):
    """Shift an EXCLUSIVE bound. Date bounds sit on period starts and
    shift without clamping. A datetime keeps its time-of-day while its
    day shifts through the same exclusive-day arithmetic — Jun 30
    23:59:59 maps to May 31 23:59:59, never May 30 (clamping the day
    would silently drop end-of-month rows from every comparison)."""
    if months:
        if isinstance(t, datetime):
            day = _months_back(t.date() + timedelta(days=1), months) - timedelta(days=1)
            t = datetime.combine(day, t.time())
        else:
            t = _months_back(t, months)
    return t - timedelta(days=days) if days else t


def _shift_window(op, months: int, days: int):
    if not isinstance(op, TimeWindow):
        return op
    return TimeWindow(
        _shift_back(op.lo, months, days), _shift_exc(op.hi_exc, months, days)
    )
```

**(e)** Update the consumers:

- `_assemble_compare`: `shifted = _shift_window(time_op, months, days)`;
  `scan_lo[cmp] = shifted.lo if isinstance(shifted, TimeWindow) else None`;
  the fytd branch's `isinstance(time_op, Between)` → `isinstance(time_op,
  TimeWindow)`.
- `_fytd_lines`: replace the datetime/date `hi` branch with:

```python
    if isinstance(time_op, TimeWindow):   # mid-period truncation carries in
        predicates.append(f"{tc} < {_lit(time_op.hi_exc)}")
```

- `_complete_predicate`: `isinstance(time_op, Between)` →
  `isinstance(time_op, TimeWindow)`, and the clamp becomes
  `cond += f" AND {period_end} < {_lit(time_op.hi_exc)}"`.

**(f)** Delete now-unused code: the old `_shift_op` body and, if nothing
else references it, remove `Between` from the compiler's context_ops import
only if truly unused (`_to_window` still needs it — keep it).

- [ ] **Step 4: Update expected SQL in existing tree tests**

The emitted predicate changes shape (semantics identical):
- `fact.col >= DATE 'LO' AND fact.col < DATE 'HI' + INTERVAL 1 DAY`
  becomes `fact.col >= DATE 'LO' AND fact.col < DATE 'HI+1day'`
  (e.g. `< DATE '2026-06-30' + INTERVAL 1 DAY` → `< DATE '2026-07-01'`).
- `fact.col <= TIMESTAMP 'X'` becomes
  `fact.col < TIMESTAMP 'X + 1 microsecond'`
  (e.g. `<= TIMESTAMP '2026-06-30 23:59:59'` →
  `< TIMESTAMP '2026-06-30 23:59:59.000001'`).

Run: `grep -rn "INTERVAL 1 DAY" tests/metrics/` and
`grep -rn "<= TIMESTAMP" tests/metrics/` — update each *expected* SQL
string in `assert_sql_equiv` calls by the two rules above. Do NOT touch
seeded data or canary needles that target the LO bound; if a canary needle
is a HI date literal, move the needle to the corresponding new literal
(next day).

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/metrics/test_compare.py -v` (the new regression
test now passes, `test_shifted_windows_keep_month_end_days` and
`test_yoy_keeps_leap_day` still pass — they pin the date path), then
`set -o pipefail; uv run pytest -q`.

- [ ] **Step 6: Commit**

```bash
git add src/wh/metrics/compiler.py tests/metrics/
git commit -m "Compile time windows half-open: TimeWindow(lo, hi_exc)

The inclusive-Between time lane needed a special case at every consumer
(predicate type dispatch, the exclusive-bound trick in compare shifts,
a duplicated branch in fytd) and the class kept escaping: datetime upper
bounds still day-clamped in shifted windows, silently dropping month-end
rows from every prior/yoy value (review finding F3, fourth escape of this
class). Between now converts once in split_context to an exclusive upper
instant; shifting and predicates are uniform arithmetic. Surface contexts,
hashing and serialisation are unchanged."
```

---

## Task 11: Quote generated aliases (review 2F)

YAML-authored names are validated as plain identifiers (the injection
firewall — unchanged) but emitted unquoted, so a measure legitimately named
`filter` or a dim named `order` dies at bind-time EXPLAIN with a raw parser
error. Quote every YAML-derived alias and every reference to one. Column
and table references stay UNQUOTED on purpose: quoting would flip DuckDB to
case-sensitive matching and break YAML that spells a column in different
case than the mirror.

**Files:**
- Modify: `src/wh/metrics/compiler.py`
- Test: `tests/metrics/test_compiler.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_compiler.py`:

```python
def test_reserved_word_names_compile_and_execute(con, make_defs):
    defs = make_defs("""\
events:
  fact: main.waitlist_removals
  time: {column: removal_date}
  dimensions:
    order: removal_reason
  measures:
    filter: {description: n, expr: "count(*)"}
""")
    c = compile_slice(defs["events"], ["filter"], by=["order"], grain="month")
    res = con.execute(c.sql)
    cols = [d[0] for d in res.description]
    assert "filter" in cols and "order" in cols
    assert res.fetchall()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/metrics/test_compiler.py::test_reserved_word_names_compile_and_execute -v`
Expected: FAIL — DuckDB parser error on `AS order` / `AS filter`.

- [ ] **Step 3: Implement the quoting**

In `src/wh/metrics/compiler.py`, change every emission of a YAML-derived
alias and every reference to one (compiler-owned tokens — `period`,
`__asat`, `__cell_n`, `value`, `b` — stay bare):

- `_inner_lines`:
  - `select.append(f"{m.ratio[0]} AS \"__{name}_num\"")` and
    `select.append(f"{m.ratio[1]} AS \"__{name}_den\"")`
  - `select.append(f'{_measure_sql(m)} AS "{name}"')`
- `_by_item` local-dim return:
  `return f'fact.{ref.fact_column} AS "{dname}"', None`
  (the shared-dim item is already quoted; `group_aliases` picks up the
  quoted alias automatically via the existing `rsplit(" AS ", 1)[1]`).
- `_fytd_lines` measure lines: same three quoted-alias forms as
  `_inner_lines` (`"__{name}_num"`, `"__{name}_den"`, `"{name}"`).
- `_sel` — quote the measure-column references and the output alias:

```python
def _sel(prefix: str, name: str, m, alias: str | None = None,
         suppress: int | None = None) -> str:
    p = f"{prefix}." if prefix else ""
    a = f'"{alias or name}"'
    if m.ratio:
        e = f'CAST({p}"__{name}_num" AS DOUBLE) / NULLIF({p}"__{name}_den", 0)'
        if suppress is not None:
            e = (
                f'CASE WHEN {p}__cell_n < {suppress} '
                f'OR {p}"__{name}_den" < {suppress} THEN NULL ELSE {e} END'
            )
        return f"{e} AS {a}"
    if suppress is not None:
        return (
            f'CASE WHEN {p}__cell_n < {suppress} THEN NULL ELSE {p}"{name}" END '
            f"AS {a}"
        )
    return f'{p}"{name}" AS {a}'
```

(The docstring of `_sel` stays; note the plain-path now always emits an
alias — the parse tree is identical because the alias equals the column
name.)

`group_aliases` entries carry their own quoting, so `_assemble_compare`'s
`b.{g}` / `{name}.{g}` joins and `_fytd_lines`' `p.{a}` references need no
change.

- [ ] **Step 4: Run the full suite**

Run: `set -o pipefail; uv run pytest -q`
Expected: PASS with no expected-SQL updates — quotes are lexical, the
parse trees `assert_sql_equiv` compares are identical for `AS n` vs
`AS "n"`. Any tree diff here means a name changed case or content — fix
the emission, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/wh/metrics/compiler.py tests/metrics/test_compiler.py
git commit -m "Quote YAML-derived aliases in generated SQL

A measure or dim legitimately named filter/order passed the loader and
died at bind-time EXPLAIN with a raw parser error (review 2F). Aliases
and references to them are now quoted; column and table refs stay
unquoted so case-insensitive matching against the mirror is preserved.
Identifier validation is unchanged — quoting is belt and braces, not a
substitute for the injection firewall."
```

---

## Task 12: Capture provenance data at execution time (review 2D)

`.provenance()` currently re-runs the as-at queries, refresh-stamp lookups
and schema DESCRIBEs at call time; if the mirror refreshed between
`.frame()` and `.provenance()` (the session connection reopens lazily), the
provenance describes data the number was never computed from.

**Files:**
- Modify: `src/wh/metrics/provenance.py` (extract `capture_data`)
- Modify: `src/wh/metrics/result.py` (`Slice.__init__`, `frame`,
  `provenance`)
- Test: `tests/metrics/test_provenance.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/metrics/test_provenance.py` (mirror the existing tests'
way of building a `Slice` — they construct via `BoundModel`/workspace or
directly; use the direct form):

```python
def test_provenance_describes_the_executed_frame_not_current_data(con, defs):
    from wh.metrics.result import Slice

    s = Slice(defs["waitlist"], ["patients_waiting"], grain="month",
              con=lambda: con)
    s.frame()
    before = s.provenance().data["as_at"]["base"]
    con.execute(
        "INSERT INTO main.waitlist VALUES "
        "(DATE '2026-07-17','C1','D1','Cat 1','U1',142,90)"
    )
    after = s.provenance().data["as_at"]["base"]
    assert after == before          # captured at frame(), not re-derived


def test_provenance_without_frame_reads_current_data(con, defs):
    from wh.metrics.result import Slice

    s = Slice(defs["waitlist"], ["patients_waiting"], grain="month",
              con=lambda: con)
    assert s.provenance().data["as_at"]["base"]   # still works, fresh gather
```

(If the seeded `main.waitlist` already has a 2026-07-17 row, pick a later
date — the INSERT must move the July as-at moment.)

- [ ] **Step 2: Run tests to verify the first fails**

Run: `uv run pytest tests/metrics/test_provenance.py -v -k executed_frame`
Expected: FAIL — the second `provenance()` call re-runs the as-at query
and sees the inserted row.

- [ ] **Step 3: Extract `capture_data` in provenance.py**

Turn `Provenance._gather_data` into a module-level function; the only
`self` uses are the model and the truncation block's
`self.context["applied"]`, which recomputes from `args`:

```python
def capture_data(c, model: Model, compiled, args: dict) -> dict:
    """Everything data-side a number needs to explain itself, gathered on
    the connection (and at the moment) the number was computed."""
```

Body = the current `_gather_data` body with `self._model` → `model`,
`self._warnings_in` dropped (warnings stay on the Provenance object), the
`_refreshed_at` call becoming `_refreshed_at(c, t)` (make `_refreshed_at` a
module function too — it never uses `self`), `"warnings"` removed from the
returned dict, `"model_hash"` removed from the returned dict, and the
truncation block's first lines replaced with:

```python
    truncation = None
    entries = args["ctx"].to_dict()
    time_entry = entries.get("time") if "time" in compiled.applied else None
    if time_entry and "between" in time_entry and args["grain"]:
```

In `Provenance.__init__`, add a `data=None` keyword and replace
`self.data = self._gather_data(c, compiled, args)` with:

```python
        self.data = dict(data) if data is not None else capture_data(
            c, model, compiled, args
        )
        self.data["warnings"] = list(self._warnings_in)
        self.data["model_hash"] = self._model_hash
```

(Warnings and model hash are definition-side — they belong to the
Provenance object, not the capture, so a capture taken at `frame()` doesn't
freeze them.) Delete the old `_gather_data` method and the method form of
`_refreshed_at`.

- [ ] **Step 4: Wire the capture into `Slice`**

In `src/wh/metrics/result.py`:
- `Slice.__init__`: add `self._data_capture = None` (before the ctx
  resolution).
- `frame()`:

```python
    def frame(self, backend: str | None = None):
        from ..frames import default_backend, from_arrow
        from .provenance import capture_data

        con = self._con()
        table = con.sql(self.sql).to_arrow_table()
        if self._data_capture is None:
            # provenance must describe THIS execution — capture the
            # data-side facts on the same connection, at the same moment
            self._data_capture = capture_data(
                con, self._model, self._compiled, self._args
            )
        return from_arrow(
            table, backend or self._preferred_backend or default_backend()
        )
```

- `provenance()`:

```python
        return Provenance(
            self._model, self._compiled, self._args, self._con,
            self._warnings, data=self._data_capture,
        )
```

(`Provenance.__init__`'s signature gains `data=None` after `warnings` —
keep the parameter order `(model, compiled, args, con, warnings=(),
data=None)`.)

- [ ] **Step 5: Run tests** — the two new tests, then the full suite.
`.view()` and `.suppress()` intentionally don't capture (a `.view()` runs
in SQL cells outside our control; a suppressed Slice captures on its own
first `frame()`).

- [ ] **Step 6: Commit**

```bash
git add src/wh/metrics/provenance.py src/wh/metrics/result.py tests/metrics/test_provenance.py
git commit -m "Capture provenance data when the slice executes

provenance() re-ran the as-at, refresh-stamp and schema queries at call
time, so a mirror refresh between frame() and provenance() produced a
provenance describing data the number was never computed from (review
2D). frame() now captures the data-side facts on its own connection;
provenance() reuses the capture, gathering fresh only for never-executed
slices."
```

---

## Task 13: Docs sweep, CLAUDE.md, final gate

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/metrics.md` (final consistency pass)

- [ ] **Step 1: Sweep docs for stale statements**

Run: `grep -n "INTERVAL 1 DAY\|<= TIMESTAMP\|time_agg\|facility: clinic_code" docs/metrics.md README.md`
Fix any example or prose that still shows implicit shared linking, `avg`,
or the old inclusive-bound phrasing. The "day-inclusive" user-facing
contract in docs/metrics.md is unchanged in MEANING — only rewrite text
that describes the emitted SQL shape, if any.

- [ ] **Step 2: Update CLAUDE.md**

- Add to **Current state**: `Semantic-compiler hardening COMPLETE
  (2026-07-21): all 8 findings of
  docs/reviews/2026-07-21-semantic-compiler-adversarial-review.md fixed
  plus review items 2A/2B/2C/2D/2E/2F — strict YAML keys, explicit
  {shared: ...} dim linking (bare = local, always), per-(table,key)
  uniqueness checks, fact-only table refs, wh.last validation +
  date-floored anchors, all-All values() lane, output-namespace check,
  half-open TimeWindow time lane, quoted aliases, time_agg avg rejected,
  provenance captured at frame().`
- Add to **Metrics-layer notes** (design invariants):
  - `Time lane is HALF-OPEN internally: surface Between converts once in
    split_context to TimeWindow(lo, hi_exc); shifting/predicates/
    completeness never special-case bound types. Don't reintroduce
    inclusive-hi arithmetic — this class escaped four times.`
  - `Bare dim names are ALWAYS local; shared linking is explicit
    ({shared: key}); a bare name matching a shared dim is a load error.`
  - `Provenance data is captured at frame() on the executing connection;
    provenance() must not re-derive data-side facts for executed slices.`

- [ ] **Step 3: Full gate and commit**

```bash
set -o pipefail
uv run pytest -q
uv run wh validate
git add CLAUDE.md docs/
git commit -m "Update CLAUDE.md and docs for the semantic-compiler hardening"
git push -u origin claude/semantic-compiler-review-jdv33t
```

---

## Self-review notes

- **Coverage against the review:** F1→Task 4, F2→Task 1, F3→Task 10,
  F4→Task 9, F5→Task 7, F6→Task 6, F7→Task 5, F8→Task 8, 2A→Task 1,
  2B→Task 9, 2C→Task 10, 2D→Task 12, 2E→Task 3, 2F→Task 11, dim
  syntax→Task 2. Nothing from the agreed scope is unassigned.
- **Ordering rationale:** loader syntax lands first so every later test is
  written in the final YAML form; Task 10 (TimeWindow) precedes Task 11
  (quoting) so the expected-SQL updates happen once; Task 12 last because
  it restructures provenance around everything already being stable.
- **Known risks called out in-task:** Task 2's grep-and-migrate of
  co-loaded fixtures (the collision error itself names any missed file);
  Task 10's expected-SQL updates (two mechanical rules; canary needles on
  HI literals move to the next day); Task 11 relies on tree-equality being
  quote-blind (it is — quotes are lexical).
