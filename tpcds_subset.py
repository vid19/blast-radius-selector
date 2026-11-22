"""Exact replay of tests/test_executor.py::TestExecutor::test_execute_tpcds for selected queries only,
using the test module's own setup/initializer/mp_execute/cached_execute and the same pickle round trips
the worker processes do. For commits that change no sqlglot code, an unchanged query gives the result it
gives on the pinned commit (where the test passes), so only changed queries need running.

Usage: tpcds_subset.py <sqlglot_root> <src_root> <patched tpc-ds.sql> <i> [<i> ...]
Exit 0 if every selected query passes, 1 otherwise."""
import os
import pickle
import sys

root, src, fixture = sys.argv[1], sys.argv[2], sys.argv[3]
indices = [int(x) for x in sys.argv[4:]]
sys.path[:0] = [root, src]
try:
    import duckdb
    import numpy as np
    import pandas as pd

    import tests.test_executor as T
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs

    pairs = list(load_sql_fixture_pairs(os.path.relpath(fixture, FIXTURES_DIR)))

    class Case(T.TestExecutor):
        pass

    Case.setUpClass()  # the test's own data loading
    Case.tpcds_sqls = pairs
    case = Case("test_execute_tpcds")
    T.initializer(*pickle.loads(pickle.dumps((TPCDS_SCHEMA, Case.tpcds_tables))))
    for i in indices:
        args, sql, _ = pairs[i]
        table = pickle.loads(pickle.dumps(T.mp_execute(*pickle.loads(pickle.dumps((T.parse_one(sql), args))))))
        if table is None:
            continue
        a = case.cached_execute(sql, tpch=False)
        b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
        T.assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
except BaseException as e:  # noqa
    print(type(e).__name__, str(e)[:200], file=sys.stderr)
    sys.exit(1)
sys.exit(0)
