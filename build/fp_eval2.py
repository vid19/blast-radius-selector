import ast, difflib, gzip, json, os, random, re, shutil, subprocess, sys, tempfile
SRC="/home/claude/sqlglot"; ENTRY="/home/claude/entry"
OPS=[(r"\band\b","or"),(r"\bor\b","and"),(r"==","!="),(r"!=","=="),(r" is not "," is "),(r"\bTrue\b","False"),(r"\bFalse\b","True"),(r"\bif not ","if "),(r"(?<![\w.])1(?![\w.])","2"),(r"(?<![\w.])0(?![\w.])","1"),(r"\breturn ","return None or "),(r'"([a-z_]+)"','"\\1x"')]
M=json.loads(gzip.open(f"{ENTRY}/maps/maps.json.gz").read()); EX=M["ids"].index("tests/test_executor.py::TestExecutor::test_execute_tpcds")
WL=("sqlglot/generators/duckdb.py","sqlglot/dialects/duckdb.py","sqlglot/planner.py")
pool=[]
for rel,d in M["lines"].items():
    if rel not in WL: continue
    L=open(f"{SRC}/{rel}").read().split("\n")
    for ln,s in d.items():
        if EX not in M["sets"][s]: continue
        ln=int(ln); st=L[ln-1].strip()
        if not st or st.startswith(("#","def ","class ","@")): continue
        for p,r in OPS:
            if re.search(p,L[ln-1]): pool.append((rel,ln,p,r))
rnd=random.Random(int(sys.argv[1])); n=int(sys.argv[2])
base=open("/tmp/fp1.txt").read()
open("/tmp/fp_base.txt","w").write(base)
w=tempfile.mkdtemp()+"/repo"; shutil.copytree(SRC,w,ignore=shutil.ignore_patterns(".git","__pycache__",".coverage*"))
env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","SKIP_INTEGRATION":"0","PYTHONPATH":w}
print("pool",len(pool),flush=True); done=0
# favour the executor-only files
weights=[4 if r.endswith("planner.py") else 1 for r,_,_,_ in pool]
while done<n:
    rel,ln,p,r=rnd.choices(pool,weights)[0]; L=open(f"{SRC}/{rel}").read().split("\n")
    m=rnd.choice(list(re.finditer(p,L[ln-1]))); N=L[:]; N[ln-1]=L[ln-1][:m.start()]+r+L[ln-1][m.end():]
    try: ast.parse("\n".join(N))
    except SyntaxError: continue
    open(f"{w}/{rel}","w").write("\n".join(N))
    try: fp=subprocess.run([sys.executable,f"{ENTRY}/tpcds_fingerprint.py",w,f"{ENTRY}/src","/tmp/fp_base.txt"],env=env,capture_output=True,timeout=120).returncode
    except subprocess.TimeoutExpired: fp=9
    try: ex=subprocess.run([sys.executable,"-m","pytest","-p","no:cacheprovider","-q","--timeout","300","tests/test_executor.py::TestExecutor::test_execute_tpcds"],cwd=w,env=env,capture_output=True,text=True,timeout=400).returncode
    except subprocess.TimeoutExpired: ex=1
    shutil.copyfile(f"{SRC}/{rel}",f"{w}/{rel}")
    v="same" if fp==0 else "CHANGED"; e="FAIL" if ex else "pass"
    print(done,rel.split("/")[-1],ln,r,"fingerprint",v,"executor",e,"DANGER" if (fp==0 and ex) else "",flush=True)
    open("/home/claude/harness/fp_eval.jsonl","a").write(json.dumps({"rel":rel,"line":ln,"op":r,"fp":v,"ex":e})+"\n")
    done+=1
