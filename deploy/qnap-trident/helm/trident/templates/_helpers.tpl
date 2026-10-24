{{/*
Expand the name of the chart.
*/}}
{{- define "qnap-trident.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "qnap-trident.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "qnap-trident.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "qnap-trident.labels" -}}
helm.sh/chart: {{ include "qnap-trident.chart" . }}
{{ include "qnap-trident.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "qnap-trident.selectorLabels" -}}
app.kubernetes.io/name: {{ include "qnap-trident.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Determines if rancher roles should be created by checking for the presence of the cattle-system namespace
or annotations with the prefix "cattle.io/" in the namespace where the chart is being installed.
Override auto-detection and force install the roles by setting Values.forceInstallRancherClusterRoles to 'true'.
*/}}
{{- define "shouldInstallRancherRoles" -}}
{{- $isRancher := false -}}
{{- $currentNs := .Release.Namespace -}}
{{- $currentNsObj := lookup "v1" "Namespace" "" $currentNs -}}
{{- /* Check if 'forceInstallRancherClusterRoles' is set */ -}}
{{- if .Values.forceInstallRancherClusterRoles }}
    {{- $isRancher = true -}}
{{- end }}
{{- /* Check if the annotation prefix "cattle.io/" exists on the namespace */ -}}
{{- if $currentNsObj }}
  {{- range $key, $value := $currentNsObj.metadata.annotations }}
    {{- if hasPrefix "cattle.io/" $key }}
      {{- $isRancher = true -}}
    {{- end }}
  {{- end }}
{{- end }}
{{- /* Check if cattle-system ns exists */ -}}
{{- $cattleNs := lookup "v1" "Namespace" "" "cattle-system" -}}
{{- if $cattleNs }}
  {{- $isRancher = true -}}
{{- end }}
{{- $isRancher -}}
{{- end }}