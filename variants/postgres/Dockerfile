# Build from repo root: docker build -t app-postgres -f variants/postgres/Dockerfile .
#
# BASE_IMAGE is the runtime base. Default is python:3.12-slim (no auth,
# anyone can build). Operators can override at build time:
#   --build-arg BASE_IMAGE=gcr.io/distroless/python3-debian12  (free hardened)
#   --build-arg BASE_IMAGE=dhi/python:3.12                      (paid hardened, DHI)
# The build stage stays on python:3.12-slim (needs pip + shell); only
# the runtime stage swaps. Site-packages copied across, PYTHONPATH
# wired up. Entrypoint is entrypoint.py so shell-less runtimes
# (distroless) work. (#169)

# Global ARG must be declared before any FROM that references it; per-stage
# ARGs declared between FROMs don't substitute into a subsequent FROM.
ARG BASE_IMAGE=python:3.12-slim

FROM python:3.12-slim AS build
WORKDIR /build
COPY core/ /build/core/
COPY variants/postgres/requirements.txt /build/
# All optional core extras at build time so the published image
# supports every feature via env var alone — no rebuild required to
# enable metrics / tracing / external config sources.
RUN pip install --no-cache-dir --target=/install \
        '/build/core[metrics,tracing,config-git,config-db]' \
 && pip install --no-cache-dir --target=/install -r /build/requirements.txt

FROM ${BASE_IMAGE} AS runtime
WORKDIR /app
COPY --from=build /install /opt/deps
ENV PYTHONPATH=/opt/deps
COPY variants/postgres/app/ /app/app/
COPY scripts/entrypoint.py /app/entrypoint.py
CMD ["python3", "/app/entrypoint.py"]
