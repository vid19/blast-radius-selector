"""Turn raw build-time traces into the compact maps the selector loads at runtime.
Everything here is derived from running sqlglot's own tests at the pinned commit."""
import json, glob, gzip, os, sys, collections, importlib
from coverage import CoverageData

SRC = os.environ["SQLGLOT"].rstrip("/") + "/"
RAW = os.environ["RAW"].rstrip("/") + "/"
tests = json.load(open(sys.argv[1]))["tests"]
ids = [t["id"] for t in tests]
idx = {t: i for i, t in enumerate(ids)}
cls_of = {t: t.rsplit("::", 1)[0] for t in ids}           # file::Class
by_cls = collections.defaultdict(set); by_file = collections.defaultdict(set)
for t in ids:
    by_cls[cls_of[t]].add(idx[t]); by_file[t.split("::")[0]].add(idx[t])

sets, set_ix = [], {}
def S(s):
    k = tuple(sorted(s))
    if k not in set_ix:
        set_ix[k] = len(sets); sets.append(list(k))
    return set_ix[k]

# ---- line coverage
cd = CoverageData(SRC + ".coverage"); cd.read()
lines = {}
for f in cd.measured_files():
    rel = f.replace(SRC, "")
    per = {}
    for ln, ctxs in cd.contexts_by_lineno(f).items():
        s = set()
        for c in ctxs:
            if not c:
                continue
            nid, phase = c.rsplit("|", 1)
            if nid not in idx:
                continue
            if phase == "setup":          # class-level setup affects the whole class
                s |= by_cls[cls_of[nid]]
            else:
                s.add(idx[nid])
        per[ln] = S(s)
    lines[rel] = per

# ---- traces
tr = {"tests": {}, "collect": {}}
for f in glob.glob(RAW + "t2.*.json"):
    d = json.load(open(f)); tr["tests"].update(d["tests"])
    for k, v in d["collect"].items():
        tr["collect"].setdefault(k, set()).update(v)
opens = collections.defaultdict(set)
for nid, rec in tr["tests"].items():
    if nid not in idx: continue
    for phase, paths in rec["opens"].items():
        tgt = by_cls[cls_of[nid]] if phase == "setup" else {idx[nid]}
        for p in paths: opens[p] |= tgt
for tf, paths in tr["collect"].items():
    for p in paths: opens[p] |= by_file.get(tf, set())

# ---- class users, expanded over the MRO
sys.path.insert(0, SRC)
import sqlglot  # noqa
def resolve(name):
    mod, q = name.split(":")
    o = importlib.import_module(mod)
    for part in q.split("."): o = getattr(o, part)
    return o
users = collections.defaultdict(set)
for nid, rec in tr["tests"].items():
    if nid not in idx: continue
    for cname in rec["classes"]:
        try: c = resolve(cname)
        except Exception: continue
        for b in c.__mro__:
            if b.__module__.startswith("sqlglot"):
                users[b.__module__ + ":" + b.__qualname__].add(idx[nid])

# ---- collection-time imports per test file
ci = json.load(open(RAW + "collect_imports.json"))
mod_importers = collections.defaultdict(set)
for tf, mods in ci.items():
    for m in mods: mod_importers[m] |= by_file.get(tf, set())

out = {
    "ids": ids,
    "sets": sets,
    "lines": {f: {str(l): s for l, s in d.items()} for f, d in lines.items()},
    "import_lines": json.load(open(RAW + "import_lines.json")),
    "opens": {p: S(s) for p, s in opens.items()},
    "users": {k: S(v) for k, v in users.items()},
    "mod_importers": {k: S(v) for k, v in mod_importers.items()},
}
raw = json.dumps(out, separators=(",", ":")).encode()
gzip.open(sys.argv[2], "wb", 9).write(raw)
print("sets", len(sets), "raw MB", len(raw) / 1e6)

# ---- derived per-module tables (so the runtime never walks the source tree)
import ast, os
def modname(rel):
    p = rel[:-3]
    if p.endswith("/__init__"): p = p[:-9]
    return p.replace("/", ".")
mod_users = collections.defaultdict(set)
for k, v in users.items():
    mod_users[k.split(":")[0]] |= v
static_importers = collections.defaultdict(set)
for root, _, files in os.walk(SRC + "sqlglot"):
    for fn in files:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(root, fn), SRC)
        try: tree = ast.parse(open(os.path.join(root, fn)).read())
        except SyntaxError: continue
        me = modname(rel)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                static_importers[n.module].add(me)
                for a in n.names: static_importers[n.module + "." + a.name].add(me)
            elif isinstance(n, ast.Import):
                for a in n.names: static_importers[a.name].add(me)
mod_runtime = {}
for rel, d in out["lines"].items():
    s = set()
    for v in set(d.values()): s |= set(sets[v])
    mod_runtime[modname(rel)] = S(s)
out["sets"] = sets
out["mod_users"] = {k: S(v) for k, v in mod_users.items()}
out["mod_runtime"] = mod_runtime
out["static_importers"] = {k: sorted(v) for k, v in static_importers.items()}
raw = json.dumps(out, separators=(",", ":")).encode()
gzip.open(sys.argv[2], "wb", 9).write(raw)
print("final sets", len(sets), "raw MB", len(raw) / 1e6)

# ---- attribute/name references: who reads a name at runtime, and is it re-derived at import
imp_lines = out["import_lines"]
refs = collections.defaultdict(set)
derived = set()
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
for root, _, files in os.walk(SRC + "sqlglot"):
    for fn in files:
        if not fn.endswith(".py"): continue
        rel = os.path.relpath(os.path.join(root, fn), SRC)
        try: tree = ast.parse(open(os.path.join(root, fn)).read())
        except SyntaxError: continue
        il = set(imp_lines.get(rel, ()))
        cov_f = out["lines"].get(rel, {})
        def visit(node, in_func, stmt_targets):
            if isinstance(node, ast.stmt) and not in_func:
                tg = set()
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                        if isinstance(t, ast.Name): tg.add(t.id)
                stmt_targets = tg
            nm = None
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load): nm = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load): nm = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier(): nm = node.value
            if nm is not None:
                ln = node.lineno
                if in_func:
                    if ln in il and not isinstance(in_func, ast.Lambda):
                        derived.add(nm)
                    s = cov_f.get(str(ln))
                    if s is not None: refs[nm] |= set(sets[s])
                elif nm not in stmt_targets:
                    derived.add(nm)
            for ch in ast.iter_child_nodes(node):
                visit(ch, node if isinstance(node, FUNCS) else in_func, stmt_targets)
        visit(tree, None, set())
# only names assigned in class bodies are ever looked up
class_attrs = set()
for root, _, files in os.walk(SRC + "sqlglot"):
    for fn in files:
        if not fn.endswith(".py"): continue
        try: tree = ast.parse(open(os.path.join(root, fn)).read())
        except SyntaxError: continue
        for c in ast.walk(tree):
            if isinstance(c, ast.ClassDef):
                for st in c.body:
                    for t in (st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, (ast.AnnAssign, ast.AugAssign)) else []):
                        if isinstance(t, ast.Name): class_attrs.add(t.id)
out["name_refs"] = {k: S(v) for k, v in refs.items() if k in class_attrs}
derived &= class_attrs
out["sets"] = sets
out["derived"] = sorted(derived)
raw = json.dumps(out, separators=(",", ":")).encode()
gzip.open(sys.argv[2], "wb", 9).write(raw)
print("with name refs: sets", len(sets), "raw MB", len(raw) / 1e6, "derived", len(derived), "names", len(refs))

# ---- measured inline CPU of each test for the probe (build/probe_costs.py)
pc_path = RAW + "probe_costs.json"
if os.path.exists(pc_path):
    pc = json.load(open(pc_path))
    out["probe_cost"] = pc
    raw = json.dumps(out, separators=(",", ":")).encode()
    gzip.open(sys.argv[2], "wb", 9).write(raw)
    print("with probe costs", len(raw) / 1e6)

# ---- which modules' import-time code reaches each function (build/import_consumers.py)
ic_path = RAW + "import_consumers.json"
if os.path.exists(ic_path):
    out["import_consumers"] = json.load(open(ic_path))
    raw = json.dumps(out, separators=(",", ":")).encode()
    gzip.open(sys.argv[2], "wb", 9).write(raw)
    print("with import consumers", len(raw) / 1e6)
