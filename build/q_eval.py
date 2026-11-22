import ast, difflib, gzip, json, os, random, re, shutil, subprocess, sys, tempfile
SRC="/home/claude/sqlglot"; ENTRY="/home/claude/entry"
OPS=[(r"\band\b","or"),(r"\bor\b","and"),(r"==","!="),(r"!=","=="),(r" is not "," is "),(r"\bTrue\b","False"),(r"\bFalse\b","True"),(r"\bif not ","if "),(r"(?<![\w.])1(?![\w.])","2"),(r"(?<![\w.])0(?![\w.])","1"),(r"\breturn ","return None or "),(r'"([a-z_]+)"','"\\1x"')]
EXID="tests/test_executor.py::TestExecutor::test_execute_tpcds"
M=json.loads(gzip.open(f"{ENTRY}/maps/maps.json.gz").read()); EX=M["ids"].index(EXID)
FILES=("sqlglot/executor/python.py","sqlglot/executor/context.py","sqlglot/executor/env.py","sqlglot/executor/table.py","sqlglot/generators/python.py","sqlglot/planner.py")
pool=[]
for rel in FILES:
    L=open(f"{SRC}/{rel}").read().split("\n"); d=M["lines"].get(rel,{})
    for ln,s in d.items():
        if EX not in M["sets"][s]: continue
        st=L[int(ln)-1].strip()
        if not st or st.startswith(("#","def ","class ","@")): continue
        for p,r in OPS:
            if re.search(p,L[int(ln)-1]): pool.append((rel,int(ln),p,r))
tests=json.load(open("/home/claude/kit/sample/tests.json"))["tests"]; ids=[t["id"] for t in tests]
rnd=random.Random(int(sys.argv[1])); n=int(sys.argv[2])
w=tempfile.mkdtemp()+"/repo"; shutil.copytree(SRC,w,ignore=shutil.ignore_patterns(".git","__pycache__",".coverage*"))
env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","SKIP_INTEGRATION":"0","PYTHONPATH":w}
print("pool",len(pool),flush=True); done=0; drops=0
while done<n:
    rel,ln,p,r=rnd.choice(pool); L=open(f"{SRC}/{rel}").read().split("\n")
    m=rnd.choice(list(re.finditer(p,L[ln-1]))); N=L[:]; N[ln-1]=L[ln-1][:m.start()]+r+L[ln-1][m.end():]
    try: ast.parse("\n".join(N))
    except SyntaxError: continue
    patch=f"diff --git a/{rel} b/{rel}\n"+"".join(difflib.unified_diff([x+"\n" for x in L],[x+"\n" for x in N],f"a/{rel}",f"b/{rel}"))
    with tempfile.TemporaryDirectory() as dd:
        os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json",dd); open(f"{dd}/commits/m.patch","w").write(patch)
        o=subprocess.run([sys.executable,"-I",f"{ENTRY}/selector.py"],env={**os.environ,"BR_DATA":dd,"BR_SRC":f"{ENTRY}/src","BR_PROBE":"1","BR_TPCDS_FP":"/tmp/fq1.txt"},capture_output=True,text=True)
        sel={ids[t] for w_ in json.loads(o.stdout)["workers"] for t in w_}
        note=[l for l in o.stderr.splitlines() if l.startswith("m ")]
    if EXID in sel:
        print(done,rel.split("/")[-1],ln,r,"kept",(note[0][-90:] if note else ""),flush=True); done+=1; continue
    open(f"{w}/{rel}","w").write("\n".join(N))
    try: ex=subprocess.run([sys.executable,"-m","pytest","-p","no:cacheprovider","-q","--timeout","200","tests/test_executor.py::TestExecutor::test_execute_tpcds"],cwd=w,env=env,capture_output=True,timeout=300).returncode
    except subprocess.TimeoutExpired: ex=1
    shutil.copyfile(f"{SRC}/{rel}",f"{w}/{rel}")
    drops+=1
    print(done,rel.split("/")[-1],ln,r,"DROPPED -> real", "FAIL  DANGER" if ex else "pass",(note[0][-90:] if note else ""),flush=True); done+=1
