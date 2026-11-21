"""Inline replacements for process pools, shared by the probe and the build."""
import pickle


class _Future:
    def __init__(self):
        self._r = self._e = None

    def result(self, timeout=None):
        if self._e is not None:
            raise self._e
        return self._r

    def exception(self, timeout=None):
        return self._e

    def done(self):
        return True

    def add_done_callback(self, fn):
        fn(self)


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        f = _Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f._r = pickle.loads(pickle.dumps(fn(*a, **k)))
        except BaseException as e:  # noqa
            f._e = e
        return f

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
