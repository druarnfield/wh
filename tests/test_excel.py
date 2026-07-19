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


def test_read_excel_auto_header(messy_xlsx):
    from wh.sources.excel import read_excel_arrow

    t = read_excel_arrow(messy_xlsx)
    assert t.column_names == ["UR", "Referral Date", "col_2", "Days Waiting"]
    assert t.column("UR").to_pylist() == ["A1", "A2"]
    assert t.column("Days Waiting").to_pylist() == ["1,234", "8"]   # mixed -> str


def test_read_excel_sheet_by_name_and_index(messy_xlsx):
    from wh.sources.excel import read_excel_arrow

    assert read_excel_arrow(messy_xlsx, sheet="Notes", header=None).num_columns == 1
    assert read_excel_arrow(messy_xlsx, sheet=1, header=None).num_columns == 1


def test_read_excel_unknown_sheet_lists_available(messy_xlsx):
    from wh.errors import WhError
    from wh.sources.excel import read_excel_arrow

    with pytest.raises(WhError, match="Data, Notes"):
        read_excel_arrow(messy_xlsx, sheet="nope")


def test_read_excel_explicit_header_and_none(messy_xlsx):
    from wh.sources.excel import read_excel_arrow

    t = read_excel_arrow(messy_xlsx, header=2)
    assert t.column("UR").to_pylist() == ["A1", "A2"]
    t2 = read_excel_arrow(messy_xlsx, header=None, skip_rows=3)
    assert t2.column_names[:2] == ["col_0", "col_1"]
    assert t2.num_rows == 2


def test_read_excel_multirow_header(tmp_path):
    from openpyxl import Workbook

    from wh.sources.excel import read_excel_arrow

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

    from wh.sources.excel import read_excel_arrow

    wb = Workbook()
    wb.active.append(["x", "x", None])
    wb.active.append([1, 2, 3])
    p = tmp_path / "dupe.xlsx"
    wb.save(p)
    assert read_excel_arrow(p).column_names == ["x", "x_2", "col_2"]


def test_read_excel_missing_file():
    from wh.errors import WhError
    from wh.sources.excel import read_excel_arrow

    with pytest.raises(WhError, match="no such file"):
        read_excel_arrow("nope.xlsx")
