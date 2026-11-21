"""Runs a few sqlglot tests against a patched copy of sqlglot and reports which fail.

Usage: python probe_runner.py <patched_root> <src_root> [--all]  (test ids as JSON on stdin)
Prints a JSON list of failing test ids. Without --all it stops at the first failure.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_runner_lib import run_tests  # noqa: E402

ids = json.loads(sys.stdin.read())
print(json.dumps(run_tests(sys.argv[1], sys.argv[2], ids, "--all" in sys.argv)))
