#!/usr/bin/env bash
# Rebuild maps/maps.json.gz and src/ from sqlglot at the pinned commit. Run on linux/amd64
# (or inside the python:3.13-slim image) so timings, wheels and behaviour match the scorer.
# Takes ~6 minutes on 2 cores. Output is committed; the Docker build never runs this.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; ENTRY="$(dirname "$HERE")"
WORK="${WORK:-/tmp/br_build}"; PIN=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
export SQLGLOT="$WORK/sqlglot" RAW="$WORK/raw"
mkdir -p "$WORK" "$RAW"
if [ ! -d "$SQLGLOT/.git" ]; then
  git init -q "$SQLGLOT" && git -C "$SQLGLOT" fetch -q --depth 1 https://github.com/tobymao/sqlglot.git "$PIN" && git -C "$SQLGLOT" checkout -q FETCH_HEAD
fi
python3 -m venv "$WORK/venv"; . "$WORK/venv/bin/activate"
pip install -q -r "$ENTRY/requirements.lock" coverage pytest-cov
cd "$SQLGLOT"; rm -f .coverage*
export SKIP_INTEGRATION=0 PYTHONPATH="$SQLGLOT:$HERE" TRACE_ROOT="$SQLGLOT" TRACE_OUT="$RAW/t2"
printf '[run]\nsource = sqlglot\n' > "$WORK/covrc"
# 1. every test, process pools run inline, with per-test coverage contexts + file/class traces
python -m pytest tests -q -p no:cacheprovider -p traceplug2 -n 2 --cov --cov-config="$WORK/covrc" --cov-context=test --cov-report= | tail -2
# 2. lines that run while modules are imported
cat > "$WORK/import_all.py" <<'PY'
import pkgutil, importlib, sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception: pass
PY
coverage run --rcfile="$WORK/covrc" --data-file="$RAW/.cov_import" "$WORK/import_all.py"
python - <<PY
from coverage import CoverageData; import json
cd = CoverageData("$RAW/.cov_import"); cd.read()
json.dump({f.replace("$SQLGLOT/", ""): sorted(cd.lines(f)) for f in cd.measured_files()}, open("$RAW/import_lines.json", "w"))
PY
# 3. modules each test file imports at collection
python - <<PY
import json, subprocess, glob, sys
out = {}
for f in sorted(glob.glob("tests/test_*.py") + glob.glob("tests/dialects/test_*.py")):
    code = "import sys; import %s; print('\\\\n'.join(sorted(m for m in sys.modules if m.startswith(('sqlglot','tests')))))" % f[:-3].replace("/", ".")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    out[f] = r.stdout.split() if r.returncode == 0 else ["ERROR"]
json.dump(out, open("$RAW/collect_imports.json", "w"))
PY
# 4. vendored sources + fixtures, measured probe costs, compact maps
rm -rf "$ENTRY/src"; mkdir -p "$ENTRY/src"
git ls-files 'sqlglot/*.py' 'sqlglot/**/*.py' 'tests/*.py' 'tests/**/*.py' 'tests/fixtures' | sort -u | tar cf - -T - | tar xf - -C "$ENTRY/src"
cp LICENSE "$ENTRY/src/LICENSE-sqlglot"
python "$HERE/probe_costs.py" "$ENTRY/src" "$RAW/probe_costs.json" "$ENTRY/sample/tests.json" 2>/dev/null
python "$HERE/import_consumers.py" "$SQLGLOT" "$RAW/import_consumers.json"
python "$HERE/make_maps.py" "$ENTRY/sample/tests.json" "$ENTRY/maps/maps.json.gz"
echo "maps and src rebuilt"
