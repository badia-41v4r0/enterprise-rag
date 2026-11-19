{{- define "erag.name" -}}{{ .Release.Name }}-enterprise-rag{{- end -}}

{{- define "erag.labels" -}}
app.kubernetes.io/name: enterprise-rag
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "erag.selector" -}}
app.kubernetes.io/name: enterprise-rag
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "erag.image" -}}{{ .Values.image.repository }}:{{ .Values.image.tag }}{{- end -}}

{{/* Settings shared by app, worker and the migration job. */}}
{{- define "erag.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "erag.name" . }}
  - secretRef:
      name: {{ .Values.secrets.existingSecret }}
{{- end -}}

{{- define "erag.security" -}}
securityContext:
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
{{- end -}}
