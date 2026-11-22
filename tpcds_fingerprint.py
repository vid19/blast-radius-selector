"""Everything tests/test_executor.py::TestExecutor::test_execute_tpcds derives from sqlglot for the queries
it executes, short of running them: the DuckDB SQL it compares against, the optimized query, and the full
logical plan (every step field, sub-plans for subqueries). Deterministic text, one line per query.

Usage: tpcds_fingerprint.py <sqlglot_root> <src_root> [baseline_file]
With a baseline file: exit 0 if identical, 1 at the first difference. Without: print the fingerprint."""
import hashlib
import math
import pickle
import sys

root, src = sys.argv[1], sys.argv[2]
baseline = sys.argv[3] if len(sys.argv) > 3 else None
sys.path[:0] = [root, src]

from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs  # noqa: E402
from sqlglot import exp, parse_one, planner, transpile  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402


def edump(e):
    """Full structure of an expression, including inferred types, which .sql() does not show."""
    if isinstance(e, exp.Expr):
        parts = [type(e).__name__]
        t = getattr(e, "_type", None)
        if t is not None:
            parts.append("T=" + t.sql())
        for k in sorted(e.args):
            parts.append(k + "=" + edump(e.args[k]))
        return "(" + " ".join(parts) + ")"
    if isinstance(e, list):
        return "[" + ",".join(edump(x) for x in e) + "]"
    return repr(e)


def ser(v, seen):
    if isinstance(v, exp.Expr):
        return "E:" + edump(v) + ":" + sub_plans(v, seen)
    if isinstance(v, planner.Step):
        return "S:" + v.name
    if isinstance(v, dict):
        return "{" + ",".join(sorted(ser(k, seen) + "=" + ser(x, seen) for k, x in v.items())) + "}"
    if isinstance(v, (set, frozenset)):
        return "<" + ",".join(sorted(ser(x, seen) for x in v)) + ">"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(ser(x, seen) for x in v) + "]"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return repr(v)


def step_fp(step, seen):
    fields = {k: v for k, v in vars(step).items() if k not in ("dependencies", "dependents")}
    deps = sorted(d.name for d in step.dependencies)
    dependents = sorted(d.name for d in step.dependents)
    return type(step).__name__ + ser(fields, seen) + "deps" + repr(deps) + "dependents" + repr(dependents)


def plan_fp(plan, seen):
    # what the executor reads from a Plan: leaves, dag, ctes, and every reachable step
    head = [
        "leaves:" + repr(sorted(s.name for s in plan.leaves)),
        "dag:" + repr(sorted((k.name, sorted(v.name for v in vs)) for k, vs in plan.dag.items())),
        "ctes:" + (plan.ctes.sql() if plan.ctes is not None else "-"),
    ]
    out, stack, done = head, [plan.root], set()
    while stack:
        s = stack.pop()
        if id(s) in done:
            continue
        done.add(id(s))
        out.append(step_fp(s, seen))
        stack.extend(sorted(s.dependencies, key=lambda d: d.name))
    return "|".join(sorted(out))


def sub_plans(e, seen):
    # the executor plans subqueries at run time with the same planner
    parts = []
    for q in e.find_all(exp.Select):
        if q is e:
            continue
        key = q.sql()
        if key in seen:
            continue
        seen.add(key)
        try:
            parts.append(plan_fp(planner.Plan(q.copy()), seen))
        except Exception as ex:  # noqa
            parts.append("ERR:" + type(ex).__name__)
    return ";".join(parts)


def fingerprints():
    schema = TPCDS_SCHEMA
    for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if not meta.get("execute"):
            continue
        try:
            duck = transpile(sql, write="duckdb")[0]
        except Exception as ex:  # noqa
            duck = "ERR:" + repr(ex)
        try:
            # the test ships the parsed query to a worker process: same pickle round trip
            expression = pickle.loads(pickle.dumps(parse_one(sql)))
            optimized = optimize(expression, ensure_schema(schema), leave_tables_isolated=True)
            # results come back pickled too; the plan's expressions must survive the same trip
            optimized = pickle.loads(pickle.dumps(optimized))
            text = edump(optimized) + "#" + plan_fp(planner.Plan(optimized), set())
        except Exception as ex:  # noqa
            text = "ERR:" + repr(ex)
        yield hashlib.sha1((duck + "\n" + text).encode()).hexdigest()


if baseline is None:
    for f in fingerprints():
        print(f)
    sys.exit(0)
expected = open(baseline).read().split()
n = 0
for f in fingerprints():
    if n >= len(expected) or f != expected[n]:
        sys.exit(1)
    n += 1
sys.exit(0 if n == len(expected) else 1)
