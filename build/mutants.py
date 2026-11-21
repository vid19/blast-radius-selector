"""Offline evaluation harness: make single-line mutants of the pinned sqlglot (like the sample
patches), run the selector on each, run the selected tests (or the full suite) on the mutant,
and record failures. This is for measuring recall/cost only; nothing from it ships."""
import ast, difflib, json, os, random, re, shutil, subprocess, sys, tempfile, time
SRC = os.environ.get("SQLGLOT", "/home/claude/sqlglot")
MUT = "/tmp/mutrepo"
OPS = [
    (r"\band\b", "or"), (r"\bor\b", "and"), (r"==", "!="), (r"!=", "=="), (r" is not ", " is "),
    (r" is ", " == "), (r"<=", "<"), (r"(?<![<=!])<(?!=)", "<="), (r">=", ">"), (r"(?<![>=-])>(?!=)", ">="),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bif not ", "if "), (r"\[0\]", "[1]"),
    (r"(?<![\w.])1(?![\w.])", "2"), (r"(?<![\w.])0(?![\w.])", "1"), (r"\+ 1\b", "- 1"),
]
def candidates_lines(cov):
    out = []
    for rel, d in cov.items():
        if not rel.startswith("sqlglot/") or "/executor/" in rel and random.random() < 0.5:
            continue
        lines = open(os.path.join(SRC, rel)).read().split("\n")
        for i, text in enumerate(lines, 1):
            s = text.strip()
            if not s or s.startswith(("#", "import", "from", "def ", "class ", "@", '"', "'")):
                continue
            for pat, rep in OPS:
                if re.search(pat, s):
                    out.append((rel, i, pat, rep))
    return out
def make_patch(rel, i, pat, rep):
    text = open(os.path.join(SRC, rel)).read()
    lines = text.split("\n")
    ms = list(re.finditer(pat, lines[i - 1]))
    m = random.choice(ms)
    new_line = lines[i - 1][:m.start()] + (rep if not pat.startswith(" is") else rep) + lines[i - 1][m.end():]
    if new_line == lines[i - 1]:
        return None
    new = lines[:]; new[i - 1] = new_line
    try: ast.parse("\n".join(new))
    except SyntaxError: return None
    d = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{rel}", f"b/{rel}", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "".join(d)
def run_pytest(ids, timeout):
    if not ids: return set(), 0.0
    with tempfile.TemporaryDirectory() as td:
        xml = os.path.join(td, "r.xml")
        args = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-n", "2", "--timeout", str(timeout), f"--junitxml={xml}"] + ids
        t = time.time()
        subprocess.run(args, cwd=MUT, env={**os.environ, "SKIP_INTEGRATION": "0", "PYTHONPATH": MUT}, capture_output=True, timeout=1800)
        el = time.time() - t
        import xml.etree.ElementTree as ET
        failed = set()
        if os.path.exists(xml):
            for tc in ET.parse(xml).iter("testcase"):
                if tc.find("failure") is not None or tc.find("error") is not None:
                    f = tc.get("file") or ""
                    cls = tc.get("classname", "")
                    path = cls.rsplit(".", 1)[0].replace(".", "/") + ".py"
                    failed.add(f"{path}::{cls.rsplit('.',1)[1]}::{tc.get('name').split('[')[0]}")
        return failed, el
def main():
    seed, n, full_every = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    random.seed(seed)
    tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
    allids = [t["id"] for t in tests]
    cov = json.load(open("/home/claude/maps_raw/cov.json"))["cov"]
    pool = candidates_lines(cov)
    if os.path.exists(MUT): shutil.rmtree(MUT)
    shutil.copytree(SRC, MUT, ignore=shutil.ignore_patterns(".git", "__pycache__", ".coverage*"))
    out = open(f"/home/claude/harness/results_{seed}.jsonl", "a")
    done = 0
    while done < n:
        rel, i, pat, rep = random.choice(pool)
        patch = make_patch(rel, i, pat, rep)
        if not patch: continue
        with tempfile.TemporaryDirectory() as dd:
            os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json", dd)
            open(f"{dd}/commits/m.patch", "w").write(patch)
            r = subprocess.run([sys.executable, "-I", "/home/claude/entry/selector.py"], env={**os.environ, "BR_DATA": dd, "BR_SRC": SRC}, capture_output=True, text=True)
            sel = [allids[t] for w in json.loads(r.stdout)["workers"] for t in w]
        shutil.copy(os.path.join(SRC, rel), "/tmp/orig.py")
        subprocess.run(["git", "apply", "--unsafe-paths", "--directory=.", "-"], input=patch, text=True, cwd=MUT, check=True)
        full = full_every and done % full_every == 0
        try:
            failed, el = run_pytest(allids if full else sel, 120)
        finally:
            shutil.copy("/tmp/orig.py", os.path.join(MUT, rel))
        rec = {"file": rel, "line": i, "op": [pat, rep], "patch": patch, "selected": sel, "failed": sorted(failed), "full": bool(full), "elapsed": el}
        out.write(json.dumps(rec) + "\n"); out.flush()
        miss = sorted(set(failed) - set(sel))
        print(done, rel, i, rep, "sel", len(sel), "fail", len(failed), "MISS" if miss else "", miss[:3], round(el,1), flush=True)
        done += 1
main()
