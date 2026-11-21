# The scorer builds this on linux/amd64 and runs it with no network, as user nobody, read-only.
# Same base image the organisers labelled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot's test dependencies, exactly as labelled (PyPI only). The probe imports test modules.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# Pinned sqlglot sources and test fixtures (eb11aec, MIT) are vendored in src/. Maps in maps/
# were built from sqlglot's own tests at eb11aec (see build/). Nothing else is downloaded.
COPY selector.py probe_runner.py probe_runner_lib.py probe_server.py maxnodes_small.py genmem_check.py /app/
COPY maps /app/maps
COPY src /app/src
RUN python -m compileall -q /app/src /app/probe_runner_lib.py && chmod -R a+rX /app

ENV BR_SRC=/app/src BR_MAPS=/app/maps/maps.json.gz BR_PROBE=1 BR_DIAG_DROP_HEAVY=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-I", "/app/selector.py"]
