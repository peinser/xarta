#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
CHART="$ROOT/k8s/helm/charts/core"
VALUES="$ROOT/k8s/helm/ci/core-values.yaml"
RELEASE=peppol-test

generated=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-peppol.yaml \
  --set services.peppol.enabled=true \
  --set services.peppol.settings.config.ci.adapter=e-invoice-be-rest)
grep -Eq 'checksum-peppol-config: "[0-9a-f]{64}"' <<<"$generated"

external=$(helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --show-only templates/deployments/service-peppol.yaml \
  --set services.peppol.enabled=true \
  --set services.peppol.existingSecret=peppol-config-v1 \
  --set services.peppol.existingSecretChecksum=rotated-v2)
grep -Fq 'checksum-peppol-config: "rotated-v2"' <<<"$external"
grep -Fq 'secretName: peppol-config-v1' <<<"$external"

printf 'Peppol Helm configuration tests passed\n'
