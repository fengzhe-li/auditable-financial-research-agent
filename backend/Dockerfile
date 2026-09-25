# Phase 8: backend container. No PostgreSQL/Redis/Qdrant - the running
# system is exactly what Phases 1-7 already are: a Python package backed by
# a single on-disk SQLite file (see docs/ROADMAP.md's Phase 1 deviation
# entry for why SQLite, not PostgreSQL, is this project's actual data
# layer). This image serves the read/review API afra.api.app defines
# (Phase 7) - nothing here changes what that API does.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first for better layer caching.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[api]"

COPY scripts ./scripts
COPY benchmark ./benchmark
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV AFRA_DB_PATH=/data/afra_demo.db \
    AFRA_API_HOST=0.0.0.0 \
    AFRA_API_PORT=8000
RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
