# python-base #################################################################

FROM python:3.13-slim AS python-base

LABEL artifact=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYSETUP_PATH="/opt/pysetup" \
    VENV_PATH="/opt/pysetup/.venv" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT="/opt/pysetup/.venv"

# builder-base ################################################################

FROM python-base AS builder-base

ENV PATH="$VENV_PATH/bin:$PATH"

LABEL artifact=false

WORKDIR ${PYSETUP_PATH}

RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv build --wheel

RUN python -m venv /opt/deps && \
    uv pip install --python /opt/deps/bin/python dist/*.whl

# validate ####################################################################

FROM builder-base AS validate

LABEL artifact=false

COPY tests ./tests
COPY apps/postal-station ./apps/postal-station
COPY examples ./examples
COPY db ./db
COPY k8s ./k8s
COPY .dev/conf/signature.json ./.dev/conf/signature.json
COPY .dev/conf/timestamp-providers.json ./.dev/conf/timestamp-providers.json
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups && \
    uv run --frozen black --check --diff src/xarta tests && \
    uv run --frozen ruff check src/xarta tests && \
    uv run --frozen mypy && \
    uv run --frozen pytest && \
    uv run --frozen bandit -q -lll -r src/xarta

# production ##################################################################

FROM python-base AS production

LABEL artifact=true

ARG UID=1001
ARG GID=1001

RUN groupadd -g "${GID}" xarta && \
    useradd -d /app --no-log-init -u "${UID}" -g "${GID}" xarta

USER xarta
WORKDIR /app

COPY --from=builder-base --chown=xarta:xarta /opt/deps /opt/deps
COPY --chown=xarta:xarta ./docker/core/entrypoint.sh ./entrypoint.sh

# Unset environment variables required by the builder and validation process.
ENV PATH="/opt/deps/bin:$PATH" \
    PYSETUP_PATH= \
    VENV_PATH= \
    UV_PROJECT_ENVIRONMENT=

RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
