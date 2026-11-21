"""Blast Radius selector.

For each patch: rebuild the changed files from the pinned sqlglot checkout, diff their ASTs to
find the statements that really changed, and map those to the tests that can reach them using
maps built from the pinned commit (line coverage, class usage, file reads, imports). Then pack
the selected tests onto workers as cheaply as the cost model allows.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
import sys
from difflib import SequenceMatcher

DATA = os.environ.get("BR_DATA", "/data")
SRC = os.path.abspath(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAPS = os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", "maps.json.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- patch parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """Returns a list of {old, new, hunks:[(old_start, lines)]} with paths relative to the repo."""
    files, cur = [], None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "rename": False}
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            files.append(cur)
        elif line.startswith("--- ") and cur is not None and not cur["hunks"]:
            p = line[4:].split("\t")[0].strip()
            cur["old"] = None if p == "/dev/null" else re.sub(r"^a/", "", p)
        elif line.startswith("+++ ") and cur is not None and not cur["hunks"]:
            p = line[4:].split("\t")[0].strip()
            cur["new"] = None if p == "/dev/null" else re.sub(r"^b/", "", p)
        elif line.startswith("new file mode") and cur is not None:
            cur["old_missing"] = True
        elif line.startswith("deleted file mode") and cur is not None:
            cur["new_missing"] = True
        elif line.startswith(("rename from", "rename to", "copy from", "copy to")) and cur is not None:
            cur["rename"] = True
            if line.startswith("rename from"):
                cur["old"] = line[len("rename from "):].strip()
            elif line.startswith("rename to"):
                cur["new"] = line[len("rename to "):].strip()
        elif line.startswith("Binary files") and cur is not None:
            cur["binary"] = True
        else:
            m = HUNK.match(line)
            if m and cur is not None:
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                body = []
                i += 1
                o = n = 0
                while i < len(lines) and (o < old_len or n < new_len):
                    b = lines[i]
                    if b.startswith("\\"):
                        i += 1
                        continue
                    tag = b[:1] if b else " "
                    body.append((tag, b[1:]))
                    if tag in (" ", "-"):
                        o += 1
                    if tag in (" ", "+"):
                        n += 1
                    i += 1
                while i < len(lines) and lines[i].startswith("\\"):
                    i += 1
                cur["hunks"].append((old_start, body))
                continue
        i += 1
    for f in files:
        if f.get("old_missing"):
            f["old"] = None
        if f.get("new_missing"):
            f["new"] = None
    return files


def read_src(path):
    try:
        with open(os.path.join(SRC, path), "rb") as fh:
            return fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def apply_hunks(old_text, hunks):
    """Applies hunks to old_text. Returns (new_text, changed_old_lines, insert_points)."""
    old = old_text.split("\n") if old_text is not None else []
    out, pos = [], 0  # pos: 0-based index into old
    removed, inserted = set(), set()
    for old_start, body in hunks:
        start = max(old_start - 1, 0) if any(t in (" ", "-") for t, _ in body) else old_start
        out.extend(old[pos:start])
        pos = start
        for tag, text in body:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else text)
                pos += 1
            elif tag == "-":
                removed.add(pos + 1)
                pos += 1
            elif tag == "+":
                inserted.add(pos)  # inserted after old line `pos` (1-based), 0 = top
                out.append(text)
    out.extend(old[pos:])
    return "\n".join(out), removed, inserted


# --------------------------------------------------------------------------- maps

class _LazySets:
    def __init__(self, maps):
        self.m = maps
        self.cache = {}

    def __getitem__(self, i):
        v = self.cache.get(i)
        if v is None:
            raw = self.m._raw_sets[i]
            if self.m._identity:
                v = frozenset(raw)
            else:
                r = self.m._remap
                v = frozenset(r[j] for j in raw if r[j] is not None)
            self.cache[i] = v
        return v


class Maps:
    def __init__(self, tests):
        with gzip.open(MAPS, "rb") as fh:
            m = json.loads(fh.read())
        self.raw = m
        num = {t["id"]: t["number"] for t in tests}
        self.known = set(num.values())
        remap = [num.get(i) for i in m["ids"]]
        self._raw_sets = m["sets"]
        self._identity = remap == list(range(len(remap)))
        self._remap = remap
        self.sets = _LazySets(self)
        self.lines = m["lines"]
        self.import_lines = {k: set(v) for k, v in m["import_lines"].items()}
        self.opens = m["opens"]
        self.users = m["users"]
        self.mod_importers = m["mod_importers"]
        self.mod_users = m["mod_users"]
        self.mod_runtime = m["mod_runtime"]
        self.static_importers = m["static_importers"]
        self.import_consumers = m.get("import_consumers", {})
        self.probe_cost = m.get("probe_cost", {"test": {}, "class": {}, "module": {}})
        self.name_refs = m.get("name_refs", {})
        self.derived = set(m.get("derived", ()))
        self.mapped = set(x for x in remap if x is not None)
        self.by_file = {}
        self.by_class = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
            self.by_class.setdefault(t["id"].rsplit("::", 1)[0], set()).add(t["number"])
        self.by_id = num
        self.id_of = {v: k for k, v in num.items()}
        self.everything = set(num.values())

    def cov(self, path, lines):
        d = self.lines.get(path)
        out = set()
        if not d:
            return out
        for ln in lines:
            s = d.get(str(ln))
            if s is not None:
                out |= self.sets[s]
        return out

    def users_of(self, qualname):
        s = self.users.get(qualname)
        return set(self.sets[s]) if s is not None else set()

    def users_prefix(self, module):
        s = self.mod_users.get(module)
        return set(self.sets[s]) if s is not None else set()

    def mod_runtime_by_mod(self, mod):
        s = self.mod_runtime.get(mod)
        return set(self.sets[s]) if s is not None else set()

    def opened(self, path):
        s = self.opens.get(path)
        return set(self.sets[s]) if s is not None else set()

    def importers(self, module):
        s = self.mod_importers.get(module)
        return set(self.sets[s]) if s is not None else set()

    def module_runtime_users(self, path):
        """Tests that ran any line of the module (import-time lines included)."""
        s = self.mod_runtime.get(module_name(path))
        return set(self.sets[s]) if s is not None else set()


# --------------------------------------------------------------------------- AST diffing

STMT_LISTS = ("body", "orelse", "finalbody", "handlers", "cases")


def _fp(node):
    """Structural fingerprint of a node (line numbers and comments ignored), cached on the node."""
    v = node.__dict__.get("_fp")
    if v is not None:
        return v
    parts = [type(node).__name__]
    for f in node._fields:
        x = getattr(node, f, None)
        if isinstance(x, ast.AST):
            parts.append(_fp(x))
        elif isinstance(x, list):
            parts.append(tuple(_fp(y) if isinstance(y, ast.AST) else (type(y).__name__, y) for y in x))
        else:
            parts.append((type(x).__name__, x))
    v = hash(tuple(parts))
    node.__dict__["_fp"] = v
    return v


def dump(node):
    return _fp(node)


def header_equal(a, b):
    """Compare two compound statements ignoring their statement lists."""
    if type(a) is not type(b):
        return False
    for f in a._fields:
        if f in STMT_LISTS:
            continue
        va, vb = getattr(a, f, None), getattr(b, f, None)
        if isinstance(va, list) and isinstance(vb, list):
            if len(va) != len(vb) or any(
                (dump(x) if isinstance(x, ast.AST) else x) != (dump(y) if isinstance(y, ast.AST) else y)
                for x, y in zip(va, vb)
            ):
                return False
        elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
            if not (isinstance(va, ast.AST) and isinstance(vb, ast.AST) and dump(va) == dump(vb)):
                return False
        elif va != vb:
            return False
    return True


def stmt_lists(node):
    out = []
    for f in STMT_LISTS:
        v = getattr(node, f, None)
        if isinstance(v, list):
            out.append((f, v))
    return out


class Site:
    """A changed place in the OLD tree.
    kind: 'stmt' (simple statement changed/deleted), 'header' (compound statement's own fields),
          'insert' (statements added into a block of `node`), 'expr' (a sub-expression inside `node`)."""

    def __init__(self, kind, node, parent_chain, new_node=None, sub=None):
        self.kind, self.node, self.chain, self.new_node, self.sub = kind, node, parent_chain, new_node, sub


def diff_blocks(old_list, new_list, chain, owner, sites):
    a = [dump(x) for x in old_list]
    b = [dump(x) for x in new_list]
    sm = SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for o, n in zip(old_list[i1:i2], new_list[j1:j2]):
                diff_nodes(o, n, chain, sites)
            continue
        if tag in ("replace", "delete"):
            for o in old_list[i1:i2]:
                sites.append(Site("stmt", o, chain))
        if tag in ("replace", "insert"):
            sites.append(Site("insert", owner, chain, sub=(old_list[i1 - 1] if i1 > 0 else None,
                                                         old_list[i1] if i1 < len(old_list) else None)))


def diff_nodes(o, n, chain, sites):
    if dump(o) == dump(n):
        return
    if type(o) is type(n) and stmt_lists(o):
        if not header_equal(o, n):
            sites.append(Site("header", o, chain, new_node=n))
        for (f, ol), (_, nl) in zip(stmt_lists(o), stmt_lists(n)):
            if f == "handlers":
                if len(ol) == len(nl):
                    for x, y in zip(ol, nl):
                        diff_nodes(x, y, chain + [o], sites)
                else:
                    sites.append(Site("header", o, chain, new_node=n))
                continue
            diff_blocks(ol, nl, chain + [o], o, sites)
        return
    sites.append(Site("stmt", o, chain, new_node=n))


def diff_module(old_tree, new_tree):
    sites = []
    diff_blocks(old_tree.body, new_tree.body, [old_tree], old_tree, sites)
    return sites


def expr_diffs(o, n, out, depth=0, pairs=None):
    """Minimal differing sub-expressions (old nodes) between two nodes of equal type."""
    if dump(o) == dump(n):
        return
    if type(o) is not type(n) or depth > 60:
        out.append(o)
        if pairs is not None:
            pairs.append((o, n))
        return
    for f in o._fields:
        va, vb = getattr(o, f, None), getattr(n, f, None)
        if isinstance(va, ast.AST) and isinstance(vb, ast.AST):
            expr_diffs(va, vb, out, depth + 1, pairs)
        elif isinstance(va, list) and isinstance(vb, list):
            if len(va) != len(vb):
                out.append(o)
                if pairs is not None:
                    pairs.append((o, n))
                return
            for x, y in zip(va, vb):
                if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                    expr_diffs(x, y, out, depth + 1, pairs)
                elif x != y:
                    out.append(o)
                    if pairs is not None:
                        pairs.append((o, n))
                    return
        elif va != vb:
            out.append(o)
            if pairs is not None:
                pairs.append((o, n))
            return


def _literal(n):
    if isinstance(n, ast.Constant):
        return True
    if isinstance(n, ast.UnaryOp) and isinstance(n.operand, ast.Constant):
        return True
    return isinstance(n, (ast.cmpop, ast.boolop, ast.operator, ast.unaryop))


def literal_only_change(o, n):
    """True when two statements differ only in literal values / operators (cannot fail at import)."""
    if n is None or type(o) is not type(n):
        return False
    pairs = []
    expr_diffs(o, n, [], 0, pairs)
    return bool(pairs) and all(_literal(a) and _literal(b) for a, b in pairs)


def span(node):
    return range(node.lineno, (getattr(node, "end_lineno", None) or node.lineno) + 1)


def header_span(node):
    """Lines of a compound statement before its first nested statement."""
    first = None
    for _, lst in stmt_lists(node):
        for s in lst:
            ln = getattr(s, "lineno", None)
            if ln is not None and (first is None or ln < first):
                first = ln
    start = node.lineno
    if hasattr(node, "decorator_list") and node.decorator_list:
        start = min([start] + [d.lineno for d in node.decorator_list])
    end = (first - 1) if first else (node.end_lineno or node.lineno)
    return range(start, max(end, node.lineno) + 1)


def simple_lines(node):
    """Lines of a statement, excluding nested statement bodies."""
    if stmt_lists(node):
        return header_span(node)
    return span(node)


def defined_names(node):
    names = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    for t in ast.walk(node) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else []:
        if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
            names.add(t.id)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
    return names


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


# --------------------------------------------------------------------------- per-file analysis

def module_name(path):
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


class Analyzer:
    def __init__(self, maps, tests):
        self.m = maps
        self.tests = tests
        self.trees = {}
        self.pending = {}
        self.changed_py = {}
        self.other_change = False
        self.ref_index = None

    def old_tree(self, path):
        if path not in self.trees:
            text = read_src(path)
            try:
                self.trees[path] = ast.parse(text) if text is not None else None
            except SyntaxError:
                self.trees[path] = None
        return self.trees[path]

    def classes_in(self, path, tree):
        mod = module_name(path)
        names = []

        def walk(body, prefix):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    q = prefix + n.name
                    names.append(mod + ":" + q)
                    walk(n.body, q + ".")

        walk(tree.body, "")
        return names

    def import_failure(self, path):
        """Every test that imports the module (or a module importing it), at collection or later."""
        m = self.m
        start = module_name(path)
        seen, todo = {start}, [start]
        while todo:
            cur = todo.pop()
            for nxt in m.static_importers.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
            # importing a parent package imports the module too
        out = set()
        for mod in seen:
            out |= m.importers(mod) | m.users_prefix(mod) | m.mod_runtime_by_mod(mod)
        return out

    def import_check(self, changed):
        """Import the patched modules in a fresh interpreter. Returns the paths that fail."""
        import shutil
        import subprocess
        root = "/tmp/br_import"
        if not os.path.isdir(os.path.join(root, "sqlglot")):
            shutil.rmtree(root, ignore_errors=True)
            shutil.copytree(os.path.join(SRC, "sqlglot"), os.path.join(root, "sqlglot"))
        failed = []
        try:
            for path, text in changed.items():
                with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
                    fh.write(text)
            mods = [module_name(p) for p in changed]
            code = (
                "import importlib, sys\n"
                "bad = []\n"
                "for m in sys.argv[1:]:\n"
                "    try:\n"
                "        importlib.import_module(m)\n"
                "    except BaseException as e:\n"
                "        bad.append(m)\n"
                "print(' '.join(bad))\n"
            )
            env = {"PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "PATH": os.environ.get("PATH", "")}
            r = subprocess.run([sys.executable, "-c", code] + mods, capture_output=True, text=True, timeout=30, env=env, cwd=root)
            bad = set(r.stdout.split()) if r.returncode == 0 else set(mods)
            failed = [p for p in changed if module_name(p) in bad]
        except Exception as e:  # noqa
            log("import check failed", e)
            failed = list(changed)
        finally:
            for path in changed:
                src = os.path.join(SRC, path)
                dst = os.path.join(root, path)
                if os.path.exists(src):
                    shutil.copyfile(src, dst)
                elif os.path.exists(dst):
                    os.remove(dst)
        return failed

    def analyze_sqlglot_py(self, path, old_text, new_text, notes):
        old = self.old_tree(path)
        if old is None:
            notes.append("old-unparsable")
            return set(self.m.everything)
        try:
            new = ast.parse(new_text)
        except SyntaxError:
            notes.append("syntax-error")
            return self.import_failure(path)
        if dump(old) == dump(new):
            notes.append("ast-equal")
            return set()
        sites = diff_module(old, new)
        cands = set()
        risky = False
        for s in sites:
            cands |= self.site_candidates(path, old, s, notes)
            in_func = any(isinstance(c, FUNC) for c in s.chain)
            if not in_func and not (s.kind in ("stmt", "header") and literal_only_change(s.node, s.new_node)):
                if not (s.kind in ("stmt", "header") and s.new_node is not None and "lambda" in notes[-1:]):
                    risky = True
        if risky:
            notes.append("import-risky")
            self.pending[path] = new_text
        return cands

    def site_candidates(self, path, tree, site, notes):
        m = self.m
        chain = site.chain
        node = site.node
        # innermost function in the chain (or node itself for header changes of a def)
        func = None
        for c in reversed(chain):
            if isinstance(c, FUNC):
                func = c
                break
        imp = m.import_lines.get(path, set())
        if func is not None:
            # runtime change inside a function body
            if site.kind == "insert":
                owner = site.node
                lines = set(header_span(owner)) if owner is not func else set(span(func)) - set(header_span(func))
                for nb in site.sub:
                    if nb is not None:
                        lines |= set(simple_lines(nb))
                if owner is func:
                    lines |= set(span(func)) - set(header_span(func))
            elif site.kind == "header":
                lines = set(header_span(node))
            else:
                lines = set(simple_lines(node))
            out = m.cov(path, lines)
            if any(ln in imp for ln in lines):
                # the function runs while some modules are imported: what they build there is used
                # later by whoever uses those modules
                first = min([func.lineno] + [d.lineno for d in getattr(func, "decorator_list", [])])
                consumers = m.import_consumers.get(f"{path}:{first}") if not isinstance(func, ast.Lambda) else None
                own = module_name(path)
                if consumers and own not in consumers and os.environ.get("BR_IMPORT_CONSUMERS", "1") == "1":
                    notes.append(f"import-consumers:{','.join(c.rsplit('.', 1)[-1] for c in consumers)}")
                    for cm in consumers:
                        out |= m.users_prefix(cm) | m.mod_runtime_by_mod(cm)
                        for imp_mod in m.static_importers.get(cm, ()):
                            out |= m.users_prefix(imp_mod)
                else:
                    notes.append("import-time-func")
                    out |= self.module_level(path, tree, node, chain, notes)
            return out
        # not inside a function body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and site.kind in ("header", "stmt"):
            body_lines = set(span(node)) - set(header_span(node))
            out = m.cov(path, body_lines)
            if site.kind == "stmt" or not self._only_defaults_changed(node, site.new_node):
                out |= self.module_level(path, tree, node, chain, notes)
            return out
        if site.kind in ("stmt", "header") and site.new_node is not None and not stmt_lists(node):
            # class/module level expression change: if confined to lambda bodies, use their coverage
            diffs = []
            expr_diffs(node, site.new_node, diffs)
            lam = self._lambda_only(node, diffs)
            if lam is not None:
                notes.append("lambda")
                return m.cov(path, lam)
        return self.module_level(path, tree, node, chain, notes)

    def _only_defaults_changed(self, o, n):
        if n is None or type(o) is not type(n):
            return False
        return o.name == n.name and [dump(d) for d in o.decorator_list] == [dump(d) for d in n.decorator_list]

    def _lambda_only(self, stmt, diffs):
        if not diffs:
            return None
        parents = {}
        for p in ast.walk(stmt):
            for ch in ast.iter_child_nodes(p):
                parents[ch] = p
        lines = set()
        for d in diffs:
            q, lam = d, None
            while q in parents:
                q = parents[q]
                if isinstance(q, ast.Lambda):
                    lam = q
                    break
            if lam is None or lam is d:
                return None
            lines |= set(span(lam.body))
        return lines

    def module_level(self, path, tree, node, chain, notes):
        """Class-body or module-level change: data used by whoever uses the class/module."""
        m = self.m
        out = set()
        cls_chain = [c for c in chain if isinstance(c, ast.ClassDef)]
        if isinstance(node, ast.ClassDef):
            cls_chain = cls_chain + [node]
        mod = module_name(path)
        if cls_chain:
            q = mod + ":" + ".".join(c.name for c in cls_chain)
            u = m.users_of(q)
            notes.append(f"class-users:{len(u)}")
            targets = set()
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and cls_chain and chain and chain[-1] is cls_chain[-1]:
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(t, ast.Name):
                        targets.add(t.id)
                    else:
                        targets = set()
                        break
            if u and targets and all(t not in m.derived and t in m.name_refs for t in targets):
                readers = set()
                for t in targets:
                    readers |= m.sets[m.name_refs[t]]
                notes.append(f"attr-readers:{len(u & readers)}")
                u = u & readers
            out |= u
            if not u:
                # never instantiated (mixin, constant holder): anyone running code in the module
                out |= m.users_prefix(mod) | m.module_runtime_users(path)
        else:
            notes.append("module-level")
            out |= m.users_prefix(mod) | m.module_runtime_users(path)
            for imp in m.static_importers.get(mod, ()):
                out |= m.users_prefix(imp) | m.mod_runtime_by_mod(imp)
        return out

    def analyze_test_py(self, path, old_text, new_text, notes):
        m = self.m
        in_file = set(m.by_file.get(path, set()))
        dependents = set()
        modname = module_name(path)
        dependents |= m.importers(modname) - in_file
        old = self.old_tree(path)
        try:
            new = ast.parse(new_text) if new_text is not None else None
        except SyntaxError:
            notes.append("test-syntax-error")
            return in_file | dependents
        if old is None or new is None:
            return in_file | dependents
        if dump(old) == dump(new):
            notes.append("ast-equal")
            return set()
        out = set()
        for s in diff_module(old, new):
            cls = [c for c in s.chain if isinstance(c, ast.ClassDef)]
            fn = [c for c in s.chain if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))]
            node_is_fn = isinstance(s.node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if cls and (fn or node_is_fn):
                f = fn[0] if fn else s.node
                tid = f"{path}::{cls[0].name}::{f.name}"
                if f.name.startswith("test") and tid in m.by_id and len(cls) == 1:
                    out.add(m.by_id[tid])
                    continue
                key = f"{path}::{cls[0].name}"
                out |= m.by_class.get(key, set())
                # subclasses in other files inherit helpers
                out |= dependents
                continue
            out |= in_file | dependents
        return out

    def analyze_file(self, f, notes):
        m = self.m
        old_path, new_path = f["old"], f["new"]
        path = old_path or new_path
        if f.get("binary"):
            notes.append("binary")
            return m.opened(path) if path else set()
        old_text = read_src(old_path) if old_path else None
        if old_path and old_text is None and not f.get("old_missing"):
            notes.append(f"missing-src:{old_path}")
        new_text = None
        if new_path:
            new_text, _, _ = apply_hunks(old_text if old_text is not None else "", f["hunks"]) if f["hunks"] else (old_text, None, None)
        out = set()
        if old_path and new_path and old_path != new_path:
            # rename: anything that read or imported the old path loses it
            out |= m.opened(old_path)
            if old_path.endswith(".py") and old_path.startswith("sqlglot/"):
                out |= self.import_failure(old_path)
        path = old_path or new_path
        if not (path.endswith(".py") and path.startswith("sqlglot/")) or old_path is None or new_path is None or old_path != new_path:
            self.other_change = True
        elif new_text is not None:
            self.changed_py[path] = new_text
        if path.endswith(".py") and path.startswith("sqlglot/"):
            if old_path is None:
                notes.append("new-module")
                return out
            if new_path is None:
                notes.append("deleted-module")
                return out | self.import_failure(old_path)
            return out | self.analyze_sqlglot_py(path, old_text, new_text, notes)
        if path.endswith(".py") and path.startswith("tests/"):
            if old_path is None:
                return out
            return out | self.analyze_test_py(path, old_text, new_text, notes)
        base = os.path.basename(path)
        if base in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py") or base.startswith("conftest"):
            changed = "\n".join(t for _, h in f["hunks"] for tag, t in h if tag in "+-")
            if re.search(r"pytest|addopts|filterwarnings|timeout|testpaths|python_files", changed):
                notes.append("pytest-config")
                return set(m.everything)
        out |= m.opened(path)
        if old_path:
            out |= m.opened(old_path)
        if out:
            notes.append(f"opened-by:{len(out)}")
        return out


# --------------------------------------------------------------------------- probe

PROBE = os.environ.get("BR_PROBE", "0") == "1"
HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "3000"))        # tests this slow are worth probing for
PROBE_BUDGET_MS = int(os.environ.get("BR_PROBE_BUDGET_MS", "2000"))  # measured probe CPU (ms) to spend
PROBE_MIN_TESTS = int(os.environ.get("BR_PROBE_MIN_TESTS", "3"))
PROBE_MIN_SAVING_MS = int(os.environ.get("BR_PROBE_MIN_SAVING_MS", "6000"))
PROBE_TIMEOUT = float(os.environ.get("BR_PROBE_TIMEOUT", "30"))
PROBE_WALL_BUDGET = float(os.environ.get("BR_PROBE_WALL", "100"))
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_runner.py")
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_server.py")
_server = None


def _probe_env():
    return {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "SKIP_INTEGRATION": "0"}


def probe_request(root, ids, run_all):
    """Failing ids from running `ids` against `root`, or None if the run can't be trusted."""
    import subprocess
    global _server
    if _server is not None or os.environ.get("BR_PROBE_SERVER", "1") == "1":
        try:
            if _server is None:
                _server = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, env=_probe_env(), cwd="/tmp")
            _server.stdin.write(json.dumps({"root": root, "src": SRC, "ids": ids, "all": run_all, "timeout": PROBE_TIMEOUT}) + "\n")
            _server.stdin.flush()
            line = _server.stdout.readline()
            if not line:
                raise RuntimeError("probe server died")
            reply = json.loads(line)
            if "failed" not in reply:
                log("probe", reply.get("error"))
                return None
            return reply["failed"]
        except Exception as e:  # noqa
            log("probe server", type(e).__name__, e)
            try:
                _server.kill()
            except Exception:  # noqa
                pass
            _server = False  # don't retry the server; fall through to one-shot runs
    try:
        r = subprocess.run([sys.executable, RUNNER, root, SRC] + (["--all"] if run_all else []), input=json.dumps(ids),
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT, env=_probe_env(), cwd="/tmp")
        if r.returncode != 0:
            return None
        return json.loads(r.stdout or "[]")
    except Exception as e:  # noqa
        log("probe", type(e).__name__)
        return None


def patched_root(changed):
    import shutil
    root = "/tmp/br_import"
    if not os.path.isdir(os.path.join(root, "sqlglot")):
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(os.path.join(SRC, "sqlglot"), os.path.join(root, "sqlglot"))
    for path, text in changed.items():
        with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def restore_root(changed):
    import shutil
    root = "/tmp/br_import"
    for path in changed:
        src = os.path.join(SRC, path)
        dst = os.path.join(root, path)
        if os.path.exists(src):
            shutil.copy2(src, dst)
        elif os.path.exists(dst):
            os.remove(dst)
_t0 = None


def run_probe(changed, test_ids):
    """True if any test fails (or the run misbehaves): the change is live."""
    try:
        failed = probe_request(patched_root(changed), test_ids, False)
    finally:
        restore_root(changed)
    return failed is None or bool(failed)


REPORT = {} if os.environ.get("BR_PROBE_REPORT") else None
PROBE_BASE_MS = 350  # interpreter + sqlglot
HEAVY_LIB_MS = 400   # duckdb + pandas, pulled in by these test modules
HEAVY_LIB_MODULES = ("tests/test_executor.py", "tests/test_optimizer.py")
EXACT = os.environ.get("BR_EXACT", "1") == "1"
EXACT_CPU_SCALE = float(os.environ.get("BR_EXACT_SCALE", "1.5"))   # scorer CPU vs measured cost model
EXACT_MAX_FRACTION = float(os.environ.get("BR_EXACT_FRACTION", "0.6"))


def probe_estimate(tids, maps):
    pc = maps.probe_cost
    c, sc, sm = PROBE_BASE_MS, set(), set()
    for tid in tids:
        if tid not in pc["test"]:
            return None
        cls, mod = tid.rsplit("::", 1)[0], tid.split("::")[0]
        c += pc["test"][tid]
        if cls not in sc:
            c += pc["class"].get(cls, 0)
            sc.add(cls)
        if mod not in sm:
            c += pc["module"].get(mod, 0)
            sm.add(mod)
    if any(m in sm for m in HEAVY_LIB_MODULES) and "tests/test_executor.py" not in sm:
        c += HEAVY_LIB_MS
    return c


def run_probe_all(changed, test_ids):
    """Runs every test; returns the failing ids, or None if the run can't be trusted."""
    try:
        failed = probe_request(patched_root(changed), test_ids, True)
    finally:
        restore_root(changed)
    if failed is None or any(f not in test_ids for f in failed):
        return None
    return set(failed)


def maybe_exact(sel, ms, an, maps, notes):
    """Small candidate sets on Python-only changes: run them all here and keep only the failures."""
    import time
    if not EXACT or not sel or not an.changed_py or an.other_change:
        return sel
    if any(ms[t] >= HEAVY_MS for t in sel):
        return sel
    if time.time() - _t0 > PROBE_WALL_BUDGET:
        return sel
    ids = [maps.id_of[t] for t in sorted(sel)]
    est = probe_estimate(ids, maps)
    cur = schedule(sel, ms)[1]
    if est is None or est * EXACT_CPU_SCALE > cur * EXACT_MAX_FRACTION:
        return sel
    failed = run_probe_all(an.changed_py, ids)
    if failed is None:
        notes.append("exact-untrusted")
        return sel
    notes.append(f"exact:{len(failed)}/{len(ids)}")
    return {maps.by_id[f] for f in failed}


def pick_probe(cands, maps):
    """Cheapest tests first, charging class setup and module import the first time each is used."""
    pc = maps.probe_cost
    tcost, ccost, mcost = pc["test"], pc["class"], pc["module"]
    items = []
    for t in cands:
        tid = maps.id_of[t]
        if tid not in tcost:
            continue
        cls = tid.rsplit("::", 1)[0]
        items.append((tcost[tid] + ccost.get(cls, 0) + mcost.get(tid.split("::")[0], 0), tid, t))
    items.sort()
    used, seen_c, seen_m, out = 0, set(), set(), []
    for _, tid, t in items:
        cls, mod = tid.rsplit("::", 1)[0], tid.split("::")[0]
        c = tcost[tid] + (0 if cls in seen_c else ccost.get(cls, 0)) + (0 if mod in seen_m else mcost.get(mod, 0))
        if used + c > PROBE_BUDGET_MS:
            continue
        used += c
        seen_c.add(cls)
        seen_m.add(mod)
        out.append(t)
    return out


EX_TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
OPT_TPCDS = "tests/test_optimizer.py::TestOptimizer::test_tpcds"
EXECUTION_CODE = ("sqlglot/executor/", "sqlglot/planner.py", "sqlglot/generators/python.py")
TPCDS_STAGE2 = os.environ.get("BR_TPCDS_STAGE2", "1") == "1"


def tpcds_second_stage(sel, heavy, ms, an, maps, notes):
    """The executor's TPC-DS test optimizes then executes the TPC-DS queries. If the optimizer's
    own TPC-DS test (same queries, optimized SQL compared to the expected text) still passes and
    the change is not in execution code, the executor sees the same plans, so it cannot fail."""
    ex, opt = maps.by_id.get(EX_TPCDS), maps.by_id.get(OPT_TPCDS)
    if not TPCDS_STAGE2 or ex not in heavy or opt not in sel:
        return sel
    if any(p.startswith(EXECUTION_CODE) for p in an.changed_py):
        return sel
    if schedule(sel, ms)[1] - schedule(sel - {ex}, ms)[1] < PROBE_MIN_SAVING_MS:
        return sel
    try:
        failed = probe_request(patched_root(an.changed_py), [OPT_TPCDS], False)
    finally:
        restore_root(an.changed_py)
    if failed == []:
        notes.append("tpcds-stage2-pass")
        return sel - {ex}
    notes.append("tpcds-stage2-live")
    return sel


MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
MAX_NODES_SMALL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maxnodes_small.py")
MAX_NODES_STAGE = os.environ.get("BR_MAX_NODES_STAGE", "1") == "1"
_max_nodes_base = None


def _run_small(root):
    import subprocess
    r = subprocess.run([sys.executable, MAX_NODES_SMALL, root, SRC], capture_output=True, text=True,
                       timeout=PROBE_TIMEOUT, env=_probe_env(), cwd="/tmp")
    if r.returncode != 0:
        return None
    return float(r.stdout.strip().splitlines()[-1])


def max_nodes_stage(sel, ms, an, maps, notes):
    """test_max_nodes (28.8 s) parses one huge generated query. A scaled-down copy of it (same
    statements, 100 columns instead of 10,000) runs in well under a second. If the scaled copy passes
    and is not slower than on the unpatched code (a slowdown could time the real test out), the real
    test cannot fail."""
    global _max_nodes_base
    mn = maps.by_id.get(MAX_NODES)
    if not MAX_NODES_STAGE or mn not in sel or not an.changed_py or an.other_change:
        return sel
    if schedule(sel, ms)[1] - schedule(sel - {mn}, ms)[1] < PROBE_MIN_SAVING_MS:
        return sel
    try:
        if _max_nodes_base is None:
            _max_nodes_base = _run_small(SRC) or 0.0
        t = _run_small(patched_root(an.changed_py))
    except Exception as e:  # noqa
        log("max_nodes stage", type(e).__name__)
        t = None
    finally:
        restore_root(an.changed_py)
    if t is None or not _max_nodes_base or t > _max_nodes_base * 1.5 + 0.05:
        notes.append("max-nodes-live")
        return sel
    notes.append("max-nodes-pass")
    return sel - {mn}


GENMEM = "tests/test_generator.py::TestGenerator::test_generator_memory"
GENMEM_CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "genmem_check.py")
GENMEM_STAGE = os.environ.get("BR_GENMEM_STAGE", "1") == "1"


def genmem_stage(sel, ms, an, maps, notes):
    """test_generator_memory runs the same leak check once per dialect. Run that exact check here,
    only for the dialects built from (or importing) the changed modules."""
    import subprocess
    gm = maps.by_id.get(GENMEM)
    if not GENMEM_STAGE or gm not in sel or not an.changed_py or an.other_change:
        return sel
    if schedule(sel, ms)[1] - schedule(sel - {gm}, ms)[1] < 2000:
        return sel
    mods = [module_name(p) for p in an.changed_py]
    try:
        root = patched_root(an.changed_py)
        r = subprocess.run([sys.executable, GENMEM_CHECK, root, SRC] + mods, capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT, env=_probe_env(), cwd="/tmp")
        code = r.returncode
    except Exception as e:  # noqa
        log("genmem stage", type(e).__name__)
        code = 1
    finally:
        restore_root(an.changed_py)
    if code == 0:
        notes.append(f"genmem-pass:{len(r.stdout.split())}")
        return sel - {gm}
    notes.append("genmem-keep" if code == 2 else "genmem-live")
    return sel


def maybe_drop_heavy(sel, ms, an, maps, notes):
    import time
    heavy = {t for t in sel if ms[t] >= HEAVY_MS}
    if not PROBE or not heavy or not an.changed_py or an.other_change:
        return sel
    if time.time() - _t0 > PROBE_WALL_BUDGET:
        notes.append("probe-skipped-time")
        return sel
    rest = sel - heavy
    saving = schedule(sel, ms)[1] - schedule(rest, ms)[1]
    if saving < PROBE_MIN_SAVING_MS:
        return sel
    probe = pick_probe(rest, maps)
    if len(probe) < PROBE_MIN_TESTS:
        notes.append("probe-too-few")
        return sel
    if REPORT is not None:
        REPORT[an.cid] = {"heavy": [maps.id_of[t] for t in heavy], "probe": [maps.id_of[t] for t in probe]}
        return sel
    ids = [maps.id_of[t] for t in probe]
    if run_probe(an.changed_py, ids):
        notes.append(f"probe-live:{len(probe)}")
        return tpcds_second_stage(sel, heavy, ms, an, maps, notes)
    notes.append(f"probe-pass:{len(probe)}:drop{len(heavy)}")
    return rest


# --------------------------------------------------------------------------- scheduling

def schedule(sel, ms):
    """Pack tests onto 1..8 workers, minimising busiest load + 2 s per worker (LPT per k)."""
    if not sel:
        return [], 0
    items = sorted(sel, key=lambda t: -ms[t])
    total = sum(ms[t] for t in items)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(items):
            break
        lb = max(ms[items[0]], -(-total // k)) + WORKER_MS * k
        if best is not None and lb >= best[0]:
            continue
        loads = [0] * k
        bins = [[] for _ in range(k)]
        for t in items:
            j = loads.index(min(loads))
            bins[j].append(t)
            loads[j] += ms[t]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1], best[0]


def main():
    global _t0
    import time
    _t0 = time.time()
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
    maps = Maps(tests)
    an = Analyzer(maps, tests)
    unmapped = maps.everything - maps.mapped
    cdir = os.path.join(DATA, "commits")
    total = 0
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".patch"):
            continue
        cid = fn[: -len(".patch")]
        notes = []
        try:
            text = open(os.path.join(cdir, fn), "rb").read().decode("utf-8", "replace")
            files = parse_patch(text)
            sel = set()
            an.pending = {}
            an.cid = cid
            an.changed_py = {}
            an.other_change = False
            for f in files:
                sel |= an.analyze_file(f, notes)
            if an.pending:
                for path in an.import_check(an.pending):
                    notes.append(f"import-fails:{path}")
                    sel |= an.import_failure(path)
            if not files and text.strip():
                notes.append("unparsed-patch")
                sel = set(maps.everything)
            if sel and unmapped:
                sel |= unmapped
        except Exception as e:  # never crash: fall back to everything
            notes.append(f"error:{type(e).__name__}:{e}")
            sel = set(maps.everything)
        sel &= maps.everything
        if not any(n.startswith("error") for n in notes):
            try:
                sel = max_nodes_stage(sel, ms, an, maps, notes)
                sel = genmem_stage(sel, ms, an, maps, notes)
                sel = maybe_drop_heavy(sel, ms, an, maps, notes)
                sel = maybe_exact(sel, ms, an, maps, notes)
            except Exception as e:  # noqa
                log("probe error", e)
        bins, cost = schedule(sel, ms)
        total += cost
        log(cid, len(sel), cost / 1000, notes[:6])
        print(json.dumps({"commit": cid, "workers": bins}, separators=(",", ":")))
    if _server:
        try:
            _server.stdin.close()
            _server.wait(timeout=10)
        except Exception:  # noqa
            _server.kill()
    log("simulated selection cost", total / 1000)
    if REPORT is not None:
        with open(os.environ["BR_PROBE_REPORT"], "w") as fh:
            json.dump(REPORT, fh)


if __name__ == "__main__":
    main()
