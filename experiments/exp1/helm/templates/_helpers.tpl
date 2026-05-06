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
