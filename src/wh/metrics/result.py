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
    ):
        self._model = model
        self._con = con
        self._preferred_backend = preferred_backend
        if not ctx.is_resolved:
            ctx = ctx.resolve(anchor=self._anchor())
        self._compiled = compile_slice(model, measures, by=by, ctx=ctx, grain=grain)
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
    ) -> Slice:
        self._ws._bind_warnings(self._model)     # validate before first query
        return Slice(
            self._model, measures, by=by, ctx=context, grain=grain,
            strict_context=strict_context,
            con=lambda: self._ws.con,
            preferred_backend=self._ws.config.frames,
        )

    def __repr__(self):
        m = ", ".join(self._model.measures)
        return f"<wh model '{self._model.name}' — measures: {m}>"
