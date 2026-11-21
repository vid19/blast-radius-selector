"""Scaled-down copy of tests/test_parser.py::TestParser::test_max_nodes (same statements, 100
columns instead of 10,000 and node limits scaled to match). Exit 1 if any check fails."""
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from sqlglot import exp, parse_one  # noqa: E402
from sqlglot.errors import ParseError  # noqa: E402


def check():
    assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    cols = [f"col{i}" for i in range(500)]
    assert isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select)
    width, depth = 100, 100
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join(expr for _ in range(width))}"
    assert isinstance(parse_one(sql, max_nodes=1_000_000), exp.Select)
    for limit in (10_000, 5_000):
        try:
            parse_one(sql, max_nodes=limit)
        except ParseError as e:
            assert "Maximum number of AST nodes" in str(e) and f"({limit})" in str(e), str(e)
        else:
            raise AssertionError("no ParseError")
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    except ParseError:
        pass
    else:
        raise AssertionError("no ParseError")
    assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)


import time  # noqa: E402

t0 = time.process_time()
try:
    check()
except BaseException:
    sys.exit(1)
print(round(time.process_time() - t0, 4))
