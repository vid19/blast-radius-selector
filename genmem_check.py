"""Exact per-dialect copy of tests/test_generator.py::TestGenerator::test_generator_memory, run only
for the dialects whose classes come from the changed modules (or import them).
Usage: genmem_check.py <patched_root> <src_root> <changed module> ...
Exit 0: every affected dialect passes. 1: a dialect fails. 2: can't scope (too many / unknown)."""
import gc
import sys
import tracemalloc

root, src, changed = sys.argv[1], sys.argv[2], set(sys.argv[3:])
sys.path[:0] = [root, src]
try:
    import importlib
    import inspect

    from sqlglot.dialects.dialect import Dialect, Dialects

    affected = []
    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        mods = set()
        for cls in (type(dialect), dialect.generator_class, dialect.parser_class, dialect.tokenizer_class):
            for c in inspect.getmro(cls):
                mods.add(c.__module__)
        dmod = importlib.import_module(type(dialect).__module__)
        for m in list(mods) + [dmod.__name__]:
            mod = sys.modules.get(m)
            if mod is None:
                continue
            for v in vars(mod).values():  # modules a dialect module pulls names from (e.g. typing/x.py)
                vm = getattr(v, "__module__", None) or (v.__name__ if inspect.ismodule(v) else None)
                if vm:
                    mods.add(vm)
        try:
            text = open(dmod.__file__, encoding="utf-8").read()
        except OSError:
            text = ""
        if mods & changed or any(f"from {c} import" in text or f"import {c}" in text for c in changed):
            affected.append((name, dialect))
except BaseException:
    sys.exit(1)
if not affected or len(affected) > 8:
    sys.exit(2)
try:
    for name, dialect in affected:
        for _ in range(100):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if not after - before < 1024:
            sys.exit(1)
except BaseException:
    sys.exit(1)
print(" ".join(str(n) for n, _ in affected))
