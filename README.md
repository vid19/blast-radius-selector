# Blast Radius entry

Picks, for each sqlglot patch, only the tests that can reach the changed code, then packs them onto
workers to minimise `busiest worker + 2 s/worker`. Sample set: **118.2 s simulated, 0 misses** (v2: 127.4 s; v1: 165.6 s; starter: 1210.5 s;
oracle: 94.4 s). CPU is mostly the local test runs, about 1 s each.

## Layout

| Path | What |
| --- | --- |
| `selector.py` | The program the image runs. |
| `probe_runner.py`, `probe_runner_lib.py` | Runs sqlglot tests in-process against a patched copy. |
| `probe_server.py` | Warm process (duckdb/pandas imported once) that forks a clean child per probe. |
| `maps/maps.json.gz` | Maps built from sqlglot's own tests at eb11aec: per-line coverage, class users, files each test reads, imports, attribute readers. |
| `src/` | Pinned sqlglot `.py` sources and test fixtures (MIT, see `src/LICENSE-sqlglot`), so each patch's files can be rebuilt and AST-diffed. |
| `build/build_maps.sh` | Rebuilds `maps/` and `src/` from scratch (~6 min, 2 cores). Never run in the Docker build. |
| `build/traceplug2.py` | pytest plugin used by the map build (inline process pools, file/class traces). |
| `build/probe_costs.py` | Measures each test's inline CPU for the probe's budget. |
| `build/mutants.py` | Offline harness: random single-line mutants, runs the selector and the tests, logs misses. Evaluation only. |
| `Makefile`, `score.py`, `sample/` | The organisers' kit. |

## Run

```sh
make sample                                   # docker, exactly like the scorer
BR_DATA=sample BR_SRC=src python3 selector.py > out.jsonl && python3 score.py sample sample/answers.json out.jsonl
```

## How a commit is decided

1. Parse the unified diff; rebuild each changed file from `src/`.
2. `sqlglot/*.py`: parse old and new ASTs. (Functions that also run at import time, like parser code used
   by the DuckDB generator's constant `parse_one` calls, widen only to the modules whose import ran them.)
   * New file fails to parse → every test that imports the module (transitively) is selected.
   * ASTs equal (comments, whitespace, formatting) → nothing.
   * Otherwise a structural diff yields changed statements. Inside a function: tests that executed
     that statement's lines (statement span, so short-circuited lines still count). Inserted
     statements: tests that reached the enclosing block. Change confined to a lambda body: the
     lambda's coverage.
   * Class-body data (`WEEK_OFFSET = …`, dict entries, `arg_types`): tests that instantiated that
     class or a subclass; narrowed to tests that read the attribute at runtime when the attribute is
     never re-derived at import time.
   * Module-level data: users of the module, its classes, and modules that import it.
   * Non-literal module/class-level edits are imported in a scratch copy; an import failure
     selects every importer.
3. `tests/*.py`: the edited test methods; helpers/class/module edits widen to the class/file and to
   files importing it.
4. TPC fixtures (`tpc-ds.sql`, `tpc-h.sql`): the executor class reads them in setUpClass, but only three tests use
   them, and only some fields. The file is parsed exactly as `load_sql_fixture_pairs` does; executor tests are
   kept only if the fields they use changed (or the file no longer parses). Expected-output edits cost 4 s, not 25 s.
5. Any other file: tests that opened it during the traced run (setup-time reads widen to the class,
   collection-time reads to the file). pytest config edits select everything.
5. Heavy-test probe (tests ≥ 3 s, mainly test_execute_tpcds): when dropping them would save ≥ 6 s, run the
   cheapest candidate tests (≈2 s of measured CPU) against the patched code in /tmp, stopping at the first
   failure. Any failure, crash or timeout keeps the heavy tests; a clean pass drops them. Cheap tests are
   always kept. On 37 offline mutants in code TPC-DS covers, every heavy failure (13) came with a probe failure.
   Second stage: when the probe says live, the change is outside execution code, and the optimizer's
   own TPC-DS test is a candidate, run that test here. If it passes, every TPC-DS query optimizes to the
   same SQL, so the executor's TPC-DS test cannot fail and is dropped. On 22 mutants this predicted the
   executor result exactly (the 3 exceptions were changes inside the executor, which are excluded).
   Inline futures are lazy, so a failing check stops at the first mismatch (about 0.7 s).
   test_max_nodes (28.8 s): a scaled-down copy of the test (`maxnodes_small.py`, 100 columns instead of
   10,000) runs in about 0.5 s. If it passes and is not slower than on the unpatched code, the real test is
   dropped. On 25 parser/tokenizer mutants with clean reruns it never passed where the real test failed.
   test_generator_memory (6.7 s): the same per-dialect leak check is run here (`genmem_check.py`), only for
   the dialects built from or importing the changed modules (at most 8; shared modules keep the real test).
   On 150 dialect-file mutants it dropped the test 33 times; the real test passed on all 33.
6. Exact runs: for a Python-only change whose candidates are all cheap (estimated CPU × 1.5 below 60% of
   their schedule cost), run them all here and select only the ones that fail (often nothing). Untrusted
   runs (crash, timeout, unexpected output) keep the full selection. Validated against real pytest on 71
   mutants / 410 test outcomes: identical.
7. Schedule: LPT for k = 1..8, keep the cheapest `max load + 2 s·k`.
