{{/* Names */}}
{{- define "oran-adapt.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "oran-adapt.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "oran-adapt.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "oran-adapt.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "oran-adapt.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oran-adapt.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "oran-adapt.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "oran-adapt.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- required "serviceAccount.name is required when serviceAccount.create is false" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The Secret every pod reads: an existing one, or the ExternalSecret's target. */}}
{{- define "oran-adapt.secretName" -}}
{{- default (printf "%s-env" (include "oran-adapt.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{/* image: call with (dict "root" $ "image" .Values.image.api) */}}
{{- define "oran-adapt.image" -}}
{{- $root := .root -}}
{{- $ref := .image.repository -}}
{{- if $root.Values.image.registry -}}
{{- $ref = printf "%s/%s" $root.Values.image.registry $ref -}}
{{- end -}}
{{- if .image.digest -}}
{{- printf "%s@%s" $ref .image.digest -}}
{{- else -}}
{{- printf "%s:%s" $ref (default $root.Chart.AppVersion $root.Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{/* envFrom shared by the API and the workers: the common and per-port ConfigMaps, the Secret. */}}
{{- define "oran-adapt.envFrom" -}}
- configMapRef:
    name: {{ include "oran-adapt.fullname" . }}-config
{{- range $port, $spec := .Values.adapters }}
- configMapRef:
    name: {{ include "oran-adapt.fullname" $ }}-adapter-{{ $port | replace "_" "-" }}
{{- end }}
- secretRef:
    name: {{ include "oran-adapt.secretName" . }}
{{- end -}}

{{/* The same settings inlined, for the pre-upgrade hook: it runs before this release's
     ConfigMaps are applied, so it must not read the previous release's. */}}
{{- define "oran-adapt.envInline" -}}
{{- range $key, $value := .Values.config.env }}
- name: {{ $key }}
  value: {{ $value | toString | quote }}
{{- end }}
{{- range $port, $spec := .Values.adapters }}
{{- range $key, $value := $spec.env }}
- name: {{ $key }}
  value: {{ $value | toString | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "oran-adapt.tmpVolume" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.tmpSizeLimit }}
{{- end -}}

{{/* Init container: wait until the migration hook has brought the schema to this release. */}}
{{- define "oran-adapt.waitForSchema" -}}
- name: wait-for-schema
  image: {{ include "oran-adapt.image" (dict "root" . "image" .Values.image.migrator) }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: ["oran-adapt", "db", "wait", "--timeout-s", {{ .Values.migrations.waitTimeoutS | toString | quote }}]
  envFrom:
    {{- include "oran-adapt.envFrom" . | nindent 4 }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  resources:
    {{- toYaml .Values.migrations.resources | nindent 4 }}
  volumeMounts:
    - {name: tmp, mountPath: /tmp}
{{- end -}}
