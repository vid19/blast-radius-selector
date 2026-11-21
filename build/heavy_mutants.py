import json, random, sys, os, re, ast, difflib
sys.path.insert(0, "/home/claude/harness")
from probe_eval import evaluate, ids, secs
SRC = "/home/claude/sqlglot"
OPS = [(r"\band\b", "or"), (r"\bor\b", "and"), (r"==", "!="), (r"!=", "=="), (r" is not ", " is "), (r" is None", " is not None"),
       (r"<=", "<"), (r"(?<![<=!])<(?!=)", "<="), (r">=", ">"), (r"(?<![>=-])>(?!=)", ">="), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
       (r"\bif not ", "if "), (r"\[0\]", "[1]"), (r"(?<![\w.])1(?![\w.])", "2"), (r"(?<![\w.])0(?![\w.])", "1"), (r"\+ 1\b", "- 1"),
       (r"\breturn ", "return None or ")]
seed, n = int(sys.argv[1]), int(sys.argv[2]); random.seed(seed)
import gzip
M = json.loads(gzip.open("/home/claude/entry/maps/maps.json.gz").read())
cov = {"cov": {f: {l: M["sets"][s] for l, s in d.items()} for f, d in M["lines"].items()}}
num = {t: i for i, t in enumerate(ids)}
H = {num["tests/test_executor.py::TestExecutor::test_execute_tpcds"], num["tests/test_parser.py::TestParser::test_max_nodes"]}
imp = json.load(open("/home/claude/maps_raw/import_lines.json"))
pool = []
for rel, d in cov["cov"].items():
    lines = open(os.path.join(SRC, rel)).read().split("\n")
    il = set(imp.get(rel, []))
    for ln, s in d.items():
        ln = int(ln)
        if ln in il or not (H & set(s)): continue
        text = lines[ln - 1]; st = text.strip()
        if not st or st.startswith(("#", "def ", "class ", "@", '"', "'")): continue
        for pat, rep in OPS:
            if re.search(pat, text): pool.append((rel, ln, pat, rep))
print("pool", len(pool), flush=True)
out = open(f"/home/claude/harness/heavy_{seed}.jsonl", "a"); done = 0
while done < n:
    rel, ln, pat, rep = random.choice(pool)
    text = open(os.path.join(SRC, rel)).read(); lines = text.split("\n")
    m = random.choice(list(re.finditer(pat, lines[ln - 1])))
    new = lines[:]; new[ln - 1] = lines[ln - 1][:m.start()] + rep + lines[ln - 1][m.end():]
    try: ast.parse("\n".join(new))
    except SyntaxError: continue
    patch = f"diff --git a/{rel} b/{rel}\n" + "".join(difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{rel}", f"b/{rel}"))
    rec = evaluate(patch, run_heavy=True)
    if not rec.get("heavy"): continue
    rec.update({"file": rel, "line": ln, "op": rep, "patch": patch})
    out.write(json.dumps(rec) + "\n"); out.flush(); done += 1
    tf = set(rec.get("true_fail", [])); hv = [h.split("::")[-1] for h in rec["heavy"]]
    print(done, rel, ln, rep, "heavy", hv, "heavyfail", [h.split("::")[-1] for h in rec["heavy"] if h in tf], "cheapfail", len(tf - set(rec["heavy"])), flush=True)
