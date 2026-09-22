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
FROM python:3.11-slim

RUN pip install --no-cache-dir \
    numpy \
    pandas \
    scikit-learn \
    xgboost \
    torch \
    joblib

WORKDIR /sandbox
