FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/ontos

RUN pip install --no-cache-dir "uv==0.9.5"

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY ontos ./ontos

# Install from the lockfile CI tests against, with the extras the Helm
# chart's defaults need (Neo4j storage, OpenFGA authz). Without them a
# default chart install fails at startup with ImportError.
RUN uv sync --frozen --no-dev --no-editable --extra neo4j --extra authz

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/ontos/bin:${PATH}"

# Only the resolved environment ships; uv and build tooling stay behind.
COPY --from=builder /opt/ontos /opt/ontos

RUN useradd --system --uid 1000 --home /home/ontos ontos && \
    mkdir -p /var/lib/ontos/audit-logs && \
    chown -R ontos:ontos /var/lib/ontos
USER ontos

ENV ONTOS_LISTEN_HOST=0.0.0.0 \
    ONTOS_LISTEN_PORT=8765 \
    ONTOS_STORAGE_BACKEND=networkx

EXPOSE 8765

CMD ["python", "-m", "ontos.runtime.server"]
