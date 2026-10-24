{{- define "temporal.fullname" -}}
{{- if contains .Chart.Name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "temporal.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: agentic-ops
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels; call with (dict "ctx" $ "component" "server") */}}
{{- define "temporal.selectorLabels" -}}
app.kubernetes.io/name: {{ .ctx.Chart.Name }}-{{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- end }}

{{- define "temporal.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/* Image for pg_dump: backup.image, else the postgres subchart's image */}}
{{- define "temporal.backupImage" -}}
{{- if .Values.backup.image }}
{{- .Values.backup.image }}
{{- else }}
{{- with .Values.postgres.image }}
{{- printf "%s/%s:%s" .registry .repository (.tag | default $.Subcharts.postgres.Chart.AppVersion) }}
{{- end }}
{{- end }}
{{- end }}

{{/* Name of the shared database credentials Secret */}}
{{- define "temporal.dbSecretName" -}}
{{- required "postgres.settings.existingSecret is required" .Values.postgres.settings.existingSecret }}
{{- end }}

{{/* Service name of the postgres subchart (mirrors groundhog2k's fullname helper) */}}
{{- define "temporal.postgresHost" -}}
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

{{- define "temporal.publicServiceName" -}}
{{- include "temporal.fullname" . }}-public
{{- end }}

{{/*
The address clients dial, host:port. In order: endpoint.address; the passthrough ingress
host on 443; the public Service's LoadBalancer address on 7233 (known only once the Service
exists, so from the second install or upgrade on). Empty if none is known yet.
*/}}
{{- define "temporal.endpointAddress" -}}
{{- if .Values.endpoint.address }}
{{- .Values.endpoint.address }}
{{- else if and .Values.passthroughIngress.enabled .Values.passthroughIngress.host }}
{{- printf "%s:443" .Values.passthroughIngress.host }}
{{- else if .Values.publicService.enabled }}
{{- $svc := lookup "v1" "Service" .Release.Namespace (include "temporal.publicServiceName" .) | default dict }}
{{- $ingress := dig "status" "loadBalancer" "ingress" list $svc }}
{{- if $ingress }}
{{- $lb := first $ingress }}
{{- printf "%s:%v" (default $lb.hostname $lb.ip) .Values.publicService.port }}
{{- end }}
{{- end }}
{{- end }}

{{/* SQL_* environment for temporal-sql-tool; call with (dict "ctx" $ "database" "DB_NAME") */}}
{{- define "temporal.sqlToolEnv" -}}
- name: SQL_PLUGIN
  value: postgres12_pgx
{{- range $env, $key := dict "SQL_HOST" "DB_HOST" "SQL_PORT" "DB_PORT" "SQL_USER" "DB_USER" "SQL_PASSWORD" "DB_PASSWORD" "SQL_DATABASE" .database }}
- name: {{ $env }}
  valueFrom:
    secretKeyRef:
      name: {{ include "temporal.dbSecretName" $.ctx }}
      key: {{ $key }}
{{- end }}
{{- end }}
