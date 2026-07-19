# warehouse-tools Phase 4 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `wh.read_excel()` (smart header detection for messy business files), `wh.clean()` (composable narwhals cleaners), and `wh.read_csv()` (DuckDB sniffing) — each returning the preferred frame backend, each able to `land=` straight into the workspace DuckDB.

**Architecture:** `src/wh/sources/excel.py` reads raw typed rows via python-calamine (read-only, fast) and turns them into an Arrow table: sheet selection with helpful errors, `header="auto"` scoring the first 20 rows (non-empty × string-ness × distinctness), multi-row headers `header=(3,4)` with horizontal forward-fill for merged cells, name normalisation/dedupe, per-column Arrow inference falling back to string for mixed columns. `src/wh/cleaning.py` (module deliberately NOT named `clean` — see the submodule-shadowing gotcha in CLAUDE.md) exposes a callable `clean` instance whose steps are narwhals `DataFrame -> DataFrame` functions, so they work on polars and pandas alike. `Workspace.read_excel/read_csv` handle backend conversion and the `land=` path, reusing the identifier helpers extracted from `land()`; module-level wrappers fall back to a workspace-free path when no `wh.yaml` exists (reading a messy file shouldn't require a project).

**Tech Stack:** python-calamine (new `[excel]` extra + dev dep), openpyxl (dev-only, to *write* messy fixture files), narwhals, duckdb `read_csv`.

**Conventions:** TDD, `set -o pipefail` before pytest→git chains, no Claude in commits, notebook-friendly `WhError`s, eager-import any new submodule whose name collides with a module-level verb (none here — `cleaning` ≠ `clean`, `sources.excel` ≠ `read_excel` — keep it that way).

---

## Task 1: Dependencies

**Step 1:** Add the extra to `pyproject.toml` under `[project]`:

```toml
[project.optional-dependencies]
excel = ["python-calamine>=0.5"]
```

**Step 2:** `uv add --group dev python-calamine openpyxl`

**Step 3:** `set -o pipefail; uv run pytest -q | tail -1` → 116 passed, 3 skipped. Commit: `chore: excel extra (python-calamine); openpyxl for test fixtures`

---

## Task 2: Cleaners — `clean()`, `snake_names`, `drop_empty`, `strip_strings`

**Files:**
- Create: `src/wh/cleaning.py`
- Test: `tests/test_cleaning.py`

**Step 1: Failing tests** (parametrised over polars and pandas via a `frame` fixture):

```python
import pandas as pd
import polars as pl
import pytest

from wh.cleaning import clean


@pytest.fixture(params=["polars", "pandas"])
def make_frame(request):
    def _make(data: dict):
        return pl.DataFrame(data) if request.param == "polars" else pd.DataFrame(data)
    return _make


def as_dict(df) -> dict:
    if isinstance(df, pl.DataFrame):
        return {k: list(v) for k, v in df.to_dict(as_series=False).items()}
    return {k: [None if pd.isna(x) else x for x in v] for k, v in df.to_dict("list").items()}


def test_clean_returns_same_backend(make_frame):
    df = make_frame({"A": [1]})
    out = clean(df)
    assert type(out) is type(df)


def test_snake_names(make_frame):
    df = make_frame({" Referral Date ": [1], "Seen (Days)": [2], "x": [3], "X ": [4]})
    out = clean(df, clean.snake_names)
    assert list(as_dict(out)) == ["referral_date", "seen_days", "x", "x_2"]


def test_drop_empty(make_frame):
    df = make_frame({"a": [1, None, None], "b": ["x", None, "y"], "junk": [None, None, None]})
    out = clean(df, clean.drop_empty)
    d = as_dict(out)
    assert "junk" not in d
    assert d["a"] == [1, None]           # middle all-null row dropped
    assert d["b"] == ["x", "y"]


def test_strip_strings(make_frame):
    df = make_frame({"s": ["  x ", "", "   ", "y"], "n": [1, 2, 3, 4]})
    out = clean(df, clean.strip_strings)
    assert as_dict(out)["s"] == ["x", None, None, "y"]
    assert as_dict(out)["n"] == [1, 2, 3, 4]


def test_steps_compose_in_order(make_frame):
    df = make_frame({" A ": ["  v  ", None], "junk": [None, None]})
    out = clean(df, clean.snake_names, clean.drop_empty, clean.strip_strings)
    assert as_dict(out) == {"a": ["v"]}
```

**Step 2:** Run → collection error. **Step 3: Implement `src/wh/cleaning.py`**

```python
"""Composable cleaners for messy business data.

    df = wh.clean(raw, wh.clean.snake_names, wh.clean.drop_empty,
                  wh.clean.strip_strings, wh.clean.parse_dates("referral_date"),
                  wh.clean.numeric("wait_days"))

Steps are narwhals DataFrame -> DataFrame functions, so they work on polars
and pandas alike; `clean()` returns the same frame type it was given.
(Module named `cleaning`, not `clean`, to avoid the package-attribute
shadowing gotcha — see CLAUDE.md.)
"""

from __future__ import annotations

import re

import narwhals as nw


def _snake(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip()).strip("_").lower() or "col"


def snake_names(df: nw.DataFrame) -> nw.DataFrame:
    mapping, used = {}, set()
    for c in df.columns:
        base = _snake(c)
        n, i = base, 1
        while n in used:
            i += 1
            n = f"{base}_{i}"
        used.add(n)
        mapping[c] = n
    return df.rename(mapping)


def drop_empty(df: nw.DataFrame) -> nw.DataFrame:
    keep = [c for c in df.columns if df.get_column(c).null_count() < len(df)]
    df = df.select(keep)
    if df.columns:
        df = df.filter(~nw.all_horizontal(*[nw.col(c).is_null() for c in df.columns]))
    return df


def strip_strings(df: nw.DataFrame) -> nw.DataFrame:
    cols = [c for c in df.columns if df.schema[c] == nw.String]
    if not cols:
        return df
    return df.with_columns(
        *[
            nw.when(nw.col(c).str.strip_chars() == "")
            .then(None)
            .otherwise(nw.col(c).str.strip_chars())
            .alias(c)
            for c in cols
        ]
    )


class _Clean:
    """Callable pipeline runner that also namespaces the step functions."""

    snake_names = staticmethod(snake_names)
    drop_empty = staticmethod(drop_empty)
    strip_strings = staticmethod(strip_strings)

    def __call__(self, df, *steps):
        ndf = nw.from_native(df, eager_only=True)
        for step in steps:
            ndf = step(ndf)
        return ndf.to_native()


clean = _Clean()
```

**Step 4:** PASS (if narwhals API details differ — e.g. `all_horizontal` arity or `when/then` — adjust to the installed narwhals version, keeping tests as the contract). **Step 5:** Commit: `feat: clean() with snake_names/drop_empty/strip_strings`

---

## Task 3: Cleaners — `parse_dates`, `numeric`

**Step 1: Failing tests**

```python
from datetime import date, datetime


def test_parse_dates_strings(make_frame):
    df = make_frame({"d": ["2026-01-02", None]})
    out = clean(df, clean.parse_dates("d", format="%Y-%m-%d"))
    assert as_dict(out)["d"][0] == datetime(2026, 1, 2)


def test_parse_dates_excel_serials_polars():
    # serial 45658 = 2025-01-01 (origin 1899-12-30)
    df = pl.DataFrame({"d": [45658.0, None]})
    out = clean(df, clean.parse_dates("d"))
    assert out["d"][0] == datetime(2025, 1, 1)


def test_parse_dates_passthrough_datetime(make_frame):
    df = make_frame({"d": [datetime(2026, 1, 1)]})
    out = clean(df, clean.parse_dates("d"))
    assert as_dict(out)["d"] == [datetime(2026, 1, 1)]


def test_numeric(make_frame):
    df = make_frame({"v": ["1,234", "$5.50", " 7 ", "-", "", None]})
    out = clean(df, clean.numeric("v"))
    assert as_dict(out)["v"] == [1234.0, 5.5, 7.0, None, None, None]


def test_numeric_leaves_numbers(make_frame):
    df = make_frame({"v": [1.5, 2.0]})
    out = clean(df, clean.numeric("v"))
    assert as_dict(out)["v"] == [1.5, 2.0]
```

**Step 2:** Run → FAIL. **Step 3: Implement (append to cleaning.py)**

```python
from datetime import datetime as _dt

_EXCEL_EPOCH = _dt(1899, 12, 30)
_US_PER_DAY = 86_400_000_000


def parse_dates(*cols: str, format: str | None = None):
    """Parse string dates (with `format`, e.g. '%d/%m/%Y') and numeric Excel
    serials into datetimes. Datetime columns pass through untouched."""

    def step(df: nw.DataFrame) -> nw.DataFrame:
        exprs = []
        for c in cols:
            dtype = df.schema[c]
            if dtype == nw.String:
                exprs.append(nw.col(c).str.to_datetime(format=format).alias(c))
            elif dtype.is_numeric():
                exprs.append(
                    (
                        (nw.col(c) * _US_PER_DAY)
                        .cast(nw.Int64)
                        .cast(nw.Duration(time_unit="us"))
                        + nw.lit(_EXCEL_EPOCH)
                    ).alias(c)
                )
            # datetime/date already: leave alone
        return df.with_columns(*exprs) if exprs else df

    return step


def numeric(*cols: str):
    """Coerce messy string numbers ('1,234', '$5.50', '-', '') to Float64."""

    def step(df: nw.DataFrame) -> nw.DataFrame:
        exprs = []
        for c in cols:
            if df.schema[c] == nw.String:
                stripped = nw.col(c).str.strip_chars().str.replace_all(
                    r"[$€£,\s]", ""
                )
                exprs.append(
                    nw.when(stripped.is_in(["", "-", "–"]))
                    .then(None)
                    .otherwise(stripped)
                    .cast(nw.Float64)
                    .alias(c)
                )
            elif not df.schema[c].is_numeric():
                continue
        return df.with_columns(*exprs) if exprs else df

    return step
```

and add to `_Clean`: `parse_dates = staticmethod(parse_dates)`, `numeric = staticmethod(numeric)`.

(If `str.replace_all` regex or `Duration` casting misbehaves on the pandas backend, keep polars as the tested contract for serials and document — the serial test is polars-only for this reason.)

**Step 4:** PASS. **Step 5:** Commit: `feat: parse_dates (incl. excel serials) and numeric cleaners`

---

## Task 4: Excel — header detection (pure)

**Files:**
- Create: `src/wh/sources/excel.py`
- Test: `tests/test_excel.py`

**Step 1: Failing tests**

```python
import pytest

from wh.sources.excel import detect_header

MESSY = [
    ["Acme Health — Waitlist Extract", None, None],
    [None, None, None],
    ["Run: 2026-07-19", None, None],
    ["UR", "Referral Date", "Days Waiting"],
    ["A1", "2026-01-01", 12],
    ["A2", "2026-01-05", 8],
]


def test_detect_header_skips_title_rows():
    assert detect_header(MESSY) == 3


def test_detect_header_clean_file():
    rows = [["a", "b"], [1, 2], [3, 4]]
    assert detect_header(rows) == 0


def test_detect_header_empty_sheet():
    from wh.errors import WhError
    with pytest.raises(WhError, match="header"):
        detect_header([[None, None], [None, None]])
```

**Step 2:** Run → collection error. **Step 3: Implement**

```python
"""Excel reading via python-calamine: raw typed rows -> Arrow table.

header="auto" scores each of the first SCAN rows on how header-like it is
(mostly non-empty, mostly strings, all-distinct) and picks the best."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa

from ..errors import WhError

SCAN = 20


def _cell_empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def detect_header(rows: list[list[Any]], scan: int = SCAN) -> int:
    best, best_score = None, 0.0
    for i, row in enumerate(rows[:scan]):
        if i == len(rows) - 1:
            break                       # header needs at least one data row below
        filled = [v for v in row if not _cell_empty(v)]
        if len(filled) < 2:
            continue
        non_empty = len(filled) / max(len(row), 1)
        stringy = sum(isinstance(v, str) for v in filled) / len(filled)
        distinct = len({str(v).strip().lower() for v in filled}) / len(filled)
        score = non_empty * stringy * distinct
        if score > best_score:
            best, best_score = i, score
    if best is None:
        raise WhError(
            "couldn't find a header row in the first rows of the sheet — "
            "pass header=<row index> (0-based) or header=None"
        )
    return best
```

**Step 4:** PASS. **Step 5:** Commit: `feat: excel header-row auto-detection`

---

## Task 5: Excel — full reader to Arrow

**Files:**
- Modify: `src/wh/sources/excel.py`
- Test: `tests/test_excel.py` (add) + fixture builder in `tests/conftest.py`

**Step 1: Fixture builder in conftest.py** (openpyxl writes; calamine reads):

```python
@pytest.fixture
def messy_xlsx(tmp_path):
    """A realistically messy workbook: title rows, merged header cell,
    blank column, numbers-as-text, second sheet."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Acme Health — Waitlist Extract"])
    ws.append([])
    ws.append(["UR", "Referral Date", None, "Days Waiting"])
    ws.append(["A1", "2026-01-01", None, "1,234"])
    ws.append(["A2", "2026-01-05", None, "8"])
    wb.create_sheet("Notes").append(["ignore me"])
    p = tmp_path / "messy.xlsx"
    wb.save(p)
    return p
```

**Step 2: Failing tests**

```python
import pyarrow as pa

from wh.sources.excel import read_excel_arrow


def test_read_excel_auto_header(messy_xlsx):
    t = read_excel_arrow(messy_xlsx)
    assert t.column_names == ["UR", "Referral Date", "col_2", "Days Waiting"]
    assert t.column("UR").to_pylist() == ["A1", "A2"]
    assert t.column("Days Waiting").to_pylist() == ["1,234", "8"]   # mixed -> str


def test_read_excel_sheet_by_name_and_index(messy_xlsx):
    assert read_excel_arrow(messy_xlsx, sheet="Notes").num_columns == 1
    assert read_excel_arrow(messy_xlsx, sheet=1).num_columns == 1


def test_read_excel_unknown_sheet_lists_available(messy_xlsx):
    from wh.errors import WhError
    with pytest.raises(WhError, match="Data, Notes"):
        read_excel_arrow(messy_xlsx, sheet="nope")


def test_read_excel_explicit_header_and_none(messy_xlsx):
    t = read_excel_arrow(messy_xlsx, header=2)
    assert t.column("UR").to_pylist() == ["A1", "A2"]
    t2 = read_excel_arrow(messy_xlsx, header=None, skip_rows=3)
    assert t2.column_names[:2] == ["col_0", "col_1"]
    assert t2.num_rows == 2


def test_read_excel_multirow_header(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Referral", None, "Seen"])     # merged-style: fill right
    ws.append(["Date", "UR", "Date"])
    ws.append(["a", "b", "c"])
    p = tmp_path / "multi.xlsx"
    wb.save(p)
    t = read_excel_arrow(p, header=(0, 1))
    assert t.column_names == ["Referral Date", "Referral UR", "Seen Date"]


def test_read_excel_dedupes_names(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["x", "x", None])
    wb.active.append([1, 2, 3])
    p = tmp_path / "dupe.xlsx"
    wb.save(p)
    assert read_excel_arrow(p).column_names == ["x", "x_2", "col_2"]
```

**Step 3:** Run → FAIL. **Step 4: Implement (append to excel.py)**

```python
def _load_rows(path: Path, sheet) -> list[list[Any]]:
    try:
        from python_calamine import CalamineWorkbook
    except ImportError as e:
        raise WhError(
            "excel support needs python-calamine — install warehouse-tools[excel]"
        ) from e
    if not Path(path).exists():
        raise WhError(f"no such file: {path}")
    wb = CalamineWorkbook.from_path(str(path))
    names = wb.sheet_names
    if sheet is None:
        target = names[0]
    elif isinstance(sheet, int):
        if sheet >= len(names):
            raise WhError(f"sheet index {sheet} out of range ({len(names)} sheets)")
        target = names[sheet]
    else:
        if sheet not in names:
            raise WhError(f"no sheet '{sheet}' (sheets: {', '.join(names)})")
        target = sheet
    return wb.get_sheet_by_name(target).to_python(skip_empty_area=False)


def _fill_right(row: list[Any]) -> list[Any]:
    out, last = [], None
    for v in row:
        if not _cell_empty(v):
            last = v
        out.append(last)
    return out


def _column_names(header_rows: list[list[Any]] | None, ncols: int) -> list[str]:
    if header_rows is None:
        return [f"col_{i}" for i in range(ncols)]
    filled = [_fill_right(r) for r in header_rows]
    names, used = [], set()
    for i in range(ncols):
        parts = []
        for r in filled:
            v = r[i] if i < len(r) else None
            if not _cell_empty(v):
                parts.append(str(v).strip())
        base = " ".join(parts) or f"col_{i}"
        n, k = base, 1
        while n in used:
            k += 1
            n = f"{base}_{k}"
        used.add(n)
        names.append(n)
    return names


def read_excel_arrow(
    path: str | Path,
    *,
    sheet: str | int | None = None,
    header: str | int | tuple | None = "auto",
    skip_rows: int = 0,
) -> pa.Table:
    """Read one sheet into an Arrow table. header: "auto" | int | (int, int)
    | None (row indexes are 0-based, counted after skip_rows)."""
    rows = _load_rows(Path(path), sheet)[skip_rows:]
    if header == "auto":
        idx = detect_header(rows)
        header_rows, data = [rows[idx]], rows[idx + 1 :]
    elif header is None:
        header_rows, data = None, rows
    elif isinstance(header, tuple):
        lo, hi = min(header), max(header)
        header_rows, data = rows[lo : hi + 1], rows[hi + 1 :]
    else:
        header_rows, data = [rows[header]], rows[header + 1 :]

    ncols = max((len(r) for r in (header_rows or []) + data), default=0)
    names = _column_names(header_rows, ncols)

    columns = {}
    for i, name in enumerate(names):
        vals = [r[i] if i < len(r) else None for r in data]
        vals = [None if _cell_empty(v) else v for v in vals]
        try:
            arr = pa.array(vals)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arr = pa.array([None if v is None else str(v) for v in vals])
        columns[name] = arr
    return pa.table(columns)
```

Note: mixed columns (`"1,234"` text + `8` number in the fixture) must fall back to all-string — Excel numbers render via `str()`, which is fine because `clean.numeric` handles the rest. If calamine's `to_python()` signature differs (no `skip_empty_area` kwarg), drop the kwarg — tests are the contract.

**Step 5:** PASS. Commit: `feat: excel reader — sheets, auto/multi-row headers, arrow output`

---

## Task 6: Workspace + module wrappers (`read_excel`, `read_csv`, `clean` export, `land=`)

**Files:**
- Modify: `src/wh/workspace.py` (extract `_split_table`/`_qi` helpers from `land()`, add `read_excel`/`read_csv`), `src/wh/__init__.py`
- Test: `tests/test_read_files.py`

**Step 1: Failing tests**

```python
import duckdb
import polars as pl
import pytest

import wh
from wh.workspace import Workspace


def test_workspace_read_excel_backend(project, messy_xlsx):
    ws = Workspace.load(project / "wh.yaml")
    df = ws.read_excel(messy_xlsx)
    assert isinstance(df, pl.DataFrame)
    assert df["UR"].to_list() == ["A1", "A2"]


def test_workspace_read_excel_lands(project, messy_xlsx):
    ws = Workspace.load(project / "wh.yaml")
    assert ws.read_excel(messy_xlsx, land="files.waitlist") == 2
    assert ws.con.execute('SELECT count(*) FROM "files"."waitlist"').fetchone() == (2,)


def test_workspace_read_csv(project, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a,b\n1,x\n2,y\n")
    ws = Workspace.load(project / "wh.yaml")
    df = ws.read_csv(p)
    assert isinstance(df, pl.DataFrame)
    assert df["a"].to_list() == [1, 2]


def test_workspace_read_csv_lands(project, tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a,b\n1,x\n2,y\n")
    ws = Workspace.load(project / "wh.yaml")
    assert ws.read_csv(p, land="files.d") == 2
    assert ws.con.execute('SELECT sum(a) FROM "files"."d"').fetchone() == (3,)


def test_module_read_excel_works_without_config(messy_xlsx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)               # no wh.yaml anywhere above tmp
    monkeypatch.setattr(wh, "_default", None)
    df = wh.read_excel(messy_xlsx)
    assert df["UR"].to_list() == ["A1", "A2"]


def test_module_read_csv_works_without_config(tmp_path, monkeypatch):
    p = tmp_path / "d.csv"
    p.write_text("a\n1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wh, "_default", None)
    assert wh.read_csv(p)["a"].to_list() == [1]


def test_module_land_without_config_raises(messy_xlsx, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wh, "_default", None)
    with pytest.raises(wh.ConfigError):
        wh.read_excel(messy_xlsx, land="files.x")


def test_clean_exported():
    assert callable(wh.clean) and callable(wh.clean.snake_names)
```

(The `messy_xlsx` fixture moves to `tests/conftest.py` in Task 5 so both test files share it. `monkeypatch.chdir(tmp_path)` relies on tmp dirs having no `wh.yaml` in their parents — true for pytest tmp roots.)

**Step 2:** Run → FAIL. **Step 3: Implement**

workspace.py — extract from `land()` (replacing its inline parsing/quoting, keep the `_mirror` guard inside `_split_table`):

```python
def _split_table(table: str) -> tuple[str, str]:
    parts = table.split(".")
    if len(parts) == 1:
        schema, name = "main", parts[0]
    elif len(parts) == 2:
        schema, name = parts
    else:
        raise WhError(f"table must be 'name' or 'schema.name', got '{table}'")
    if not schema or not name:
        raise WhError(f"table must be 'name' or 'schema.name', got '{table}'")
    if schema == "_mirror":
        raise WhError("the _mirror schema holds mirror metadata — land elsewhere")
    return schema, name


def _qi(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'
```

then `land()` uses them, and add methods:

```python
    def _backend(self, backend: str | None) -> str:
        from .frames import default_backend

        return backend or self.config.frames or default_backend()

    def _land_arrow_or_rel(self, obj, table: str) -> int:
        """CREATE OR REPLACE a workspace table from an Arrow table/relation."""
        schema, name = _split_table(table)
        qualified = f"{_qi(schema)}.{_qi(name)}"
        con = self.con
        con.register("_wh_file_src", obj)
        try:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {_qi(schema)}")
            con.execute(
                f"CREATE OR REPLACE TABLE {qualified} AS SELECT * FROM _wh_file_src"
            )
        finally:
            con.unregister("_wh_file_src")
        (count,) = con.execute(f"SELECT count(*) FROM {qualified}").fetchone()
        return count

    def read_excel(
        self,
        path,
        *,
        sheet=None,
        header="auto",
        skip_rows: int = 0,
        backend: str | None = None,
        land: str | None = None,
    ):
        """Smart Excel reader (see wh.sources.excel). Returns a frame, or the
        row count when land='schema.table' writes it into the workspace db."""
        from .frames import from_arrow
        from .sources.excel import read_excel_arrow

        table = read_excel_arrow(path, sheet=sheet, header=header, skip_rows=skip_rows)
        if land is not None:
            return self._land_arrow_or_rel(table, land)
        return from_arrow(table, self._backend(backend))

    def read_csv(
        self,
        path,
        *,
        backend: str | None = None,
        land: str | None = None,
        **options,
    ):
        """CSV via DuckDB's sniffing reader; **options pass to read_csv."""
        from .frames import from_arrow

        rel = self.con.read_csv(str(path), **options)
        if land is not None:
            return self._land_arrow_or_rel(rel, land)
        return from_arrow(rel.to_arrow_table(), self._backend(backend))
```

`__init__.py` — export `clean` and the readers with the no-config fallback:

```python
from .cleaning import clean

def read_excel(path, **kwargs):
    """Smart Excel reader. Works without a wh.yaml unless land= is given."""
    try:
        ws = workspace()
    except ConfigError:
        if kwargs.get("land") is not None:
            raise
        from .frames import default_backend, from_arrow
        from .sources.excel import read_excel_arrow

        backend = kwargs.pop("backend", None)
        return from_arrow(read_excel_arrow(path, **kwargs), backend or default_backend())
    return ws.read_excel(path, **kwargs)


def read_csv(path, **kwargs):
    """DuckDB-sniffed CSV reader. Works without a wh.yaml unless land= is given."""
    try:
        ws = workspace()
    except ConfigError:
        if kwargs.get("land") is not None:
            raise
        import duckdb

        from .frames import default_backend, from_arrow

        backend = kwargs.pop("backend", None)
        rel = duckdb.read_csv(str(path), **kwargs)
        return from_arrow(rel.to_arrow_table(), backend or default_backend())
    return ws.read_csv(path, **kwargs)
```

Add `"read_excel", "read_csv", "clean"` to `__all__`. (`clean` is imported eagerly from `cleaning` — different name, no shadowing risk; `wh.sources.excel` never collides with the `read_excel` function.)

**Step 4:** Full suite → PASS. **Step 5:** Commit: `feat: read_excel/read_csv on workspace and module level, clean export, land= support`

---

## Task 7: Docs + acceptance

**Step 1:** README — move `read_excel` out of "coming later" (that section is now just Oracle/append-push), add a cleaners example. CLAUDE.md — phase 4 COMPLETE, gotchas (module named `cleaning` on purpose; excel mixed columns fall back to string by design; `read_excel`/`read_csv` work configless unless `land=`).

**Step 2: Acceptance** — build a genuinely messy workbook in the scratchpad with openpyxl (title rows, merged cells, currency strings, date strings, junk rows/columns), then:

```python
import wh
raw = wh.read_excel(".../messy_acceptance.xlsx")
df = wh.clean(raw, wh.clean.snake_names, wh.clean.drop_empty,
              wh.clean.strip_strings, wh.clean.parse_dates("referral_date", format="%d/%m/%Y"),
              wh.clean.numeric("amount"))
print(df)
wh.read_excel(".../messy_acceptance.xlsx", land="files.acceptance")
print(wh.connect().execute("SELECT count(*) FROM files.acceptance").fetchone())
```

Expected: clean typed frame; landed table queryable.

**Step 3:** Full suite. Commit: `feat: phase 4 — excel/csv readers + cleaners (docs + acceptance)`

---

## Done — phase 4 acceptance

- `wh.read_excel(messy)` finds the real header under title rows; sheets by name/index; multi-row + merged headers join sensibly; mixed columns degrade to string not errors
- `wh.clean(df, ...)` chains on polars *and* pandas, returning the caller's frame type
- `wh.read_csv` sniffs; both readers `land=` into the workspace db (replace semantics)
- Module-level readers work with no wh.yaml (frame path only); `land=` without config raises `ConfigError`
- Suite green; nothing needs a server
