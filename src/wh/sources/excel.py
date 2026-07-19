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
