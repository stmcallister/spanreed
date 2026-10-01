# One image, three entrypoints. The command is always `python -m newsbrief` and
# the argument picks the workload: collect, compose, or web.
#
# Two stages so the runtime image carries no pip, no wheels, and no build
# cache. Everything the application needs lives in one virtualenv that is
# copied across.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# A source edit invalidates this layer and reinstalls the dependencies. The
# dependency list is short and the wheels are all prebuilt, so buying back that
# rebuild would cost a lock file and a second install step for a few seconds.
COPY pyproject.toml ./
COPY src/ ./src/

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install .


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DB_PATH=/data/newsbrief.db

# Non-root, with a fixed uid so the PVC's fsGroup can be set to match.
RUN groupadd --gid 1000 newsbrief \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin newsbrief \
    && mkdir -p /data \
    && chown 1000:1000 /data

COPY --from=builder --chown=root:root /opt/venv /opt/venv

# Bootstrap configuration for a database that has none yet. Read once, on a
# first start, and ignored forever after that.
COPY --chown=root:root seed/ /app/seed/

USER 1000:1000
WORKDIR /app
EXPOSE 8000

# No default argument. Every workload states which one it is, so a manifest
# that forgets to say fails loudly instead of quietly serving the wrong thing.
ENTRYPOINT ["python", "-m", "newsbrief"]
