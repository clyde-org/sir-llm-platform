{{/*
Expand the name of the chart.
*/}}
{{- define "vllm-stack.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "vllm-stack.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Namespace helper
*/}}
{{- define "vllm-stack.namespace" -}}
{{ .Values.namespace | default "llm-gateway" }}
{{- end }}