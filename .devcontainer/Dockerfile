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

ENV PATH="$VENV_PATH/bin:$PATH"

# builder-base ################################################################

FROM python-base AS builder-base

LABEL artifact=false

RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential \
    curl

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

WORKDIR ${PYSETUP_PATH}
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups && uv build

# kubectl-base ################################################################

FROM bitnami/kubectl:latest AS kubectl-base

# devcontainer ################################################################

FROM builder-base AS devcontainer

LABEL artifact=false

ARG KUBECONFORM_VERSION=v0.7.0
ARG TARGETARCH

ENV EDITOR=vim

RUN apt-get update && apt-get -y install make git npm nodejs jq vim sqitch libdbd-pg-perl postgresql-client libdbd-sqlite3-perl sqlite3 apache2-utils

RUN curl -sf https://binaries.nats.dev/nats-io/natscli/nats@latest | sh && mv nats .venv/bin/ && nats context add nats --server nats:4222 --description "Development" --select

RUN curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 && chmod 700 get_helm.sh && ./get_helm.sh && rm get_helm.sh

RUN case "${TARGETARCH}" in amd64|arm64) ;; *) echo "Unsupported kubeconform architecture: ${TARGETARCH}" >&2; exit 1 ;; esac && \
    curl -fsSLo /tmp/kubeconform.tar.gz \
      "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${TARGETARCH}.tar.gz" && \
    tar -xzf /tmp/kubeconform.tar.gz -C /tmp kubeconform && \
    install -m 0755 /tmp/kubeconform /usr/local/bin/kubeconform && \
    rm -f /tmp/kubeconform /tmp/kubeconform.tar.gz && \
    kubeconform -v

COPY --from=kubectl-base /opt/bitnami/kubectl/bin/kubectl /usr/local/bin/
