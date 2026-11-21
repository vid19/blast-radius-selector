"""Inline replacements for process pools, shared by the probe and the build."""
import pickle


class _Future:
    """Computed on first use, so tests that submit everything and then compare one by one stop
    doing work as soon as a comparison fails."""

    def __init__(self, fn, args, kwargs):
        self._call = (fn, args, kwargs)
        self._r = self._e = None

    def _run(self):
        if self._call is not None:
            fn, args, kwargs = self._call
            self._call = None
            try:
                a, k = pickle.loads(pickle.dumps((args, kwargs)))
                self._r = pickle.loads(pickle.dumps(fn(*a, **k)))
            except BaseException as e:  # noqa
                self._e = e

    def result(self, timeout=None):
        self._run()
        if self._e is not None:
            raise self._e
        return self._r

    def exception(self, timeout=None):
        self._run()
        return self._e

    def done(self):
        return True

    def cancelled(self):
        return False

    def add_done_callback(self, fn):
        fn(self)

    def __hash__(self):
        return id(self)


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        return _Future(fn, args, kwargs)

    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def patch_module(mod):
    if hasattr(mod, "ProcessPoolExecutor"):
        mod.ProcessPoolExecutor = InlineExecutor
    if hasattr(mod, "as_completed"):
        mod.as_completed = lambda fs, timeout=None: list(fs)
    if hasattr(mod, "wait"):
        mod.wait = lambda fs, timeout=None, return_when=None: (set(fs), set())


def run_tests(patched, src, ids, run_all):
    """Runs test ids (file::Class::method) against `patched` sqlglot. Returns failing ids.
    Class or module setup failures fail every requested test of that class/module."""
    import json  # noqa: F401
    import os
    import sys
    import unittest

    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path[:0] = [patched, src]
    os.chdir(src)
    suite = unittest.TestSuite()
    id_of = {}
    failed = []
    for tid in ids:
        path, cls, meth = tid.split("::")
        modname = path[:-3].replace("/", ".")
        try:
            mod = __import__(modname, fromlist=["_"])
            patch_module(mod)
            case = getattr(mod, cls)(meth)
        except BaseException:
            failed.append(tid)
            if not run_all:
                return failed
            continue
        id_of[case.id()] = tid
        suite.addTest(case)
    class _Stop(Exception):
        pass

    class Result(unittest.TestResult):
        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None and not run_all:
                raise _Stop()  # one failing subtest already fails the test

    result = Result()
    result.failfast = not run_all
    devnull = open(os.devnull, "w")
    out, err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = devnull
    try:
        suite.run(result)
    finally:
        sys.stdout, sys.stderr = out, err
    for t, _ in result.failures + result.errors:
        base = getattr(t, "test_case", t)
        tid = id_of.get(base.id()) if hasattr(base, "id") else None
        if tid is not None:
            hits = [tid]
        else:
            # _ErrorHolder from setUpClass / setUpModule / tearDown*Class: e.g.
            # "setUpClass (tests.test_x.TestX)" -> fail every test of that class or module
            desc = str(t)
            hits = [v for k, v in id_of.items() if k.rsplit(".", 1)[0] in desc or k.rsplit(".", 2)[0] in desc.split("(")[-1]]
            if not hits:
                hits = list(id_of.values())
        for h in hits:
            if h not in failed:
                failed.append(h)
    return failed
