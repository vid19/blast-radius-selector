"""For a patch: selector candidates, split heavy (>=3 s) vs cheap; run cheap with the probe
runner (all, no failfast) on a patched copy; run heavy with real pytest. Emits a JSON record."""
import json, os, shutil, subprocess, sys, tempfile, time, resource
SRC = "/home/claude/sqlglot"; ENTRY = "/home/claude/entry"
tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
ids = [t["id"] for t in tests]; secs = {t["id"]: t["seconds"] for t in tests}
HEAVY = 3.0
def cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN); return r.ru_utime + r.ru_stime
def evaluate(patch, run_heavy=True):
    with tempfile.TemporaryDirectory() as dd:
        os.makedirs(f"{dd}/commits"); shutil.copy("/home/claude/kit/sample/tests.json", dd)
        open(f"{dd}/commits/m.patch", "w").write(patch)
        r = subprocess.run([sys.executable, "-I", f"{ENTRY}/selector.py"], env={**os.environ, "BR_DATA": dd, "BR_SRC": f"{ENTRY}/src", "BR_PROBE": "1", "BR_PROBE_REPORT": f"{dd}/rep.json"}, capture_output=True, text=True)
        sel = [ids[t] for w in json.loads(r.stdout)["workers"] for t in w]
        rep = json.load(open(f"{dd}/rep.json")).get("m") if os.path.exists(f"{dd}/rep.json") else None
    heavy = [t for t in sel if secs[t] >= HEAVY]; cheap = sorted((t for t in sel if secs[t] < HEAVY), key=lambda t: secs[t])
    rec = {"sel": len(sel), "heavy": heavy, "cheap_n": len(cheap), "cheap_secs": round(sum(secs[t] for t in cheap), 2), "probe": rep["probe"] if rep else None}
    if not heavy:
        return rec
    if rep:
        cheap = rep["probe"]
    work = tempfile.mkdtemp()
    shutil.copytree(f"{SRC}/sqlglot", f"{work}/p/sqlglot", ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=f"{work}/p", check=False)
    # git apply needs the path prefix sqlglot/..., which exists under p/
    c0 = cpu(); t0 = time.time()
    try:
        if True:
            raise subprocess.TimeoutExpired("skip", 0)
        p = subprocess.run([sys.executable, f"{ENTRY}/probe_runner.py", f"{work}/p", SRC, "--all"], input=json.dumps(cheap), capture_output=True, text=True, timeout=120)
        rec["probe_fail"] = json.loads(p.stdout or "[]") if p.returncode == 0 else ["CRASH"]
    except subprocess.TimeoutExpired:
        rec["probe_fail"] = ["TIMEOUT"]
    rec["probe_cpu"] = round(cpu() - c0, 2); rec["probe_wall"] = round(time.time() - t0, 2)
    if run_heavy:
        # real pytest for heavy + cheap on a full patched repo copy
        repo = f"{work}/repo"; shutil.copytree(SRC, repo, ignore=shutil.ignore_patterns(".git", "__pycache__", ".coverage*"))
        subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=repo, check=False)
        xml = f"{work}/r.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-n", "2", "--timeout", "180", f"--junitxml={xml}"] + heavy + cheap,
                       cwd=repo, env={**os.environ, "SKIP_INTEGRATION": "0", "PYTHONPATH": repo}, capture_output=True, timeout=1800)
        import xml.etree.ElementTree as ET
        failed = []
        if os.path.exists(xml):
            for tc in ET.parse(xml).iter("testcase"):
                if tc.find("failure") is not None or tc.find("error") is not None:
                    cls = tc.get("classname", ""); path = cls.rsplit(".", 1)[0].replace(".", "/") + ".py"
                    failed.append(f"{path}::{cls.rsplit('.', 1)[1]}::{tc.get('name').split('[')[0]}")
        rec["true_fail"] = sorted(set(failed))
    shutil.rmtree(work, ignore_errors=True)
    return rec
if __name__ == "__main__":
    for pid in sys.argv[1:]:
        print(pid, json.dumps(evaluate(open(f"/home/claude/kit/sample/commits/{pid}.patch").read(), run_heavy=False)), flush=True)
