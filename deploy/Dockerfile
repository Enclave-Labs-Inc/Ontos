FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ontos ./ontos

RUN uv pip install --system --no-cache .

FROM base AS runtime

RUN useradd --system --uid 1000 --home /home/ontos ontos && \
    mkdir -p /var/lib/ontos/audit-logs && \
    chown -R ontos:ontos /var/lib/ontos
USER ontos

ENV ONTOS_LISTEN_HOST=0.0.0.0 \
    ONTOS_LISTEN_PORT=8765 \
    ONTOS_STORAGE_BACKEND=networkx

EXPOSE 8765

CMD ["python", "-m", "ontos.runtime.server"]
