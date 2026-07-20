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

        table = self._con().sql(self.sql).to_arrow_table()
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
            self._model, self._compiled, self._args, self._con, self._warnings
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
