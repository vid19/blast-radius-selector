"""Measures the CPU each test costs when the probe runs it inline (class setup separately).
Writes {"test": {id: ms}, "class": {file::Class: ms}, "module": {file: ms}}."""
import json, os, sys, time, unittest, importlib, collections
src = sys.argv[1]; out = sys.argv[2]; tests = json.load(open(sys.argv[3]))["tests"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
os.environ["SKIP_INTEGRATION"] = "0"; sys.path.insert(0, src); os.chdir(src)
import probe_runner_lib as pr  # noqa
by_cls = collections.defaultdict(list)
for t in tests:
    if t["seconds"] < 3.0:
        by_cls[t["id"].rsplit("::", 1)[0]].append(t["id"])
res = {"test": {}, "class": {}, "module": {}}
for key, ids in sorted(by_cls.items()):
    path, cls = key.split("::")
    modname = path[:-3].replace("/", ".")
    c0 = time.process_time()
    try:
        mod = importlib.import_module(modname)
    except Exception:
        continue
    res["module"].setdefault(path, round((time.process_time() - c0) * 1000))
    pr.patch_module(mod)
    klass = getattr(mod, cls)
    class R(unittest.TestResult):
        def startTest(self, test):
            self._c = time.process_time(); super().startTest(test)
        def stopTest(self, test):
            res["test"][f"{path}::{cls}::{test._testMethodName}"] = round((time.process_time() - self._c) * 1000); super().stopTest(test)
    suite = unittest.TestSuite(klass(i.split("::")[2]) for i in ids)
    c0 = time.process_time()
    devnull = open(os.devnull, "w"); so, se = sys.stdout, sys.stderr; sys.stdout = sys.stderr = devnull
    try:
        suite.run(R())
    finally:
        sys.stdout, sys.stderr = so, se
    total = (time.process_time() - c0) * 1000
    res["class"][key] = max(0, round(total - sum(res["test"].get(i, 0) for i in ids)))
    print(key, res["class"][key], round(total), file=sys.stderr, flush=True)
json.dump(res, open(out, "w"))
