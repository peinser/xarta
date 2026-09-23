#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
rendered="$(helm template xarta "$root/k8s/helm/charts/core" -f "$root/k8s/helm/ci/core-values.yaml")"

grep -q 'name: xarta-core-archive-postgresql' <<<"$rendered"
grep -q 'name: ARCHIVE_POSTGRESQL_DATABASE' <<<"$rendered"
grep -q 'name: xarta-core-postgresql' <<<"$rendered"

archive_database="$(awk '/name: xarta-core-archive-postgresql/{found=1} found && /database:/{print $2; exit}' <<<"$rendered")"
[[ "$(base64 --decode <<<"${archive_database//\"/}")" == "xarta_archive" ]]
