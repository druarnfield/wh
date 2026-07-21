"""The Slice: lazy result of a governed query.

Construction compiles (and enforces strict context); execution happens on
`.frame()` / `.view()`. Unresolved contexts are resolved here, anchored to
the fact's max time value — mirror data, never wall clock.
"""

from __future__ import annotations

from ..errors import SemanticsError
from .compiler import compile_slice, _declared_surface
from .context_ops import EMPTY, Context
from .loader import Model


class Slice:
    def __init__(
        self,
        model: Model,
        measures: list[str],
        by: list[str] = (),
        ctx: Context = EMPTY,
        grain: str | None = None,
        strict_context: bool | None = None,
        con=None,                    # zero-arg callable -> DuckDB connection
        preferred_backend: str | None = None,
        compare: list[str] = (),
        complete_periods: bool = False,
        suppress: int | None = None,
        warnings: list = (),         # bind-check warnings, for provenance
    ):
        self._model = model
        self._con = con
        self._preferred_backend = preferred_backend
        self._warnings = list(warnings)
        self._data_capture = None
        if not ctx.is_resolved:
            ctx = ctx.resolve(anchor=self._anchor())
        self._args = dict(
            measures=measures, by=by, ctx=ctx, grain=grain,
            strict_context=strict_context, compare=compare,
            complete_periods=complete_periods, suppress=suppress,
        )
        self._compiled = compile_slice(
            model, measures, by=by, ctx=ctx, grain=grain, compare=compare,
            complete_periods=complete_periods, suppress=suppress,
        )
        strict = model.strict_context if strict_context is None else strict_context
        if strict and self._compiled.ignored:
            raise SemanticsError(
                f"strict context: {', '.join(self._compiled.ignored)} do(es) not "
                f"apply to model '{model.name}' — declared surface: "
                f"{_declared_surface(model)}"
            )

    def _anchor(self):
        (anchor,) = self._con().execute(
            f"SELECT max({self._model.time_column}) FROM {self._model.fact}"
        ).fetchone()
        if anchor is None:
            raise SemanticsError(
                f"cannot anchor relative time: {self._model.fact} has no "
                f"{self._model.time_column} values"
            )
        return anchor

    @property
    def sql(self) -> str:
        return self._compiled.sql

    @property
    def applied(self) -> tuple:
        return self._compiled.applied

    @property
    def ignored(self) -> tuple:
        return self._compiled.ignored

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

    def suppress(self, n: int = 5) -> "Slice":
        """A new Slice with small-cell suppression: cells under n rows read
        NULL, and ratios with denominators under n too."""
        return Slice(
            self._model, con=self._con,
            preferred_backend=self._preferred_backend,
            warnings=self._warnings,
            **{**self._args, "suppress": n},
        )

    def provenance(self):
        """What was computed, under what definition version, filtered how,
        on data from when."""
        from .provenance import Provenance

        return Provenance(
            self._model, self._compiled, self._args, self._con,
            self._warnings, data=self._data_capture,
        )

    def view(self, name: str) -> None:
        """Register as a DuckDB view (for marimo SQL cells)."""
        qname = '"' + name.replace('"', '""') + '"'
        self._con().execute(f"CREATE OR REPLACE VIEW {qname} AS {self.sql}")

    def __repr__(self):
        return f"<wh slice of '{self._model.name}' — .frame() / .sql / .view(name)>"


class BoundModel:
    """A metric model bound to a workspace's mirror. Bind checks run on
    first use per connection; their warnings are exposed on `.warnings`."""

    def __init__(self, model: Model, ws):
        self._model = model
        self._ws = ws

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def warnings(self) -> list:
        return self._ws._bind_warnings(self._model)

    def slice(
        self,
        measures: list[str],
        by: list[str] = (),
        context: Context = EMPTY,
        grain: str | None = None,
        strict_context: bool | None = None,
        compare: list[str] = (),
        complete_periods: bool = False,
    ) -> Slice:
        warnings = self._ws._bind_warnings(self._model)   # validate before first query
        return Slice(
            self._model, measures, by=by, ctx=context, grain=grain,
            strict_context=strict_context, compare=compare,
            complete_periods=complete_periods,
            con=lambda: self._ws.con,
            preferred_backend=self._ws.config.frames,
            warnings=warnings,
        )

    def values(self, attr: str, context: Context = EMPTY) -> list:
        """Possible values for a declared attribute. Unscoped shared dims
        read the dimension table (cheap); with a context, values re-derive
        from context-scoped fact rows — exclude-your-own-field is your
        composition: values("facility.clinic", context=ctx.without("facility__clinic"))."""
        from .compiler import compile_values

        if not context.is_resolved:
            context = context.resolve(anchor=_bound_anchor(self._ws, self._model))
        sql = compile_values(self._model, attr, context)
        return [r[0] for r in self._ws.con.execute(sql).fetchall()]

    def filter_dim(self, attr: str, context: Context = EMPTY, label: str | None = None):
        """A populated mo.ui.multiselect for a declared attribute. An empty
        selection means unfiltered (pass the widget itself into
        wh.context(...); its value is read at slice time). Cascading is
        composition: call this downstream of another widget's context."""
        mo = _mo()
        options = {str(v): v for v in self.values(attr, context)}
        return mo.ui.multiselect(options=options, label=label or attr)

    def filter_date(self, label: str | None = None):
        """A mo.ui.date_range over the fact's real time bounds; its value is
        a (start, end) tuple — exactly what time= accepts."""
        mo = _mo()
        lo, hi = self._ws.con.execute(
            f"SELECT CAST(min({self._model.time_column}) AS DATE), "
            f"CAST(max({self._model.time_column}) AS DATE) FROM {self._model.fact}"
        ).fetchone()
        if lo is None:
            raise SemanticsError(
                f"{self._model.fact} has no {self._model.time_column} values "
                f"to bound a date range"
            )
        return mo.ui.date_range(
            start=lo, stop=hi, value=(lo, hi),
            label=label or self._model.time_column,
        )

    def __repr__(self):
        m = ", ".join(self._model.measures)
        return f"<wh model '{self._model.name}' — measures: {m}>"


def _bound_anchor(ws, model):
    (anchor,) = ws.con.execute(
        f"SELECT max({model.time_column}) FROM {model.fact}"
    ).fetchone()
    if anchor is None:
        raise SemanticsError(
            f"cannot anchor relative time: {model.fact} has no "
            f"{model.time_column} values"
        )
    return anchor


def _mo():
    try:
        import marimo as mo
    except ImportError as e:
        raise SemanticsError(
            "marimo isn't installed — `uv add marimo` (widget helpers are "
            "notebook-only; values() works without it)"
        ) from e
    return mo
