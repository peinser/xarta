#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
CHART="$ROOT/k8s/helm/charts/core"
VALUES="$ROOT/k8s/helm/ci/core-values.yaml"
RELEASE=engines-test

assert_contains() {
  local value=$1
  local expected=$2
  if ! grep -Fq -- "$expected" <<<"$value"; then
    printf 'Expected rendered manifest to contain: %s\n' "$expected" >&2
    exit 1
  fi
}

configmap=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/config-maps/template-engines.yaml)
assert_contains "$configmap" '"endpoint": "http://engines-test-template-engine-jinja"'
assert_contains "$configmap" '"kind": "gotenberg"'
assert_contains "$configmap" '"endpoint": "http://engines-test-template-engine-gotenberg:3000"'

transform=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-transform.yaml \
  --set services.transform.enabled=true \
  --set services.transform.settings.endpoint=http://gotenberg.internal:3000 \
  --set services.transform.settings.timeout=17 \
  --set services.transform.settings.maxBytes=1048576 \
  --set services.transform.settings.nats.workers=4)
assert_contains "$transform" '"--run", "transform=v1"'
assert_contains "$transform" 'name: TRANSFORM_GOTENBERG_ENDPOINT'
assert_contains "$transform" 'value: "http://gotenberg.internal:3000"'
assert_contains "$transform" 'name: TRANSFORM_GOTENBERG_TIMEOUT'
assert_contains "$transform" 'value: "17"'
assert_contains "$transform" 'name: TRANSFORM_MAX_BYTES'
assert_contains "$transform" 'value: "1048576"'
assert_contains "$transform" 'name: NATS_WORKERS'
assert_contains "$transform" 'value: "4"'
assert_contains "$transform" 'name: TMP_STORAGE'
if grep -Fq 'POSTGRES' <<<"$transform"; then
  echo "Transform deployment must not depend on PostgreSQL" >&2
  exit 1
fi

intake=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-intake.yaml \
  --set services.transform.enabled=true)
assert_contains "$intake" 'transform'

without_transform=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-intake.yaml)
if grep -Fq '\"transform\"' <<<"$without_transform"; then
  echo "Intake must not advertise transform when disabled" >&2
  exit 1
fi

printf 'Template engine and transform Helm configuration tests passed\n'
