{{- define "querygate.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "querygate.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "querygate.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "querygate.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "querygate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "querygate.selectorLabels" -}}
app.kubernetes.io/name: {{ include "querygate.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "querygate.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "querygate.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Guard rails. These are invariants of the server, not preferences: breaking them
produces a deployment that looks healthy and behaves wrongly, which is worse
than a failed install.
*/}}
{{- define "querygate.validate" -}}
{{- $replicas := .Values.replicaCount | int -}}
{{- if and (gt $replicas 1) (not .Values.redis.url) -}}
{{- fail "replicaCount > 1 requires redis.url: the rate limiter and result cache are per-process, so N replicas would silently allow N times the configured limit." -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (not .Values.redis.url) -}}
{{- fail "autoscaling.enabled requires redis.url (see replicaCount note)." -}}
{{- end -}}
{{- if eq .Values.config.auth.provider "static" -}}
{{- if ne .Values.config.env "local" -}}
{{- fail "config.auth.provider=static is the demo verifier with fixed tokens. Use oidc for anything but config.env=local." -}}
{{- end -}}
{{- end -}}
{{- end -}}
