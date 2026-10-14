# npu-node-agent: stdlib-only python; runs privileged with host root at /host
# (same base-image note as ../Dockerfile)
FROM kv-sidecar:7f52ac0a
ENTRYPOINT []
WORKDIR /app
COPY agent/ /app/
EXPOSE 8083
CMD ["python3", "/app/agent.py"]
