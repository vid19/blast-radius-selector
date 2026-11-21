import ast, difflib, gzip, json, os, random, re, shutil, subprocess, sys, tempfile
SRC="/home/claude/sqlglot"; ENTRY="/home/claude/entry"
OPS=[(r"\band\b","or"),(r"\bor\b","and"),(r"==","!="),(r"!=","=="),(r" is not "," is "),(r"\bTrue\b","False"),(r"\bFalse\b","True"),(r"\bif not ","if "),(r"(?<![\w.])1(?![\w.])","2"),(r"(?<![\w.])0(?![\w.])","1")]
tests=json.load(open("/home/claude/kit/sample/tests.json"))["tests"]; ids=[t["id"] for t in tests]; ms={t["id"]:t["seconds"] for t in tests}
M=json.loads(gzip.open(f"{ENTRY}/maps/maps.json.gz").read())
pool=[]
for rel in ["sqlglot/parser.py","sqlglot/expressions/core.py"]:
    il=set(M["import_lines"].get(rel,[])); L=open(f"{SRC}/{rel}").read().split("\n"); t=ast.parse("\n".join(L))
    body=set()
    for n in ast.walk(t):
        if isinstance(n,ast.FunctionDef): body|=set(range(n.body[0].lineno,n.end_lineno+1))
    for ln in sorted(il & body):
        st=L[ln-1].strip()
        if not st or st.startswith(("#","def ","@")): continue
        for p,r in OPS:
            if re.search(p,L[ln-1]): pool.append((rel,ln,p,r))
rnd=random.Random(int(sys.argv[1])); full=sys.argv[3]=="full"
repo=tempfile.mkdtemp()+"/r"; shutil.copytree(SRC,repo,ignore=shutil.ignore_patterns(".git","__pycache__",".coverage*"))
print("pool",len(pool),flush=True); done=0
def select(patch, on):
    with tempfile.TemporaryDirectory() as dd:
        os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json",dd); open(f"{dd}/commits/m.patch","w").write(patch)
        o=subprocess.run([sys.executable,"-I",f"{ENTRY}/selector.py"],env={**os.environ,"BR_DATA":dd,"BR_SRC":f"{ENTRY}/src","BR_PROBE":"0","BR_IMPORT_CONSUMERS":on},capture_output=True,text=True)
        d=json.loads(o.stdout); return {ids[t] for w in d["workers"] for t in w}
import importlib.util
spec=importlib.util.spec_from_file_location("sel",f"{ENTRY}/selector.py"); S=importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
num={t["id"]:t["number"] for t in tests}; msn={t["number"]:round(t["seconds"]*1000) for t in tests}
while done<int(sys.argv[2]):
    rel,ln,p,r=rnd.choice(pool); L=open(f"{SRC}/{rel}").read().split("\n")
    m=rnd.choice(list(re.finditer(p,L[ln-1]))); N=L[:]; N[ln-1]=L[ln-1][:m.start()]+r+L[ln-1][m.end():]
    try: ast.parse("\n".join(N))
    except SyntaxError: continue
    patch=f"diff --git a/{rel} b/{rel}\n"+"".join(difflib.unified_diff([x+"\n" for x in L],[x+"\n" for x in N],f"a/{rel}",f"b/{rel}"))
    old=select(patch,"0"); new=select(patch,"1")
    co=S.schedule({num[i] for i in old},msn)[1]/1000; cn=S.schedule({num[i] for i in new},msn)[1]/1000
    res=""
    if full:
        orig=open(f"{repo}/{rel}").read(); open(f"{repo}/{rel}","w").write("\n".join(N))
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([sys.executable,"-m","pytest","-p","no:cacheprovider","-q","-n","2","--timeout","180",f"--junitxml={td}/r.xml","tests"],cwd=repo,env={**os.environ,"SKIP_INTEGRATION":"0","PYTHONPATH":repo},capture_output=True,timeout=1500)
            import xml.etree.ElementTree as ET
            fail=set()
            for tc in ET.parse(f"{td}/r.xml").iter("testcase"):
                if tc.find("failure") is not None or tc.find("error") is not None:
                    cls=tc.get("classname","")
                    if "." not in cls: fail.add("COLLECT"); continue
                    fail.add(f"{cls.rsplit('.',1)[0].replace('.','/')}.py::{cls.rsplit('.',1)[1]}::{tc.get('name').split('[')[0]}")
        open(f"{repo}/{rel}","w").write(orig)
        miss=sorted(fail-new); res=f"fail {len(fail)} {'MISS '+str(miss[:2]) if miss else 'ok'}"
    print(done,rel.split('/')[-1],ln,r,"old",len(old),co,"new",len(new),cn,res,flush=True); done+=1
