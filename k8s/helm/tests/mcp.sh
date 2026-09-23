#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
CHART="$ROOT/k8s/helm/charts/core"
VALUES="$ROOT/k8s/helm/ci/core-values.yaml"
RELEASE=mcp-test

assert_contains() {
  local value=$1
  local expected=$2
  if ! grep -Fq -- "$expected" <<<"$value"; then
    printf 'Expected rendered manifest to contain: %s\n' "$expected" >&2
    exit 1
  fi
}

hosts=(--set 'defaults.ingress.hosts[0].dns=xarta.example.test' \
  --set 'defaults.ingress.hosts[0].tls=true')
deployment=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-mcp.yaml "${hosts[@]}")
assert_contains "$deployment" 'args: ["mcp"]'
assert_contains "$deployment" "value: http://$RELEASE-service-intake/api/v1/intake"
assert_contains "$deployment" "value: http://$RELEASE-service-documenttype/api/v1/document-type"
assert_contains "$deployment" "value: http://$RELEASE-service-archive/api/v1/archive"
assert_contains "$deployment" 'name: MCP_ARCHIVE_MAX_RESOURCE_BYTES'
assert_contains "$deployment" 'xarta.example.test,xarta.example.test:*"'

ingress=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/ingresses/api-mcp.yaml \
  --set defaults.ingress.enabled=true --set services.mcp.ingress.enabled=true \
  "${hosts[@]}")
assert_contains "$ingress" 'path: /api/mcp'
assert_contains "$ingress" "name: $RELEASE-service-mcp"

if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.intake.enabled=false >/dev/null 2>&1; then
  printf 'MCP rendered without its required intake service\n' >&2
  exit 1
fi

if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.documenttype.enabled=false >/dev/null 2>&1; then
  printf 'MCP rendered without its required document-type service\n' >&2
  exit 1
fi

if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.archive.enabled=false >/dev/null 2>&1; then
  printf 'MCP rendered without its required archive service\n' >&2
  exit 1
fi

helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.mcp.settings.requestTimeoutSeconds=0.5 >/dev/null

printf 'MCP Helm topology tests passed\n'
