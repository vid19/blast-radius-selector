import ast, difflib, gzip, json, os, random, re, shutil, subprocess, sys, tempfile
SRC="/home/claude/sqlglot"; ENTRY="/home/claude/entry"
OPS=[(r"\band\b","or"),(r"\bor\b","and"),(r"==","!="),(r"!=","=="),(r" is not "," is "),(r"<=","<"),(r">=",">"),(r"\bTrue\b","False"),(r"\bFalse\b","True"),(r"\bif not ","if "),(r"(?<![\w.])1(?![\w.])","2"),(r"(?<![\w.])0(?![\w.])","1"),(r"\breturn ","return None or ")]
tests=json.load(open("/home/claude/kit/sample/tests.json"))["tests"]; ids=[t["id"] for t in tests]
HEAVY={"tests/test_parser.py::TestParser::test_max_nodes","tests/test_generator.py::TestGenerator::test_generator_memory","tests/test_executor.py::TestExecutor::test_execute_tpch","tests/test_executor.py::TestExecutor::test_optimized_tpch"}
target=sys.argv[3]
M=json.loads(gzip.open(f"{ENTRY}/maps/maps.json.gz").read()); tix=M["ids"].index(target)
pool=[]
for rel,d in M["lines"].items():
    il=set(M["import_lines"].get(rel,[])); L=open(f"{SRC}/{rel}").read().split("\n")
    for ln,s in d.items():
        ln=int(ln)
        if tix not in M["sets"][s]: continue
        st=L[ln-1].strip()
        if not st or st.startswith(("#","def ","class ","@")): continue
        for p,r in OPS:
            if re.search(p,L[ln-1]): pool.append((rel,ln,p,r))
rnd=random.Random(int(sys.argv[1])); repo=tempfile.mkdtemp()+"/r"; shutil.copytree(SRC,repo,ignore=shutil.ignore_patterns(".git","__pycache__",".coverage*"))
print("pool",len(pool),flush=True); done=0
while done<int(sys.argv[2]):
    rel,ln,p,r=rnd.choice(pool); L=open(f"{SRC}/{rel}").read().split("\n")
    m=rnd.choice(list(re.finditer(p,L[ln-1]))); N=L[:]; N[ln-1]=L[ln-1][:m.start()]+r+L[ln-1][m.end():]
    try: ast.parse("\n".join(N))
    except SyntaxError: continue
    patch=f"diff --git a/{rel} b/{rel}\n"+"".join(difflib.unified_diff([x+"\n" for x in L],[x+"\n" for x in N],f"a/{rel}",f"b/{rel}"))
    with tempfile.TemporaryDirectory() as dd:
        os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json",dd); open(f"{dd}/commits/m.patch","w").write(patch)
        o=subprocess.run([sys.executable,"-I",f"{ENTRY}/selector.py"],env={**os.environ,"BR_DATA":dd,"BR_SRC":f"{ENTRY}/src","BR_PROBE":"1"},capture_output=True,text=True)
        sel={ids[t] for w in json.loads(o.stdout)["workers"] for t in w}
        note=[l for l in o.stderr.splitlines() if l.startswith("m ")]
    if target not in sel: continue
    orig=open(f"{repo}/{rel}").read(); open(f"{repo}/{rel}","w").write("\n".join(N))
    pr=subprocess.run([sys.executable,"-m","pytest","-p","no:cacheprovider","-q","--timeout","300",target],cwd=repo,env={**os.environ,"SKIP_INTEGRATION":"0","PYTHONPATH":repo},capture_output=True,text=True)
    open(f"{repo}/{rel}","w").write(orig)
    res="FAIL" if pr.returncode!=0 else "pass"
    print(done,rel,ln,r,"sel",len(sel),res,(note[0][:80] if note else ""),flush=True)
    open(f"/home/claude/harness/heavy_rate.jsonl","a").write(json.dumps({"target":target,"rel":rel,"line":ln,"op":r,"res":res,"nsel":len(sel),"note":note,"patch":patch})+"\n")
    done+=1
