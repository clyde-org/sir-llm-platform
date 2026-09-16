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
