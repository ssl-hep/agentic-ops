{{- define "agentic-ops.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "agentic-ops.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels; call with (dict "ctx" $ "component" "backend") */}}
{{- define "agentic-ops.selectorLabels" -}}
app.kubernetes.io/name: {{ .ctx.Chart.Name }}-{{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- end }}

{{/* Name of the shared database credentials Secret */}}
{{- define "agentic-ops.dbSecretName" -}}
{{- required "postgres.settings.existingSecret is required" .Values.postgres.settings.existingSecret }}
{{- end }}

{{/* Service name of the postgres subchart (mirrors groundhog2k's fullname helper) */}}
{{- define "agentic-ops.postgresHost" -}}
{{- if .Values.postgres.fullnameOverride }}
{{- .Values.postgres.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default "postgres" .Values.postgres.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}
