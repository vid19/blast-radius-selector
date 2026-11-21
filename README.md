# Blast Radius entry

Picks, for each sqlglot patch, only the tests that can reach the changed code, then packs them onto
workers to minimise `busiest worker + 2 s/worker`. Sample set: **165.6 s simulated, 0 misses**
(starter: 1210.5 s on the same 30 commits; oracle: 94.4 s). Selector CPU ≈ 0.4 s for the batch.

## Layout

| Path | What |
| --- | --- |
| `selector.py` | The program the image runs (stdlib only). |
| `maps/maps.json.gz` | Maps built from sqlglot's own tests at eb11aec: per-line coverage, class users, files each test reads, imports, attribute readers. |
| `src/` | Pinned sqlglot `.py` sources (MIT, see `src/LICENSE-sqlglot`), so each patch's files can be rebuilt and AST-diffed. |
| `build/build_maps.sh` | Rebuilds `maps/` and `src/` from scratch (~6 min, 2 cores). Never run in the Docker build. |
| `build/traceplug2.py` | pytest plugin used by the map build (inline process pools, file/class traces). |
| `build/mutants.py` | Offline harness: random single-line mutants, runs the selector and the tests, logs misses. Evaluation only. |
| `Makefile`, `score.py`, `sample/` | The organisers' kit. |

## Run

```sh
make sample                                   # docker, exactly like the scorer
BR_DATA=sample BR_SRC=src python3 selector.py > out.jsonl && python3 score.py sample sample/answers.json out.jsonl
```

## How a commit is decided

1. Parse the unified diff; rebuild each changed file from `src/`.
2. `sqlglot/*.py`: parse old and new ASTs.
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
4. Any other file: tests that opened it during the traced run (setup-time reads widen to the class,
   collection-time reads to the file). pytest config edits select everything.
5. Schedule: LPT for k = 1..8, keep the cheapest `max load + 2 s·k`.
