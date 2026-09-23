{{/*
Namespace for all resources.
*/}}
{{- define "vllm-stack.namespace" -}}
{{ .Values.namespace | default "llm-gateway" }}
{{- end }}

{{/*
Control-plane node pinning (redis, router, litellm) as nodeAffinity.
Call with | indent <N> to align under the calling `spec:`.
Emits nothing when pinning is disabled.
*/}}
{{- define "vllm-stack.nodePin" -}}
{{- if .Values.pin.enabled }}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - key: "kubernetes.io/hostname"
              operator: In
              values:
                - {{ .Values.pin.nodeName | quote }}
{{- end }}
{{- end }}

{{/*
Shared control-plane tolerations: allow scheduling onto the master/control-plane
node where the control plane is pinned.
Call with | indent <N> to align under the calling `spec:`.
*/}}
{{- define "vllm-stack.tolerations" -}}
tolerations:
  - effect: NoSchedule
    key: node-role.kubernetes.io/control-plane
    operator: Exists
  - effect: NoSchedule
    key: node-role.kubernetes.io/master
    operator: Exists
{{- end }}

{{/*
Anthropic /v1/messages upstream map (JSON) for the router's model-aware
raw-forward endpoint (env ANTHROPIC_UPSTREAMS, see 03-router.yaml). Maps each
servable model name to the Anthropic-native vLLM endpoint hosting that pool:
  - model.servedName (+ model.legacyAlias) -> claudeService (phase-1 pool)
  - phase2.servedName                      -> phase2 service (262k pool)
*/}}
{{- define "vllm-stack.anthropicUpstreams" -}}
{{- $pairs := list -}}
{{- if .Values.claudeService.enabled -}}
{{- $p1 := printf "http://%s:%v" .Values.claudeService.name .Values.vllm.port -}}
{{- $pairs = append $pairs (printf "\"%s\":\"%s\"" .Values.model.servedName $p1) -}}
{{- if .Values.model.legacyAlias -}}
{{- $pairs = append $pairs (printf "\"%s\":\"%s\"" .Values.model.legacyAlias $p1) -}}
{{- end -}}
{{- end -}}
{{- if .Values.phase2.enabled -}}
{{- $p2 := printf "http://%s:%v" .Values.phase2.serviceName .Values.vllm.port -}}
{{- $pairs = append $pairs (printf "\"%s\":\"%s\"" (.Values.phase2.servedName | default .Values.model.servedName) $p2) -}}
{{- end -}}
{ {{ $pairs | join "," }} }
{{- end }}
