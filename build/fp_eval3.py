import ast, difflib, gzip, json, os, random, re, shutil, subprocess, sys, tempfile
SRC="/home/claude/sqlglot"; ENTRY="/home/claude/entry"
OPS=[(r"\band\b","or"),(r"\bor\b","and"),(r"==","!="),(r"!=","=="),(r" is not "," is "),(r"\bTrue\b","False"),(r"\bFalse\b","True"),(r"\bif not ","if "),(r"(?<![\w.])1(?![\w.])","2"),(r"(?<![\w.])0(?![\w.])","1"),(r"\breturn ","return None or "),(r'"([a-z_]+)"','"\\1x"')]
M=json.loads(gzip.open(f"{ENTRY}/maps/maps.json.gz").read()); EX=M["ids"].index("tests/test_executor.py::TestExecutor::test_execute_tpcds")
RT={k:set(v) for k,v in M["ex_runtime_lines"].items()}
pool=[]
for rel,d in M["lines"].items():
    if rel.startswith(("sqlglot/executor/","sqlglot/generators/python.py")): continue
    text=open(f"{SRC}/{rel}").read(); L=text.split("\n"); tree=ast.parse(text)
    il=set(M["import_lines"].get(rel,[]))
    spans=[(n.body[0].lineno,n.end_lineno) for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
    rt=RT.get(rel,set())
    for ln,s in d.items():
        ln=int(ln)
        if EX not in M["sets"][s] or ln in il: continue
        inner=[sp for sp in spans if sp[0]<=ln<=sp[1]]
        if not inner: continue
        a,b=max(inner)  # innermost (latest start)
        if rt & set(range(a,b+1)): continue   # selector would refuse: function runs during execution
        st=L[ln-1].strip()
        if not st or st.startswith(("#","def ","class ","@")): continue
        for p,r in OPS:
            if re.search(p,L[ln-1]): pool.append((rel,ln,p,r))
rnd=random.Random(int(sys.argv[1])); n=int(sys.argv[2])
w=tempfile.mkdtemp()+"/repo"; shutil.copytree(SRC,w,ignore=shutil.ignore_patterns(".git","__pycache__",".coverage*"))
env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","SKIP_INTEGRATION":"0","PYTHONPATH":w}
files=sorted(set(p[0] for p in pool)); print("pool",len(pool),"files",len(files),flush=True)
done=0
while done<n:
    rel=rnd.choice([f for f in files if f.startswith(("sqlglot/expressions/","sqlglot/serde","sqlglot/generator.py","sqlglot/parser.py","sqlglot/transforms","sqlglot/tokens","sqlglot/helper","sqlglot/dialects/dialect"))] or files); cands=[p for p in pool if p[0]==rel]; rel,ln,p,r=rnd.choice(cands)
    L=open(f"{SRC}/{rel}").read().split("\n"); m=rnd.choice(list(re.finditer(p,L[ln-1]))); N=L[:]; N[ln-1]=L[ln-1][:m.start()]+r+L[ln-1][m.end():]
    try: ast.parse("\n".join(N))
    except SyntaxError: continue
    open(f"{w}/{rel}","w").write("\n".join(N))
    try: fp=subprocess.run([sys.executable,f"{ENTRY}/tpcds_fingerprint.py",w,f"{ENTRY}/src","/tmp/fq1.txt"],env=env,capture_output=True,timeout=60).returncode
    except subprocess.TimeoutExpired: fp=9
    if fp==0:
        try: ex=subprocess.run([sys.executable,"-m","pytest","-p","no:cacheprovider","-q","--timeout","300","tests/test_executor.py::TestExecutor::test_execute_tpcds"],cwd=w,env=env,capture_output=True,text=True,timeout=400).returncode
        except subprocess.TimeoutExpired: ex=1
        res="FAIL" if ex else "pass"
    else: res="(not needed)"
    shutil.copyfile(f"{SRC}/{rel}",f"{w}/{rel}")
    v="same" if fp==0 else "CHANGED"
    print(done,rel.split("/",1)[1],ln,r,"fingerprint",v,"executor",res,"DANGER" if (fp==0 and res=="FAIL") else "",flush=True)
    open("/home/claude/harness/fp_eval3.jsonl","a").write(json.dumps({"rel":rel,"line":ln,"op":r,"fp":v,"ex":res})+"\n")
    done+=1
