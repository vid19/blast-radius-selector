"""For every sqlglot function that runs while modules are imported, record which modules'
import-time code (module or class bodies) caused it. Output: {"file:firstline": [module, ...]}"""
import importlib, json, os, pkgutil, sys
src, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, src)
root = os.path.join(src, "sqlglot") + os.sep
cons = {}
def modname(path):
    rel = os.path.relpath(path, src)[:-3]
    if rel.endswith("/__init__"): rel = rel[:-9]
    return rel.replace("/", ".")
def prof(frame, event, arg):
    if event != "call": return
    co = frame.f_code
    if not co.co_filename.startswith(root) or co.co_name == "<module>": return
    f = frame.f_back
    while f is not None:
        if f.f_code.co_name == "<module>" and f.f_code.co_filename.startswith(root):
            key = f"{os.path.relpath(co.co_filename, src)}:{co.co_firstlineno}"
            cons.setdefault(key, set()).add(modname(f.f_code.co_filename))
            return
        f = f.f_back
sys.setprofile(prof)
import sqlglot  # noqa
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception: pass
sys.setprofile(None)
json.dump({k: sorted(v) for k, v in cons.items()}, open(out, "w"))
print("functions run at import:", len(cons), file=sys.stderr)
