{{/*
What this chart's own objects share with Airflow's: its labels (airflow.labels), its image and its pull Secrets, so each
is set once, in Airflow's values.
*/}}

{{- define "ngt-airflow.labels" -}}
app: {{ .name }}
{{- with .root.Values.airflow.labels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "ngt-airflow.image" -}}
{{ .Values.airflow.images.airflow.repository }}:{{ .Values.airflow.images.airflow.tag }}
{{- end }}

{{/* The pod settings every pod here has: its own ServiceAccount, no API token, no service links. */}}
{{- define "ngt-airflow.podSpec" -}}
serviceAccountName: ngt-airflow
automountServiceAccountToken: false
# A Service named ngt-airflow* would set NGT_AIRFLOW_*_PORT, which airflow_env.sh takes for its own.
enableServiceLinks: false
{{- with .Values.airflow.imagePullSecrets }}
imagePullSecrets:
  {{- range . }}
  - name: {{ if kindIs "string" . }}{{ . }}{{ else }}{{ .name }}{{ end }}
  {{- end }}
{{- end }}
{{- end }}

{{- define "ngt-airflow.containerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
readOnlyRootFilesystem: true
{{- end }}
