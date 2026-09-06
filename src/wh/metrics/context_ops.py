"""Filter context: immutable value object, small op vocabulary, no SQL.

A context holding widgets or relative-time helpers is *unresolved* — not
yet plain data. `resolve(anchor=...)` reads widget `.value`s and anchors
relative time against the mirror's max date (never wall clock), producing
the plain-data snapshot that hashing, serialisation, and provenance are
defined over.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..errors import SemanticsError

_UNITS = ("day", "week", "month", "year")


@dataclass(frozen=True)
class Eq:
    value: object


@dataclass(frozen=True)
class In:
    values: tuple


@dataclass(frozen=True)
class Not:
    value: object


@dataclass(frozen=True)
class Between:  # inclusive both ends (BETWEEN semantics)
    lo: object
    hi: object


@dataclass(frozen=True)
class LastPeriods:
    n: int
    unit: str


@dataclass(frozen=True)
class All:  # explicitly unfiltered
    pass


def not_(value) -> Not:
    return Not(value)


def all_() -> All:
    return All()


def last(n: int, unit: str) -> LastPeriods:
    if unit not in _UNITS:
        raise SemanticsError(f"wh.last unit must be one of {', '.join(_UNITS)}")
    n = int(n)
    if n < 1:
        raise SemanticsError(
            "wh.last needs n >= 1 — zero or negative windows can only be empty"
        )
    return LastPeriods(n, unit)


_OPS = (Eq, In, Not, Between, LastPeriods, All)


def _as_date(v):
    return date.fromisoformat(v) if isinstance(v, str) else v


def _empty_error(key: str) -> SemanticsError:
    return SemanticsError(
        f"'{key}' got a literal empty selection — if this came from an empty "
        f"widget, pass the widget itself (its value is read at slice time) "
        f"or use wh.all() to mean unfiltered"
    )


def _check_scalar(key: str, v) -> None:
    if v is None:
        raise SemanticsError(
            f"'{key}': NULL isn't matchable in a context yet (wh.null is a "
            f"future op) — filter NULLs in the measure or the curated schema"
        )
    if isinstance(v, float) and not math.isfinite(v):
        raise SemanticsError(f"'{key}': non-finite context value {v!r}")
    if not isinstance(v, (str, int, float, bool, date)):
        raise SemanticsError(
            f"'{key}': context values must be scalars or dates — got "
            f"{type(v).__name__} ({v!r})"
        )


def _check_op(key: str, op):
    if isinstance(op, In):
        if not op.values:
            raise _empty_error(key)
        for v in op.values:
            _check_scalar(key, v)
    elif isinstance(op, (Eq, Not)):
        _check_scalar(key, op.value)
    elif isinstance(op, Between):
        _check_scalar(key, op.lo)
        _check_scalar(key, op.hi)
    return op


def _coerce(key: str, value):
    if isinstance(value, _OPS):
        if key == "time":
            # non-range ops would bypass compare's scan widening — the
            # design's named silent-NULL failure class
            if not isinstance(value, (Between, LastPeriods, All)):
                raise SemanticsError(
                    "time= takes a (start, end) range, wh.last(...), wh.all() "
                    "or a widget — not point/membership ops"
                )
            if isinstance(value, Between):
                value = Between(_as_date(value.lo), _as_date(value.hi))
        return _check_op(key, value)
    if hasattr(value, "value"):                      # widget: resolve later
        return value
    if key == "time":
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return _check_op(key, Between(_as_date(value[0]), _as_date(value[1])))
        raise SemanticsError(
            "time= takes a (start, end) range, wh.last(...), or a widget"
        )
    if isinstance(value, (tuple, list)):
        if not value:
            raise _empty_error(key)
        return _check_op(key, In(tuple(value)))
    return _check_op(key, Eq(value))


def _months_back(d: date, months: int) -> date:
    y, m = divmod((d.year * 12 + d.month - 1) - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


class Context:
    """Immutable mapping of `dimension__attribute` (or `time`) to ops."""

    __slots__ = ("_entries",)

    def __init__(self, entries: dict):
        object.__setattr__(self, "_entries", dict(entries))

    def __setattr__(self, name, value):
        raise AttributeError("Context is immutable — use with_/without/|")

    @property
    def entries(self) -> dict:
        return dict(self._entries)

    def with_(self, **kw) -> Context:
        return Context({**self._entries, **_coerce_all(kw)})

    def without(self, *keys) -> Context:
        return Context({k: v for k, v in self._entries.items() if k not in keys})

    def __or__(self, other: Context) -> Context:  # right side wins per attribute
        return Context({**self._entries, **other._entries})

    def __eq__(self, other):
        return isinstance(other, Context) and self._entries == other._entries

    def __repr__(self):
        inner = ", ".join(f"{k}={v!r}" for k, v in sorted(self._entries.items()))
        return f"wh.context({inner})"

    @property
    def is_resolved(self) -> bool:
        return all(isinstance(v, _OPS) and not isinstance(v, LastPeriods)
                   for v in self._entries.values())

    def resolve(self, anchor: date) -> Context:
        """Plain-data snapshot: widgets read now, relative time anchored to
        `anchor` (the fact's max DATE — same context + same mirror = same
        rows). A datetime anchor floors to its date so every unit gets
        whole-day windows; day-inclusive compilation keeps the anchor day."""
        anchor = _as_date(anchor)
        if isinstance(anchor, datetime):   # BEFORE date — datetime is a date subclass
            anchor = anchor.date()
        out = {}
        for k, v in self._entries.items():
            if not isinstance(v, _OPS):              # widget
                raw = v.value
                is_empty_selection = isinstance(raw, (tuple, list)) and not raw
                v = All() if is_empty_selection else _coerce(k, raw)
                if not isinstance(v, _OPS):
                    raise SemanticsError(
                        f"'{k}': widget .value resolved to another widget"
                    )
            if isinstance(v, LastPeriods):
                if v.unit == "day":
                    lo = anchor - timedelta(days=v.n - 1)
                elif v.unit == "week":
                    lo = anchor - timedelta(weeks=v.n) + timedelta(days=1)
                else:
                    months = v.n * (12 if v.unit == "year" else 1)
                    lo = _months_back(anchor, months) + timedelta(days=1)
                v = Between(lo, anchor)
            out[k] = v
        return Context(out)

    def to_dict(self) -> dict:
        """Serialisable form (provenance-ready). Resolved contexts only."""
        if not self.is_resolved:
            raise SemanticsError(
                "context holds widgets or relative time — call resolve() first "
                "(slice() does this for you)"
            )
        def enc(x):
            return x.isoformat() if isinstance(x, date) else x
        out = {}
        for k, v in sorted(self._entries.items()):
            if isinstance(v, Eq):
                out[k] = {"eq": enc(v.value)}
            elif isinstance(v, In):
                out[k] = {"in": [enc(x) for x in v.values]}
            elif isinstance(v, Not):
                out[k] = {"not": enc(v.value)}
            elif isinstance(v, Between):
                out[k] = {"between": [enc(v.lo), enc(v.hi)]}
            elif isinstance(v, All):
                out[k] = {"all": True}
        return out

    def __hash__(self):
        return hash(tuple(sorted((k, repr(v)) for k, v in self.to_dict().items())))


def _coerce_all(kw: dict) -> dict:
    return {k: _coerce(k, v) for k, v in kw.items()}


def _declarative_op(key, value):
    if not isinstance(value, dict):
        return value  # scalar/list shorthand follows the existing context API
    if len(value) != 1:
        raise SemanticsError(f"context.{key}: use exactly one operator")
    op, arg = next(iter(value.items()))
    if op == 'eq':
        return Eq(arg)
    if op == 'in' and isinstance(arg, list):
        return In(tuple(arg))
    if op == 'not':
        return Not(arg)
    if op == 'between' and isinstance(arg, list) and len(arg) == 2:
        return Between(*arg)
    if op == 'all' and arg is True:
        return All()
    if op == 'last' and key == 'time' and isinstance(arg, dict):
        if set(arg) == {'n', 'unit'} and type(arg['n']) is int:
            return last(arg['n'], arg['unit'])
    raise SemanticsError(
        f"context.{key}: invalid {op!r} operator — use eq, in, not, "
        "between, all: true, or time: {last: {n: 1, unit: month}}"
    )


def context(spec=None, /, **kw) -> Context:
    """Build a context from Python values or an analyst-owned YAML/JSON mapping.

    Mapping operators match to_dict(); time additionally accepts
    {last: {n: 3, unit: month}}. Keyword arguments override mapping entries.
    Empty literal selections remain errors, never implicit unfiltered scopes.
    """
    if spec is None:
        spec = {}
    if not isinstance(spec, dict) or any(not isinstance(k, str) for k in spec):
        raise SemanticsError('context: expected a mapping with text keys')
    try:
        decoded = {k: _declarative_op(k, v) for k, v in spec.items()}
        return Context(_coerce_all({**decoded, **kw}))
    except (ValueError, TypeError) as exc:
        if isinstance(exc, SemanticsError):
            raise
        raise SemanticsError(f'context: {exc}') from exc


EMPTY = Context({})
