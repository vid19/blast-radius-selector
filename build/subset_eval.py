import json, os, random, re, shutil, subprocess, sys, tempfile, importlib.util
spec=importlib.util.spec_from_file_location("sel","/home/claude/entry/selector.py"); S=importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
rel="tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"; text=open("/home/claude/sqlglot/"+rel).read()
pairs=S.Analyzer._fixture_pairs(text); ex=[i for i,p in enumerate(pairs) if p["meta"].get("execute")]
OPS=[(r"\b(\d+)\b",lambda m:str(int(m.group(1))+1)),(r" > "," >= "),(r" < "," <= "),(r"\bASC\b","DESC"),(r"\bAND\b","OR"),(r"\bSUM\(","MAX("),(r"\bLIMIT 100\b","LIMIT 10"),(r"\bDISTINCT ",""),(r"'([A-Z][a-z]+)'","'\\1x'")]
rnd=random.Random(int(sys.argv[1])); n=int(sys.argv[2])
w=tempfile.mkdtemp()+"/repo"; shutil.copytree("/home/claude/sqlglot",w,ignore=shutil.ignore_patterns(".git","__pycache__",".coverage*"))
env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1","SKIP_INTEGRATION":"0","PYTHONPATH":w}
done=0
while done<n:
    i=rnd.choice(ex); q=pairs[i]["sql"]; p,f=rnd.choice(OPS); ms=list(re.finditer(p,q))
    if not ms: continue
    m=rnd.choice(ms); nq=q[:m.start()]+re.sub(p,f,m.group(0),count=1)+q[m.end():]
    if nq==q or text.count(q)!=1: continue
    new=text.replace(q,nq,1); open("/tmp/se.sql","w").write(new)
    try: sub=subprocess.run([sys.executable,"/home/claude/entry/tpcds_subset.py","/home/claude/entry/src","/home/claude/entry/src","/tmp/se.sql",str(i)],env=env,capture_output=True,timeout=15).returncode
    except subprocess.TimeoutExpired: sub=9
    open(f"{w}/{rel}","w").write(new)
    try: real=subprocess.run([sys.executable,"-m","pytest","-p","no:cacheprovider","-q","--timeout","120","tests/test_executor.py::TestExecutor::test_execute_tpcds"],cwd=w,env=env,capture_output=True,timeout=200).returncode
    except subprocess.TimeoutExpired: real=1
    shutil.copyfile("/home/claude/sqlglot/"+rel,f"{w}/{rel}")
    s="pass" if sub==0 else "FAIL"; r="pass" if real==0 else "FAIL"
    print(done,"query",i,repr(m.group(0)),"->",repr(re.sub(p,f,m.group(0),count=1)),"replay",s,"real",r,"DANGER" if (s=="pass" and r=="FAIL") else ("agree" if s==r else "replay stricter"),flush=True)
    done+=1
