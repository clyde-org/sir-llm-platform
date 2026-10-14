#!/usr/bin/env bash
# Build + push npu-ops images (arm64, emulated via qemu) and pin the tag in the
# manifests. Tags are content hashes, so nodes always fetch new code (avoids
# containerd's stale `latest` cache).
#
#   ./build.sh            # build+push both, update manifests
#   ./build.sh agent      # agent only
#   ./build.sh dashboard  # dashboard only
set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-all}"
REGISTRY="${REGISTRY:-ghcr.io/fanbondi}"

hash_of() { cat "$@" | sha1sum | cut -c1-8; }

build_agent() {
  local tag="v1-$(hash_of agent/agent.py agent/npu_mem_load.py agent/Dockerfile)"
  echo ">> building agent ($tag)"
  DOCKER_BUILDKIT=0 docker build --platform linux/arm64 \
    -f agent/Dockerfile -t "$REGISTRY/npu-ops-agent:$tag" .
  docker push "$REGISTRY/npu-ops-agent:$tag"
  sed -i "s|image: $REGISTRY/npu-ops-agent:.*|image: $REGISTRY/npu-ops-agent:$tag|" \
    manifests/10-agent.yaml
  echo ">> pushed $REGISTRY/npu-ops-agent:$tag"
}

build_dashboard() {
  local tag="v1-$(hash_of app/main.py app/keys.py app/aggregate.py app/static/index.html Dockerfile)"
  echo ">> building dashboard ($tag)"
  DOCKER_BUILDKIT=0 docker build --platform linux/arm64 --network=host \
    -t "$REGISTRY/npu-ops:$tag" .
  docker push "$REGISTRY/npu-ops:$tag"
  sed -i "s|image: $REGISTRY/npu-ops:.*|image: $REGISTRY/npu-ops:$tag|" \
    manifests/20-dashboard.yaml
  echo ">> pushed $REGISTRY/npu-ops:$tag"
}

case "$TARGET" in
  agent) build_agent ;;
  dashboard) build_dashboard ;;
  all) build_agent; build_dashboard ;;
  *) echo "usage: $0 [all|agent|dashboard]"; exit 1 ;;
esac
echo ">> done. apply with: kubectl apply -f manifests/"
