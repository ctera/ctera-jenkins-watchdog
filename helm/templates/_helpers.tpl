{{- define "jenkins-watchdog.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "jenkins-watchdog.labels" -}}
app.kubernetes.io/name: jenkins-watchdog
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: observability
{{- end -}}

{{- define "jenkins-watchdog.selectorLabels" -}}
app.kubernetes.io/name: jenkins-watchdog
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
