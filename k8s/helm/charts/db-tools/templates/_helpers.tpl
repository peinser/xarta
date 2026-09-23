{{/*
Copyright Peinser BV

Generic utilities methods for Helm.
*/}}


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
Configures the nodeSelector, affinity and tolerations based on
the defaults specification. Job specific configurations
are not yet supported.
*/}}
{{- define "job.constraints" -}}
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
{{- define "job.pullSecrets" -}}
{{- if $.Values.image.pullSecrets }}
imagePullSecrets:
{{ toYaml $.Values.image.pullSecrets }}
{{ end -}}
{{- end -}}
