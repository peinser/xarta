#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
CHART="$ROOT/k8s/helm/charts/core"
VALUES="$ROOT/k8s/helm/ci/core-values.yaml"
RELEASE=preview-pool-test

render_template() {
  local template=$1
  shift
  helm template "$RELEASE" "$CHART" -f "$VALUES" \
    --show-only "$template" "$@"
}

assert_contains() {
  local value=$1
  local expected=$2
  if ! grep -Fq -- "$expected" <<<"$value"; then
    printf 'Expected rendered manifest to contain: %s\n' "$expected" >&2
    exit 1
  fi
}

assert_not_contains() {
  local value=$1
  local unexpected=$2
  if grep -Fq -- "$unexpected" <<<"$value"; then
    printf 'Rendered manifest unexpectedly contains: %s\n' "$unexpected" >&2
    exit 1
  fi
}

first_image() {
  awk '/^[[:space:]]*- image: / { print $3; exit }' <<<"$1"
}

checksum() {
  awk '/checksum-template-engines-config:/ { print $2; exit }' <<<"$1"
}

# Default topology: no extra pool and no routing changes.
normal_render=$(render_template templates/deployments/service-render.yaml)
default_preview=$(render_template templates/deployments/service-preview.yaml)
default_generate=$(render_template templates/deployments/service-generate.yaml)
default_all=$(helm template "$RELEASE" "$CHART" -f "$VALUES")

assert_not_contains "$default_all" "$RELEASE-service-render-preview"
assert_contains "$default_preview" \
  "value: http://$RELEASE-service-render/api/render"
assert_contains "$default_generate" \
  "value: http://$RELEASE-service-render/api/render"
assert_contains "$normal_render" "app.kubernetes.io/name: xarta"
assert_contains "$normal_render" "app.kubernetes.io/component: render"
assert_contains "$normal_render" "xarta.peinser.com/render-pool: default"

# Enabled topology: Preview alone moves to an independently scalable Render pool.
enabled=(--set services.render.previewPool.enabled=true \
  --set services.render.previewPool.replicas=2)
preview_render=$(render_template \
  templates/deployments/service-render-preview.yaml "${enabled[@]}")
preview_service=$(render_template templates/services/render-preview.yaml "${enabled[@]}")
enabled_preview=$(render_template templates/deployments/service-preview.yaml "${enabled[@]}")
enabled_generate=$(render_template templates/deployments/service-generate.yaml "${enabled[@]}")

assert_contains "$preview_render" "name: $RELEASE-service-render-preview"
assert_contains "$preview_render" "replicas: 2"
assert_contains "$preview_render" "app.kubernetes.io/component: render"
assert_contains "$preview_render" "xarta.peinser.com/render-pool: preview"
assert_contains "$preview_render" '"--run", "render=latest"'
assert_contains "$preview_render" '"--run", "render=v1"'
assert_contains "$preview_service" "name: $RELEASE-service-render-preview"
assert_contains "$preview_service" "app: $RELEASE-service-render-preview"
assert_contains "$enabled_preview" \
  "value: http://$RELEASE-service-render-preview/api/render"
assert_contains "$enabled_generate" \
  "value: http://$RELEASE-service-render/api/render"
assert_not_contains "$enabled_generate" "$RELEASE-service-render-preview"

# Both pools consume the same ConfigMap and rollout checksum.
assert_contains "$normal_render" "name: $RELEASE-service-render"
assert_contains "$preview_render" "name: $RELEASE-service-render"
if [[ "$(checksum "$normal_render")" != "$(checksum "$preview_render")" ]]; then
  printf 'Render pools do not share the template-engine checksum\n' >&2
  exit 1
fi

# Global image configuration is the only image source for either pool.
tag_args=("${enabled[@]}" --set image.registry=registry.example \
  --set image.repository=xarta/core --set image.tag=release-123)
tag_normal=$(render_template templates/deployments/service-render.yaml "${tag_args[@]}")
tag_preview=$(render_template \
  templates/deployments/service-render-preview.yaml "${tag_args[@]}")
if [[ "$(first_image "$tag_normal")" != "$(first_image "$tag_preview")" ]]; then
  printf 'Render pools resolved different tagged images\n' >&2
  exit 1
fi

digest_args=("${enabled[@]}" --set image.registry=registry.example \
  --set image.repository=xarta/core --set image.digest=sha256:abc123)
digest_normal=$(render_template \
  templates/deployments/service-render.yaml "${digest_args[@]}")
digest_preview=$(render_template \
  templates/deployments/service-render-preview.yaml "${digest_args[@]}")
if [[ "$(first_image "$digest_normal")" != "$(first_image "$digest_preview")" ]]; then
  printf 'Render pools resolved different digest images\n' >&2
  exit 1
fi

# A declarative template revision rolls both pools.
revision_args=("${enabled[@]}" --set services.render.templateRevision=abc123)
revision_normal=$(render_template \
  templates/deployments/service-render.yaml "${revision_args[@]}")
revision_preview=$(render_template \
  templates/deployments/service-render-preview.yaml "${revision_args[@]}")
assert_contains "$revision_normal" \
  'xarta.peinser.com/template-revision: "abc123"'
assert_contains "$revision_preview" \
  'xarta.peinser.com/template-revision: "abc123"'

# Reserved capacity without either dependency is a configuration error.
if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.render.enabled=false \
  --set services.render.previewPool.enabled=true >/tmp/xarta-invalid-render.log 2>&1; then
  printf 'Expected disabled Render with previewPool to fail\n' >&2
  exit 1
fi
assert_contains "$(< /tmp/xarta-invalid-render.log)" \
  "services.render.previewPool requires services.render.enabled=true"

if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.preview.enabled=false \
  --set services.render.previewPool.enabled=true >/tmp/xarta-invalid-preview.log 2>&1; then
  printf 'Expected disabled Preview with previewPool to fail\n' >&2
  exit 1
fi
assert_contains "$(< /tmp/xarta-invalid-preview.log)" \
  "services.render.previewPool requires services.preview.enabled=true"

if helm template "$RELEASE" "$CHART" -f "$VALUES" \
  --set services.render.previewPool.enabled=true \
  --set services.render.previewPool.replicas=0 >/tmp/xarta-invalid-replicas.log 2>&1; then
  printf 'Expected a zero-sized Preview Render pool to fail\n' >&2
  exit 1
fi
assert_contains "$(< /tmp/xarta-invalid-replicas.log)" \
  "services.render.previewPool.replicas must be greater than zero"

printf 'Preview Render pool Helm topology tests passed\n'
