{{- define "jenkins-watchdog.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "jenkins-watchdog.labels" -}}
app.kubernetes.io/name: jenkins-watchdog
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "jenkins-watchdog.selectorLabels" -}}
app.kubernetes.io/name: jenkins-watchdog
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "jenkins-watchdog.componentSelectorLabels" -}}
{{ include "jenkins-watchdog.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "jenkins-watchdog.postgresqlSecret" -}}
{{- default (printf "%s-postgresql" .Release.Name) .Values.postgresql.auth.existingSecret -}}
{{- end -}}

{{- define "jenkins-watchdog.envFrom" -}}
- configMapRef:
    name: {{ include "jenkins-watchdog.fullname" . }}-config
- secretRef:
    name: jenkins-watchdog-secrets
    optional: true
{{- if .Values.externalSecrets.enabled }}
- secretRef:
    name: {{ include "jenkins-watchdog.fullname" . }}-secrets
{{- end }}
{{- end -}}

{{- define "jenkins-watchdog.databasePasswordEnv" -}}
- name: WATCHDOG_DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "jenkins-watchdog.postgresqlSecret" . }}
      key: {{ .Values.postgresql.auth.secretKeys.userPasswordKey }}
{{- end -}}
