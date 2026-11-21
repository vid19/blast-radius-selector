"""Build-time pytest plugin.
- runs ProcessPoolExecutor work inline (with a pickle round trip) so coverage and traces
  are attributed to the test that submitted the work
- records per test: repo files opened (by phase), sqlglot classes instantiated (Dialect parts, Expr)
- records per test file: repo files opened while the file was imported (collection)
"""
import json, os, sys, pickle, concurrent.futures
import pytest

ROOT = os.environ["TRACE_ROOT"]
OUT = os.environ["TRACE_OUT"]
state = {"opens": None, "classes": None, "collect": None}
per_test = {}
per_file_collect = {}

def _audit(event, args):
    if event != "open":
        return
    p = args[0]
    if not isinstance(p, (str, bytes, os.PathLike)):
        return
    p = os.fsdecode(p)
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    if not p.startswith(ROOT) or "__pycache__" in p:
        return
    rel = os.path.relpath(p, ROOT)
    if state["opens"] is not None:
        state["opens"].add(rel)
    elif state["collect"] is not None:
        per_file_collect.setdefault(state["collect"], set()).add(rel)

sys.addaudithook(_audit)


class InlineFuture(concurrent.futures.Future):
    pass


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        f = InlineFuture()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [self.submit(fn, *a).result() for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


def _record_classes(cls, attr="__init__"):
    orig = cls.__init__

    def __init__(self, *a, **k):
        if state["classes"] is not None:
            state["classes"].add(type(self))
        return orig(self, *a, **k)

    cls.__init__ = __init__


def pytest_configure(config):
    from sqlglot import tokens, parser, generator
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    from sqlglot.expressions.core import Expr
    for c in (tokens.Tokenizer, parser.Parser, generator.Generator, Dialect, TypeAnnotator, Expr):
        _record_classes(c)


@pytest.hookimpl(hookwrapper=True)
def pytest_pycollect_makemodule(module_path, parent):
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_collectstart(collector):
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    path = getattr(collector, "path", None)
    if path is not None and str(path).endswith(".py"):
        state["collect"] = os.path.relpath(str(path), ROOT)
    yield
    state["collect"] = None


def pytest_collection_modifyitems(session, config, items):
    seen = set()
    for it in items:
        mod = getattr(it, "module", None)
        if mod is None or id(mod) in seen:
            continue
        seen.add(id(mod))
        if hasattr(mod, "ProcessPoolExecutor"):
            mod.ProcessPoolExecutor = InlineExecutor


def _phase(item, name):
    rec = per_test.setdefault(item.nodeid, {"opens": {}, "classes": set()})
    state["opens"] = set()
    state["classes"] = rec["classes"]
    return rec


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    rec = _phase(item, "setup")
    yield
    rec["opens"]["setup"] = sorted(state["opens"]); state["opens"] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    rec = _phase(item, "call")
    yield
    rec["opens"]["call"] = sorted(state["opens"]); state["opens"] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    rec = _phase(item, "teardown")
    yield
    rec["opens"]["teardown"] = sorted(state["opens"]); state["opens"] = None
    state["classes"] = None


def _cname(c):
    return c.__module__ + ":" + c.__qualname__


def pytest_unconfigure(config):
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    out = {
        "tests": {k: {"opens": v["opens"], "classes": sorted({_cname(c) for c in v["classes"]})} for k, v in per_test.items()},
        "collect": {k: sorted(v) for k, v in per_file_collect.items()},
    }
    with open(f"{OUT}.{wid}.json", "w") as f:
        json.dump(out, f)
