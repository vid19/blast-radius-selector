"""Warm probe process: imports the heavy test dependencies once, then forks a fresh child per
request so every probe still starts from a clean interpreter state (no sqlglot loaded).

Protocol: one JSON request per stdin line {"root", "src", "ids", "all", "timeout"};
one JSON reply per stdout line {"failed": [...]} or {"error": "..."}.
"""
import json
import os
import select
import signal
import sys
import time

for k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[k] = "1"
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import concurrent.futures  # noqa: E402,F401
import multiprocessing  # noqa: E402,F401
import pickle  # noqa: E402,F401
import tempfile  # noqa: E402,F401
import unittest  # noqa: E402,F401
import unittest.mock  # noqa: E402,F401

try:
    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import pandas.testing  # noqa: F401
except Exception:
    pass

from probe_runner_lib import run_tests  # noqa: E402

assert not any(m == "sqlglot" or m.startswith("sqlglot.") for m in sys.modules)
out = sys.stdout
sys.stdout = sys.stderr
for line in sys.stdin:
    req = json.loads(line)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            res = {"failed": run_tests(req["root"], req["src"], req["ids"], req["all"])}
        except BaseException as e:  # noqa
            res = {"error": repr(e)}
        os.write(w, json.dumps(res).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    deadline = time.time() + req.get("timeout", 30)
    chunks = []
    reply = None
    while True:
        left = deadline - time.time()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            reply = {"error": "timeout"}
            break
        ready, _, _ = select.select([r], [], [], left)
        if ready:
            data = os.read(r, 1 << 16)
            if not data:
                break
            chunks.append(data)
    os.close(r)
    os.waitpid(pid, 0)
    if reply is None:
        try:
            reply = json.loads(b"".join(chunks) or b'{"error": "no output"}')
        except ValueError:
            reply = {"error": "bad output"}
    out.write(json.dumps(reply) + "\n")
    out.flush()
