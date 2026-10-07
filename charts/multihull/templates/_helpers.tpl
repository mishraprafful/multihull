{{- define "multihull.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "multihull.fullname" -}}
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

{{- define "multihull.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "multihull.labels" -}}
helm.sh/chart: {{ include "multihull.chart" . }}
{{ include "multihull.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "multihull.selectorLabels" -}}
app.kubernetes.io/name: {{ include "multihull.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "multihull.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "multihull.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "multihull.routerImage" -}}
{{- printf "%s:%s" .Values.router.image.repository (default .Chart.AppVersion .Values.router.image.tag) }}
{{- end }}

{{- define "multihull.controllerImage" -}}
{{- printf "%s:%s" .Values.controller.image.repository (default .Chart.AppVersion .Values.controller.image.tag) }}
{{- end }}

{{- define "multihull.tomlValue" -}}
{{- $value := . -}}
{{- if and (kindIs "string" $value) (regexMatch "^[+-]?([0-9]+(\\.[0-9]*)?|\\.[0-9]+)([eE][+-]?[0-9]+)?$" $value) -}}
{{- $value = float64 $value -}}
{{- end -}}
{{- if kindIs "string" $value -}}
{{- toJson $value -}}
{{- else if and (kindIs "float64" $value) (eq $value (floor $value)) (lt $value 9007199254740992.0) (gt $value -9007199254740992.0) -}}
{{- int64 $value -}}
{{- else -}}
{{- $value -}}
{{- end -}}
{{- end }}
