#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
CHART="$ROOT/k8s/helm/charts/core"
VALUES="$ROOT/k8s/helm/ci/core-values.yaml"

manifest=$(helm template ubl-test "$CHART" -f "$VALUES" \
  --set services.ubl.enabled=true \
  --set services.ubl.settings.maxBytes=2048 --set services.ubl.settings.nats.workers=3)
for expected in 'ubl=v1' 'name: UBL_MAX_BYTES' 'value: "2048"'; do
  if ! grep -Fq -- "$expected" <<<"$manifest"; then
    printf 'Missing UBL configuration: %s\n' "$expected" >&2
    exit 1
  fi
done
intake=$(helm template ubl-test "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-intake.yaml \
  --set services.ubl.enabled=true)
grep -Fq '\"ubl\"' <<<"$intake"
disabled=$(helm template ubl-test "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-intake.yaml)
if grep -Fq '\"ubl\"' <<<"$disabled"; then
  printf 'Intake advertised a disabled UBL worker\n' >&2
  exit 1
fi
printf 'UBL Helm configuration tests passed\n'
