"""Parse-tree SQL comparison: json_serialize_sql, locations stripped."""

import json


def _tree(con, sql: str):
    (raw,) = con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
    tree = json.loads(raw)
    assert not tree.get("error"), f"unparseable SQL: {tree}\n{sql}"

    def strip_locations(node):
        if isinstance(node, dict):
            return {
                k: strip_locations(v)
                for k, v in node.items()
                if "location" not in k
            }
        if isinstance(node, list):
            return [strip_locations(x) for x in node]
        return node

    return strip_locations(tree)


def assert_sql_equiv(con, actual: str, expected: str):
    """Parse-tree equality: formatting is invisible, semantic drift fails."""
    a, e = _tree(con, actual), _tree(con, expected)
    assert a == e, f"SQL trees differ.\nactual SQL:\n{actual}\nexpected:\n{expected}"


def canary_counts(con, sql: str, needle: str):
    """Occurrences of `needle` in the parse tree, bucketed by lane:
    (in any WHERE clause outside __asat, in the __asat subquery, total)."""
    tree = _tree(con, sql)
    counts = {"where": 0, "asat": 0, "total": json.dumps(tree).count(needle)}

    def walk(node, in_asat):
        if isinstance(node, dict):
            if node.get("alias") == "__asat":
                in_asat = True
            for k, v in node.items():
                if k == "where_clause" and v is not None:
                    n = json.dumps(v).count(needle)
                    counts["asat" if in_asat else "where"] += n
                walk(v, in_asat)
        elif isinstance(node, list):
            for x in node:
                walk(x, in_asat)

    walk(tree, False)
    return counts["where"], counts["asat"], counts["total"]
