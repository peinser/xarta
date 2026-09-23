#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
CHART="$ROOT/k8s/helm/charts/core"
VALUES="$ROOT/k8s/helm/ci/core-values.yaml"
RELEASE=telemetry-test
ENDPOINT=http://otel-collector.observability:4318

disabled=$(helm template "$RELEASE" "$CHART" -f "$VALUES")
if grep -Fq 'OTEL_EXPORTER_OTLP_ENDPOINT' <<<"$disabled"; then
  printf 'Tracing environment rendered while telemetry was disabled\n' >&2
  exit 1
fi

enabled=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set telemetry.enabled=true --set telemetry.endpoint="$ENDPOINT")

for template in "$CHART"/templates/deployments/service-*.yaml; do
  if ! grep -Fq 'include "telemetry.env"' "$template"; then
    printf 'Missing telemetry environment in %s\n' "$(basename "$template")" >&2
    exit 1
  fi
done

endpoints=$(grep -Fc "value: \"$ENDPOINT\"" <<<"$enabled")
service_names=$(grep -Fc 'value: "xarta-' <<<"$enabled")
samplers=$(grep -Fc 'value: "always_on"' <<<"$enabled")
if [[ $endpoints -eq 0 || $endpoints -ne $service_names || $endpoints -ne $samplers ]]; then
  printf 'Rendered telemetry endpoint/service-name/sampler counts differ: %s/%s/%s\n' \
    "$endpoints" "$service_names" "$samplers" >&2
  exit 1
fi

for name in archive bundle doccle document-type email generate intake mcp peppol \
  postal preview qr render render-preview search sftp signature webhook; do
  template="$CHART/templates/deployments/service-$name.yaml"
  if ! grep -Fq "\"name\" \"$name\"" "$template"; then
    printf 'Missing telemetry service name xarta-%s in %s\n' "$name" "$template" >&2
    exit 1
  fi
done

if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set telemetry.enabled=true >/dev/null 2>&1; then
  printf 'Telemetry rendered without its required endpoint\n' >&2
  exit 1
fi

printf 'Telemetry Helm tests passed\n'
