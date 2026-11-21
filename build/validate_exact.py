"""Random single-line mutants; for those whose candidate set is cheap to run inline, compare the
probe runner (--all) with real pytest on exactly those tests. Any probe-pass/pytest-fail is fatal."""
import ast, difflib, gzip, json, os, random, re, shutil, subprocess, sys, tempfile, resource
SRC = "/home/claude/sqlglot"; ENTRY = "/home/claude/entry"
sys.path.insert(0, "/home/claude/harness")
OPS = [(r"\band\b", "or"), (r"\bor\b", "and"), (r"==", "!="), (r"!=", "=="), (r" is not ", " is "), (r" is None", " is not None"),
       (r"<=", "<"), (r"(?<![<=!])<(?!=)", "<="), (r">=", ">"), (r"(?<![>=-])>(?!=)", ">="), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
       (r"\bif not ", "if "), (r"\[0\]", "[1]"), (r"(?<![\w.])1(?![\w.])", "2"), (r"(?<![\w.])0(?![\w.])", "1"), (r"\+ 1\b", "- 1"),
       (r"\breturn ", "return None or ")]
tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]; ids = [t["id"] for t in tests]
M = json.loads(gzip.open(f"{ENTRY}/maps/maps.json.gz").read()); pc = M["probe_cost"]
def est(tids):
    c, sc, sm = 0, set(), set()
    for i in tids:
        cl, mo = i.rsplit("::", 1)[0], i.split("::")[0]
        if i not in pc["test"]: return 10**9
        c += pc["test"][i] + (0 if cl in sc else pc["class"].get(cl, 0)) + (0 if mo in sm else pc["module"].get(mo, 0)); sc.add(cl); sm.add(mo)
    return c
seed, n, cap = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]); random.seed(seed)
pool = []
for rel, d in M["lines"].items():
    il = set(M["import_lines"].get(rel, []))
    lines = open(os.path.join(SRC, rel)).read().split("\n")
    for ln in d:
        ln = int(ln)
        if ln in il or ln > len(lines): continue
        s = lines[ln - 1].strip()
        if not s or s.startswith(("#", "def ", "class ", "@")): continue
        for pat, rep in OPS:
            if re.search(pat, lines[ln - 1]): pool.append((rel, ln, pat, rep))
out = open(f"/home/claude/harness/exact_{seed}.jsonl", "a"); done = tried = 0
while done < n and tried < n * 20:
    tried += 1
    rel, ln, pat, rep = random.choice(pool)
    lines = open(os.path.join(SRC, rel)).read().split("\n")
    m = random.choice(list(re.finditer(pat, lines[ln - 1])))
    new = lines[:]; new[ln - 1] = lines[ln - 1][:m.start()] + rep + lines[ln - 1][m.end():]
    try: ast.parse("\n".join(new))
    except SyntaxError: continue
    patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{rel}", f"b/{rel}"))
    with tempfile.TemporaryDirectory() as dd:
        os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json", dd)
        open(f"{dd}/commits/m.patch", "w").write(patch)
        r = subprocess.run([sys.executable, "-I", f"{ENTRY}/selector.py"], env={**os.environ, "BR_DATA": dd, "BR_SRC": f"{ENTRY}/src", "BR_PROBE": "0"}, capture_output=True, text=True)
        sel = [ids[t] for w in json.loads(r.stdout)["workers"] for t in w]
    if not sel or est(sel) > cap: continue
    w = tempfile.mkdtemp()
    shutil.copytree(SRC, f"{w}/repo", ignore=shutil.ignore_patterns(".git", "__pycache__", ".coverage*"))
    subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=f"{w}/repo", check=True)
    try:
        p = subprocess.run([sys.executable, f"{ENTRY}/probe_runner.py", f"{w}/repo", f"{ENTRY}/src", "--all"], input=json.dumps(sel), capture_output=True, text=True, timeout=60)
        probe = set(json.loads(p.stdout or "[]")) if p.returncode == 0 else {"CRASH"}
    except subprocess.TimeoutExpired:
        probe = {"TIMEOUT"}
    xml = f"{w}/r.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--timeout", "120", f"--junitxml={xml}"] + sel, cwd=f"{w}/repo", env={**os.environ, "SKIP_INTEGRATION": "0", "PYTHONPATH": f"{w}/repo"}, capture_output=True, timeout=900)
    import xml.etree.ElementTree as ET
    truth = set()
    for tc in ET.parse(xml).iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            cls = tc.get("classname", ""); path = cls.rsplit(".", 1)[0].replace(".", "/") + ".py"
            truth.add(f"{path}::{cls.rsplit('.', 1)[1]}::{tc.get('name').split('[')[0]}")
    shutil.rmtree(w, ignore_errors=True)
    fatal = sorted(truth - probe) if not (probe & {"CRASH", "TIMEOUT"}) else []
    extra = sorted(probe - truth)
    rec = {"file": rel, "line": ln, "op": rep, "n": len(sel), "est": est(sel), "probe": sorted(probe), "truth": sorted(truth), "fatal": fatal, "extra": extra, "patch": patch}
    out.write(json.dumps(rec) + "\n"); out.flush(); done += 1
    print(done, rel, ln, rep, "n", len(sel), "est", est(sel), "truth", len(truth), "probe", len(probe), "FATAL" if fatal else "", fatal[:2], "extra" if extra else "", extra[:2], flush=True)
