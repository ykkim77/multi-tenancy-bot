{{/* Tenant namespace name (matches the Agentic Operator's convention) */}}
{{- define "kcu-tenant.namespace" -}}
{{- printf "tenant-%s" .Values.tenantId -}}
{{- end -}}

{{/* Common labels applied to every object */}}
{{- define "kcu-tenant.labels" -}}
app.kubernetes.io/managed-by: helm
portal.kcu.ac.kr/tenant: {{ .Values.tenantId | quote }}
portal.kcu.ac.kr/tier: {{ .Values.tier | quote }}
experiment: kcu-portal
{{- end -}}

{{/*
  Map .Values.tier to the appropriate PriorityClass name.
  Mirrors the Agentic Operator's priorityClassFor() in controllers/defaults.go.
*/}}
{{- define "kcu-tenant.priorityClassName" -}}
{{- if eq .Values.tier "priority" -}}kcu-tenant-priority
{{- else if eq .Values.tier "internal" -}}kcu-tenant-internal
{{- else -}}kcu-tenant-standard
{{- end -}}
{{- end -}}
