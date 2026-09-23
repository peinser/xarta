{{/*
Copyright Peinser BV

Generic utilities methods for Helm.
*/}}


{{/*
Returns the NATS server list and checks for its presence.
*/}}
{{- define "nats.servers" -}}
{{- if .Values.nats.servers -}}
{{- .Values.nats.servers | b64enc | quote -}}
{{- else -}}
{{- fail "The `nats.servers` setting has not been defined." -}}
{{- end -}}
{{- end -}}


{{/*
Yields to configured pullSecrets associated with the service. Currently,
the template will only yield the pullSecrets configured in the `defaults`
section of Values.yaml.
*/}}
{{- define "image.pullSecrets" -}}
{{- if $.Values.image.pullSecrets }}
imagePullSecrets:
{{ toYaml $.Values.image.pullSecrets }}
{{ end -}}
{{- end -}}


{{/*
A template returning the name of a service.
*/}}
{{- define "service.name" -}}
{{- printf "%s-service-%s" .release.Name .name -}}
{{- end -}}


{{/*
A template returning the name of a worker.
*/}}
{{- define "worker.name" -}}
{{- printf "%s-worker-%s" .release.Name .name -}}
{{- end -}}


{{/*
A default template for generating services.
*/}}
{{- define "service.svc" -}}
{{- $name := include "service.name" (dict "release" .release "name" .name) -}}
{{- with .service -}}
{{- if .enabled -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}
spec:
  selector:
    app: {{ $name }}
  ports:
  - port: 80
    protocol: TCP
    targetPort: 8000
{{- end -}}
{{- end -}}
{{- end -}}


{{/*
Default annotations for the ingress resource.
*/}}
{{- define "ingress.defaultAnnotations" }}
nginx.archive.ingress.kubernetes.io/proxy-body-size: {{ $.Values.defaults.ingress.maxBodySize | quote }}
{{ if $.Values.defaults.ingress.clusterIssuer -}}
cert-manager.io/cluster-issuer: {{ $.Values.defaults.ingress.clusterIssuer | quote }}
{{- else -}}
{{- fail "defaults.ingress.clusterIssuer cannot be unspecified." -}}
{{- end }}

kubernetes.io/ingress.class: {{ $.Values.defaults.ingress.class | quote }}
{{- if $.Values.defaults.ingress.whitelist }}
nginx.ingress.kubernetes.io/whitelist-source-range: {{ $.Values.defaults.ingress.whitelist }}
{{- end -}}
{{- end -}}


{{/* Independently owned PostgreSQL pools. */}}
{{- define "tracking-postgresql.env" }}
{{- include "owner-postgresql.env" (dict "root" . "owner" "tracking" "prefix" "TRACKING") }}
{{- end }}
{{- define "doccle-postgresql.env" }}
{{- include "owner-postgresql.env" (dict "root" . "owner" "doccle" "prefix" "DOCCLE") }}
{{- end }}
{{- define "archive-postgresql.env" }}
{{- include "owner-postgresql.env" (dict "root" . "owner" "archive" "prefix" "ARCHIVE") }}
{{- end }}
{{- define "owner-postgresql.env" }}
- name: {{ .prefix }}_POSTGRESQL_USER
  valueFrom:
    secretKeyRef:
      name: {{ .root.Release.Name }}-core-{{ .owner }}-postgresql
      key: user
- name: {{ .prefix }}_POSTGRESQL_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .root.Release.Name }}-core-{{ .owner }}-postgresql
      key: password
- name: {{ .prefix }}_POSTGRESQL_DATABASE
  valueFrom:
    secretKeyRef:
      name: {{ .root.Release.Name }}-core-{{ .owner }}-postgresql
      key: database
- name: {{ .prefix }}_POSTGRESQL_HOST
  valueFrom:
    secretKeyRef:
      name: {{ .root.Release.Name }}-core-{{ .owner }}-postgresql
      key: host
{{- $key := printf "%sPostgresql" .owner }}
{{- $settings := get .root.Values.database $key | default dict }}
{{- with $settings.minPoolSize }}
- name: {{ $.prefix }}_POSTGRESQL_MIN_POOL_SIZE
  value: {{ . | quote }}
{{- end }}
{{- with $settings.maxPoolSize }}
- name: {{ $.prefix }}_POSTGRESQL_MAX_POOL_SIZE
  value: {{ . | quote }}
{{- end }}
{{- with $settings.commandTimeout }}
- name: {{ $.prefix }}_POSTGRESQL_COMMAND_TIMEOUT
  value: {{ . | quote }}
{{- end }}
{{- end }}


{{/* Optional OTLP/HTTP tracing shared by every Xarta service process. */}}
{{- define "telemetry.env" }}
{{- if .root.Values.telemetry.enabled }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ required "telemetry.endpoint is required when telemetry is enabled" .root.Values.telemetry.endpoint | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ printf "xarta-%s" .name | quote }}
- name: OTEL_TRACES_SAMPLER
  value: "always_on"
{{- end }}
{{- end }}


{{/* Temporary storage volume, mount, and environment shared by consumers. */}}
{{- define "temporaryStorage.volume" }}
{{- if or .Values.temporaryStorage.existingSecret .Values.temporaryStorage.config }}
- name: temporary-storage-config
  secret:
    secretName: {{ .Values.temporaryStorage.existingSecret | default (printf "%s-core-temporary-storage" .Release.Name) }}
{{- else }}
- name: tmp
  persistentVolumeClaim:
    claimName: {{ required "volumes.tmp.existingClaim is required for filesystem temporary storage" .Values.volumes.tmp.existingClaim }}
{{- end }}
{{- end }}

{{- define "temporaryStorage.mount" }}
{{- if or .Values.temporaryStorage.existingSecret .Values.temporaryStorage.config }}
- name: temporary-storage-config
  readOnly: true
  mountPath: /mnt/config/temporary-storage.json
  subPath: temporary-storage.json
{{- else }}
- name: tmp
  mountPath: {{ .Values.volumes.tmp.path }}
{{- end }}
{{- end }}

{{- define "temporaryStorage.env" }}
- name: TMP_STORAGE
  value: {{ .Values.volumes.tmp.path | quote }}
{{- if or .Values.temporaryStorage.existingSecret .Values.temporaryStorage.config }}
- name: TMP_STORAGE_CONFIG_PATH
  value: /mnt/config/temporary-storage.json
{{- end }}
{{- end }}


{{/*
Yields the image tag with an optional digest, if specified.
*/}}
{{- define "image" -}}
{{- $tag := $.Values.image.tag | default "latest" -}}
{{- if $.Values.image.digest -}}
{{- $tag = $.Values.image.digest -}}
{{- end -}}
{{- $tag -}}
{{- end -}}


{{/*
Utility method yielding the number of configured replicas for a
service or worker.
*/}}
{{- define "deploy.replicas" -}}
{{- if .replicas -}}
{{- .replicas -}}
{{- else -}}
{{- $.Values.defaults.replicas -}}
{{- end -}}
{{- end -}}


{{/* PostgreSQL environment shared by durable tracking consumers. */}}
{{- define "postgresql.env" }}
- name: POSTGRESQL_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-core-postgresql
      key: user
- name: POSTGRESQL_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-core-postgresql
      key: password
- name: POSTGRESQL_DATABASE
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-core-postgresql
      key: database
- name: POSTGRESQL_HOST
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-core-postgresql
      key: host
{{- end }}


{{/*
Configures health probes for a deployment based on the defaults,
or on the configuration, if it is present.

Note that, whenever the `debug` mode has been specified, health
probes will be disabled.
*/}}
{{- define "probes" -}}
{{- if not $.Values.image.debug -}}
{{- if .probes -}}
{{- fail "Service specific probes not implemented in template!" -}}
{{- end -}}
{{- with $.Values.defaults.probes }}
{{- if .startup.enabled }}
startupProbe:
{{ omit .startup "enabled" | toYaml | nindent 2 }}
{{- end }}
{{- if .readiness.enabled }}
readinessProbe:
{{ omit .readiness "enabled" | toYaml | nindent 2 }}
{{- end }}
{{- if .liveness.enabled }}
livenessProbe:
{{ omit .liveness "enabled" | toYaml | nindent 2 }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}


{{/* Render default resource requests and limits. */}}
{{- define "resources" -}}
{{- with $.Values.defaults.resources }}
resources:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}


{{/*
Configures the nodeSelector, affinity and tolerations based on
the defaults specification. Service specific configurations
are not yet supported.
*/}}
{{- define "service.constraints" -}}
{{- with  .Values.defaults.nodeSelector }}
nodeSelector:
{{- toYaml . | nindent 2 }}
{{- end -}}
{{- with $.Values.defaults.affinity }}
affinity:
{{- toYaml . | nindent 2 }}
{{- end -}}
{{- with $.Values.defaults.tolerations }}
tolerations:
{{- toYaml . | nindent 2 }}
{{- end -}}
{{- end -}}


{{/*
Yields to configured pullSecrets associated with the service. Currently,
the template will only yield the pullSecrets configured in the `defaults`
section of Values.yaml.
*/}}
{{- define "service.pullSecrets" -}}
{{- if $.Values.image.pullSecrets }}
imagePullSecrets:
{{ toYaml $.Values.image.pullSecrets }}
{{ end -}}
{{- end -}}
