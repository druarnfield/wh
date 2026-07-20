"""Home-grown metrics layer: versioned measures, two-lane compilation.

Design: docs/plans/2026-07-20-metrics-design.md. Submodules are never
named after module-level verbs (`model`, `slice`, `context`, `frame`) —
the package/verb shadowing gotcha from phase 1.
"""

from .loader import load_definitions

__all__ = ["load_definitions"]
