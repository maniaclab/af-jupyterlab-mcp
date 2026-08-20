{{/*
Expand the name of the chart.
*/}}
{{- define "af-jupyterlab-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "af-jupyterlab-mcp.fullname" -}}
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
Chart name and version label value.
*/}}
{{- define "af-jupyterlab-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "af-jupyterlab-mcp.labels" -}}
helm.sh/chart: {{ include "af-jupyterlab-mcp.chart" . }}
{{ include "af-jupyterlab-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels. Kept stable (name + instance) so external selectors keyed on
app.kubernetes.io/name=af-jupyterlab-mcp continue to match.
*/}}
{{- define "af-jupyterlab-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "af-jupyterlab-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name to use.
*/}}
{{- define "af-jupyterlab-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "af-jupyterlab-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Validate the configuration. Fails the render early with a clear message
rather than producing a manifest the server would reject at startup.
*/}}
{{- define "af-jupyterlab-mcp.validate" -}}
{{- if not .Values.broker.brokerUrl -}}
  {{- fail "broker.brokerUrl is required (this backend has no auth mode besides broker-issued bearer JWTs)" -}}
{{- end -}}
{{- if not .Values.notebook.namespace -}}
  {{- fail "notebook.namespace is required" -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
  {{- if not .Values.ingress.host -}}
    {{- fail "ingress.enabled=true requires ingress.host" -}}
  {{- end -}}
{{- end -}}
{{- end }}

{{/*
Public resource URL: explicit value, else derived from the ingress host, else
empty (the server falls back to http://<host>:<port>).
*/}}
{{- define "af-jupyterlab-mcp.resourceUrl" -}}
{{- if .Values.server.resourceUrl -}}
{{- .Values.server.resourceUrl -}}
{{- else if and .Values.ingress.enabled .Values.ingress.host -}}
{{- printf "https://%s" .Values.ingress.host -}}
{{- end -}}
{{- end }}

{{/*
Build the `af-jupyterlab-mcp serve` argv as YAML list items (one `- "value"`
line per entry), for direct use under a container's `args:` key. Emitting a
real list -- not a single joined/shell-quoted string -- matters because
this chart runs the built image directly (no `/bin/sh -c` wrapper the way
ami-mcp's pixi-install chart uses), so each element must reach argparse as
its own argv entry.
*/}}
{{- define "af-jupyterlab-mcp.serveArgs" -}}
{{- include "af-jupyterlab-mcp.validate" . -}}
{{- $args := list "serve"
    "--host" (.Values.server.host | toString)
    "--port" (.Values.server.port | toString)
    "--audience" .Values.broker.audience
    "--forwarded-allow-ips" (printf "'%s'" .Values.forwardedAllowIps)
    "--log-level" .Values.logLevel -}}
{{- with .Values.broker.jwksUrl -}}
{{- $args = append $args "--broker-jwks-url" -}}
{{- $args = append $args . -}}
{{- end -}}
{{- with .Values.broker.issuer -}}
{{- $args = append $args "--broker-issuer" -}}
{{- $args = append $args . -}}
{{- end -}}
{{- with (include "af-jupyterlab-mcp.resourceUrl" .) -}}
{{- $args = append $args "--resource-url" -}}
{{- $args = append $args . -}}
{{- end -}}
{{- $args | join " " -}}
{{- end }}
