"""Runs a few sqlglot tests against a patched copy of sqlglot and reports whether any fails.

Usage: python probe_runner.py <patched_root> <src_root> [--all]  (test ids as JSON on stdin)
Prints a JSON list of failing test ids. Process pools run inline so the probe stays in one
process. With --all it runs every test; otherwise it stops at the first failure.
"""
import json
import os
import sys
import unittest

patched, src = sys.argv[1], sys.argv[2]
run_all = "--all" in sys.argv
os.environ["SKIP_INTEGRATION"] = "0"
sys.path[:0] = [patched, src]
os.chdir(src)


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_runner_lib import patch_module  # noqa: E402

ids = json.loads(sys.stdin.read())
suite = unittest.TestSuite()
id_of = {}
failed = []
for tid in ids:
    path, cls, meth = tid.split("::")
    modname = path[:-3].replace("/", ".")
    try:
        mod = __import__(modname, fromlist=["_"])
    except BaseException:
        failed.append(tid)
        if not run_all:
            break
        continue
    patch_module(mod)
    case = getattr(mod, cls)(meth)
    id_of[case.id()] = tid
    suite.addTest(case)

if failed and not run_all:
    print(json.dumps(failed))
    sys.exit(0)

result = unittest.TestResult()
result.failfast = not run_all
devnull = open(os.devnull, "w")
_out, _err = sys.stdout, sys.stderr
sys.stdout = sys.stderr = devnull
try:
    suite.run(result)
finally:
    sys.stdout, sys.stderr = _out, _err
for t, _ in result.failures + result.errors:
    base = getattr(t, "test_case", t)
    tid = id_of.get(base.id())
    if tid is None:
        # setUpClass errors are reported as _ErrorHolder: fail the whole class
        desc = str(t)
        tid = next((v for k, v in id_of.items() if k.rsplit(".", 1)[0] in desc), None) or desc
    if tid not in failed:
        failed.append(tid)
print(json.dumps(failed))
