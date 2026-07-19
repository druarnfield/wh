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
        if not -len(names) <= sheet < len(names):
            raise WhError(f"sheet index {sheet} out of range ({len(names)} sheets)")
        target = names[sheet]
    else:
        if sheet not in names:
            raise WhError(f"no sheet '{sheet}' (sheets: {', '.join(names)})")
        target = sheet
    return wb.get_sheet_by_name(target).to_python(skip_empty_area=False)


def _cell_str(v: Any) -> str:
    # calamine returns Excel numbers as floats; "8" in a cell must not
    # become "8.0" when a mixed column degrades to strings
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


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
    # forward-fill merged cells in the upper rows of a multi-row header;
    # empties in the last (or only) row mean "unnamed column", not a merge
    filled = [_fill_right(r) for r in header_rows[:-1]] + [header_rows[-1]]
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

    def _check_row(idx: int) -> None:
        if not 0 <= idx < len(rows):
            raise WhError(
                f"header row {idx} out of range — sheet has {len(rows)} rows "
                f"(after skip_rows={skip_rows})"
            )

    if header == "auto":
        idx = detect_header(rows)
        header_rows, data = [rows[idx]], rows[idx + 1 :]
    elif header is None:
        header_rows, data = None, rows
    elif isinstance(header, tuple):
        lo, hi = min(header), max(header)
        _check_row(lo)
        _check_row(hi)
        header_rows, data = rows[lo : hi + 1], rows[hi + 1 :]
    else:
        _check_row(header)
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
            arr = pa.array([None if v is None else _cell_str(v) for v in vals])
        columns[name] = arr
    return pa.table(columns)
