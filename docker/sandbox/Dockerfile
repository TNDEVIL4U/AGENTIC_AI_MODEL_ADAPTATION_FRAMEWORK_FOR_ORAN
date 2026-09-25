# Sandbox image for the Docker execution backend (Settings.sandbox_backend="docker"),
# consumed by src/oran_adapt/sandbox/runner.py:run_in_docker(). Deliberately minimal: only
# the packages LLM-generated adaptation code is allowed to import at all
# (sandbox.security.ALLOWED_IMPORT_ROOTS) - no shell utilities, no network tools, and none of
# this repository's own source or credentials get copied in. The container never talks to the
# network (run_in_docker passes --network none) and the image itself needs no runtime access to
# anything outside the workdir bind-mounted in at /sandbox.
#
# Build:  docker build -t oran-adapt-sandbox:latest -f docker/sandbox/Dockerfile docker/sandbox
# (the tag must match Settings.sandbox_docker_image, default "oran-adapt-sandbox:latest")
FROM python:3.13.7-slim-bookworm

# Pinned to the same versions as requirements.lock; torch is the CPU-only wheel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
    numpy==2.5.3 \
    pandas==3.0.6 \
    scipy==1.18.1 \
    scikit-learn==1.9.1 \
    xgboost==3.4.1 \
    torch==2.14.0 \
    joblib==1.6.0 \
    skops==0.15.0 \
    safetensors==0.8.0

# Non-root by default. On Linux run_in_docker overrides this with --user <host uid:gid> so the
# bind-mounted workdir stays writable; it also passes --read-only, --cap-drop ALL and
# --security-opt no-new-privileges.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
USER 10001

WORKDIR /sandbox
