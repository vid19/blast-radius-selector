# The scorer builds this on linux/amd64 and runs it with no network, as user nobody, read-only.
# Same base image the organisers labelled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pinned sqlglot sources (eb11aec, MIT) are vendored in src/ so the runtime can rebuild each
# changed file and diff ASTs. Maps in maps/ were built from sqlglot's own tests at eb11aec
# (see build/). Nothing is downloaded during the build.
COPY selector.py /app/selector.py
COPY maps /app/maps
COPY src /app/src
RUN chmod -R a+rX /app && python -m py_compile /app/selector.py

ENV BR_SRC=/app/src BR_MAPS=/app/maps/maps.json.gz PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-I", "/app/selector.py"]
