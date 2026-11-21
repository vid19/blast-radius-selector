"""Structural mutants the sample lacks; full-suite truth; run the real v4 selector (probe on)."""
import ast, difflib, json, os, random, re, shutil, subprocess, sys, tempfile
SRC = "/home/claude/sqlglot"; ENTRY = "/home/claude/entry"
tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]; ids = [t["id"] for t in tests]
def diff(rel, old, new):
    return f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))
def funcs(rel):
    t = ast.parse(open(f"{SRC}/{rel}").read())
    return [n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and len(n.body) >= 3]
def mk(kind, rnd):
    files = ["sqlglot/optimizer/simplify.py", "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/optimizer/qualify_columns.py",
             "sqlglot/dialects/duckdb.py", "sqlglot/generators/snowflake.py", "sqlglot/parsers/bigquery.py", "sqlglot/expressions/core.py",
             "sqlglot/transforms.py", "sqlglot/optimizer/scope.py", "sqlglot/tokens.py", "sqlglot/helper.py"]
    rel = rnd.choice(files); text = open(f"{SRC}/{rel}").read(); L = text.split("\n")
    if kind in ("delete_stmt", "insert_early_return", "insert_live_return", "swap_stmts") and not funcs(rel):
        return None
    if kind == "delete_stmt":
        f = rnd.choice(funcs(rel)); s = rnd.choice(f.body[1:])
        if s.end_lineno != s.lineno or isinstance(s, ast.Return): return None
        ind = len(L[s.lineno - 1]) - len(L[s.lineno - 1].lstrip())
        L[s.lineno - 1] = " " * ind + "pass"
    elif kind == "insert_early_return":
        f = rnd.choice(funcs(rel)); s = f.body[-1]
        b = f.body[1] if isinstance(f.body[0], ast.Expr) and isinstance(getattr(f.body[0], "value", None), ast.Constant) else f.body[0]
        ind = len(L[b.lineno - 1]) - len(L[b.lineno - 1].lstrip())
        L.insert(b.lineno - 1, " " * ind + "if len(str(id(object))) < 0:\n" + " " * (ind + 4) + "return None")
        # dead-code insertion: semantically equivalent, tests nothing breaks
    elif kind == "insert_live_return":
        f = rnd.choice(funcs(rel))
        b = f.body[1] if isinstance(f.body[0], ast.Expr) and isinstance(getattr(f.body[0], "value", None), ast.Constant) else f.body[0]
        ind = len(L[b.lineno - 1]) - len(L[b.lineno - 1].lstrip())
        L.insert(b.lineno - 1, " " * ind + "return None")
    elif kind == "rename_func":
        cands = [n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)]
        if not cands: return None
        f = rnd.choice(cands); L[f.lineno - 1] = L[f.lineno - 1].replace(f"def {f.name}(", f"def {f.name}_renamed(", 1)
    elif kind == "default_arg":
        cands = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef) and n.args.defaults and isinstance(n.args.defaults[-1], ast.Constant) and isinstance(n.args.defaults[-1].value, bool)]
        if not cands: return None
        f = rnd.choice(cands); d = f.args.defaults[-1]
        line = L[d.lineno - 1]; v = str(d.value); nv = str(not d.value)
        L[d.lineno - 1] = line[:d.col_offset] + nv + line[d.col_offset + len(v):]
    elif kind == "swap_stmts":
        f = rnd.choice(funcs(rel)); i = rnd.randrange(0, len(f.body) - 1); a, b = f.body[i], f.body[i + 1]
        if a.end_lineno != a.lineno or b.end_lineno != b.lineno or isinstance(b, ast.Return) or isinstance(a, ast.Return): return None
        L[a.lineno - 1], L[b.lineno - 1] = L[b.lineno - 1], L[a.lineno - 1]
    new = "\n".join(L)
    if new == text: return None
    try: ast.parse(new)
    except SyntaxError: return None
    return rel, diff(rel, text, new)
def fixture_comment(rnd):
    fx = sorted(p for p in os.popen(f"cd {SRC} && git ls-files tests/fixtures/optimizer/*.sql").read().split())
    rel = rnd.choice(fx); text = open(f"{SRC}/{rel}").read(); L = text.split("\n")
    i = rnd.randrange(len(L)); L.insert(i, "-- reviewed")
    return rel, diff(rel, text, "\n".join(L))
def test_edit(rnd):
    rel = rnd.choice(["tests/test_transpile.py", "tests/dialects/test_duckdb.py", "tests/test_optimizer.py", "tests/test_parser.py"])
    text = open(f"{SRC}/{rel}").read(); L = text.split("\n")
    idx = [i for i, l in enumerate(L) if "assertEqual(" in l and l.strip().startswith("self.assertEqual(")]
    i = rnd.choice(idx); L[i] = L[i].replace("self.assertEqual(", "self.assertNotEqual(", 1)
    return rel, diff(rel, text, "\n".join(L))
START = int(sys.argv[3]) if len(sys.argv) > 3 else 0
def run(seed, n):
    rnd = random.Random(seed); kinds = ["delete_stmt", "insert_live_return", "insert_early_return", "rename_func", "default_arg", "swap_stmts", "fixture", "test_edit", "multi"]
    repo = tempfile.mkdtemp() + "/repo"; shutil.copytree(SRC, repo, ignore=shutil.ignore_patterns(".git", "__pycache__", ".coverage*"))
    done = 0
    while done < n:
        kind = kinds[(done + START) % len(kinds)]
        if kind == "fixture": m = fixture_comment(rnd)
        elif kind == "test_edit": m = test_edit(rnd)
        elif kind == "multi":
            a = mk("insert_live_return", rnd); b = fixture_comment(rnd)
            m = (a[0] + "+" + b[0], a[1] + b[1]) if a else None
        else: m = mk(kind, rnd)
        if not m: continue
        rel, patch = m
        with tempfile.TemporaryDirectory() as dd:
            os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json", dd); open(f"{dd}/commits/m.patch", "w").write(patch)
            r = subprocess.run([sys.executable, "-I", f"{ENTRY}/selector.py"], env={**os.environ, "BR_DATA": dd, "BR_SRC": f"{ENTRY}/src", "BR_PROBE": "1"}, capture_output=True, text=True)
            sel = {ids[t] for w in json.loads(r.stdout)["workers"] for t in w}
            note = r.stderr.strip().splitlines()[-2] if len(r.stderr.strip().splitlines()) > 1 else ""
        bk = tempfile.mkdtemp(); shutil.copytree(repo, bk + "/r", ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=repo, check=True)
        xml = f"{bk}/r.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-n", "2", "--timeout", "180", f"--junitxml={xml}", "tests"], cwd=repo, env={**os.environ, "SKIP_INTEGRATION": "0", "PYTHONPATH": repo}, capture_output=True, timeout=1500)
        import xml.etree.ElementTree as ET
        fail = set()
        for tc in ET.parse(xml).iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                cls = tc.get("classname", "")
                if "." not in cls:  # collection error: the whole file failed to import
                    mod = (tc.get("name") or cls).replace(".", "/")
                    mod = mod if mod.endswith(".py") else mod + ".py"
                    fail |= {i for i in ids if i.split("::")[0] == mod} or {"COLLECT:" + mod}
                    continue
                path = cls.rsplit(".", 1)[0].replace(".", "/") + ".py"
                fail.add(f"{path}::{cls.rsplit('.', 1)[1]}::{tc.get('name').split('[')[0]}")
        shutil.rmtree(repo); shutil.move(bk + "/r", repo)
        miss = sorted(fail - sel)
        print(done, kind, rel, "sel", len(sel), "fail", len(fail), "MISS" if miss else "ok", miss[:3], note[:90], flush=True)
        open("/home/claude/harness/kinds.jsonl", "a").write(json.dumps({"kind": kind, "rel": rel, "patch": patch, "sel": sorted(sel), "fail": sorted(fail), "miss": miss}) + "\n")
        done += 1
run(int(sys.argv[1]), int(sys.argv[2]))
