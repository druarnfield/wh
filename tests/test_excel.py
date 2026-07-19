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
