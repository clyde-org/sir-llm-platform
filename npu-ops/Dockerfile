# npu-ops dashboard backend (FastAPI)
#
# NOTE on base image: this build host cannot pull fresh images (corporate TLS
# MITM breaks the docker daemon's trust for new blobs), so we build on top of
# kv-sidecar (arm64, already local, ships python3.11+pip). If a normal
# registry access is available, swap to `python:3.11-slim` (multi-arch) and
# rebuild with --platform.
FROM kv-sidecar:7f52ac0a
ENTRYPOINT []
ENV http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY=
RUN pip install --no-cache-dir --proxy http://127.0.0.1:3128 \
    fastapi==0.116.1 \
    "uvicorn[standard]==0.35.0" \
    kubernetes==33.1.0 \
    httpx==0.28.1 \
    psycopg2-binary==2.9.9
WORKDIR /app
COPY app/ /app/
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
