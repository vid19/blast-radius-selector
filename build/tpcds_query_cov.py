"""Build-time: which TPC-DS queries (of test_execute_tpcds) execute each sqlglot line.
Loads the data once (the test's own setUpClass, traced as "setup"), then forks a fresh child per
executed query so no query benefits from caches another one filled, and traces everything that query
does in the test: pickle round trips, mp_execute (optimize, plan, execute), the DuckDB transpile.
Output JSON: {"setup": {file: [lines]}, "queries": {file: {line: [query index, ...]}}}"""
import json, os, pickle, sys
import coverage

src, out = sys.argv[1], sys.argv[2]
sys.path[:0] = [src]
os.environ["SKIP_INTEGRATION"] = "0"
cov = coverage.Coverage(source=[os.path.join(src, "sqlglot")], data_file=None)
cov.start()
import tests.test_executor as T  # noqa
from tests.helpers import TPCDS_SCHEMA  # noqa


class Case(T.TestExecutor):
    pass


Case.setUpClass()
cov.stop()
data = cov.get_data()
root = os.path.abspath(src) + os.sep
setup = {f.replace(root, ""): sorted(data.lines(f) or []) for f in data.measured_files()}
pairs = Case.tpcds_sqls
T.initializer(*pickle.loads(pickle.dumps((TPCDS_SCHEMA, Case.tpcds_tables))))
queries = {}
for i, (args, sql, _) in enumerate(pairs):
    if not args.get("execute"):
        continue
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        c = coverage.Coverage(source=[os.path.join(src, "sqlglot")], data_file=None)
        c.start()
        try:
            table = pickle.loads(pickle.dumps(T.mp_execute(*pickle.loads(pickle.dumps((T.parse_one(sql), args))))))
            Case("test_execute_tpcds").cached_execute(sql, tpch=False)
            ok = table is not None
        except BaseException:
            ok = False
        c.stop()
        d = c.get_data()
        res = {f.replace(root, ""): sorted(d.lines(f) or []) for f in d.measured_files()}
        os.write(w, json.dumps({"ok": ok, "lines": res}).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    chunks = []
    while True:
        b = os.read(r, 1 << 20)
        if not b:
            break
        chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    res = json.loads(b"".join(chunks))
    for f, lines in res["lines"].items():
        q = queries.setdefault(f, {})
        for ln in lines:
            q.setdefault(str(ln), []).append(i)
    print(i, "ok" if res["ok"] else "FAILED", sum(len(v) for v in res["lines"].values()), "lines", file=sys.stderr, flush=True)
json.dump({"setup": setup, "queries": queries}, open(out, "w"))
