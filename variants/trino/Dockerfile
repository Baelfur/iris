# Build from repo root: docker build -t app-trino -f variants/trino/Dockerfile .
# See variants/postgres/Dockerfile for the BASE_IMAGE override pattern. (#169)

ARG BASE_IMAGE=python:3.12-slim

FROM python:3.12-slim AS build
WORKDIR /build
COPY core/ /build/core/
COPY variants/trino/requirements.txt /build/
RUN pip install --no-cache-dir --target=/install \
        '/build/core[metrics,tracing,config-git,config-db]' \
 && pip install --no-cache-dir --target=/install -r /build/requirements.txt

FROM ${BASE_IMAGE} AS runtime
WORKDIR /app
COPY --from=build /install /opt/deps
ENV PYTHONPATH=/opt/deps
COPY variants/trino/app/ /app/app/
COPY scripts/entrypoint.py /app/entrypoint.py
CMD ["python3", "/app/entrypoint.py"]
