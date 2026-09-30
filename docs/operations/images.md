# Container images

One `Dockerfile`, three targets. They share every layer except the last, so a build of all three
costs one dependency install.

| Target | Command | Health check | Used by |
|--------|---------|--------------|---------|
| `api` (default) | `uvicorn oran_adapt.api.main:app` (options from `UVICORN_*`) | `GET /api/v1/health` | compose `api`; Helm/kustomize API Deployment |
| `worker` | `oran-adapt worker run` | `oran-adapt worker health` (the liveness file) | compose `worker` and `cdc-consumer`; one Deployment per worker pool |
| `migrator` | `oran-adapt db upgrade`, then exit | none (one-shot) | compose `migrate`; the Helm hook Job; the kustomize Job; the `wait-for-schema` init containers |

```
docker build --target api      -t oran-adapt-api:<version> .
docker build --target worker   -t oran-adapt-worker:<version> .
docker build --target migrator -t oran-adapt-migrator:<version> .
```

## Posture

- **Non-root.** Every target runs as UID/GID 10001. The code is owned by root and read-only;
  only `/tmp` is writable (`ARTIFACT_WORKDIR`, `WORKER_HEALTH_FILE`). The chart and the kustomize
  base add `readOnlyRootFilesystem`, drop every capability and mount `/tmp` as an `emptyDir`.
- **Pinned bases.** Every `FROM` and every image compose pulls names a tag *and* a digest
  (`name:tag@sha256:...`). The digest decides what runs; the tag is for humans.
  `tests/unit/test_phase11_packaging.py` fails the gate on an unpinned base.
- **CPU only.** torch comes from the CPU wheel index (build arg `TORCH_INDEX_URL`; point it at
  a mirror for an air-gapped build). PyPI itself is not a build arg: an air-gapped build also
  needs a PyPI mirror reachable as the default index (e.g. a registry-side proxy). The in-tree trainers run on CPU; the GPU
  worker pool in the chart is scheduling for plugins that bring their own device handling
  (see `LIMITATIONS` in [helm.md](helm.md#gpu-worker-pools)).
- **Graceful shutdown.** `STOPSIGNAL SIGTERM` everywhere. The API gives in-flight requests
  `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` seconds; the worker stops claiming, and requeues a running
  job it cannot finish within `JOB_DRAIN_TIMEOUT_S`.
- **Versions.** Python packages are installed with `requirements.lock` as a constraints file.

## Moving a base image

1. Pick the new tag, then resolve its multi-arch index digest:
   `docker buildx imagetools inspect python:<tag>` (the `Digest:` of the index, not of one
   platform).
2. Change tag and digest together: `PYTHON_IMAGE` in `Dockerfile`, the `FROM` lines of
   `docker/mlflow/Dockerfile` and `docker/sandbox/Dockerfile`, the `image:` lines of
   `docker-compose.yml`, and `POSTGRES_IMAGE` in `scripts/ci/kind_e2e.sh`.
3. Run `bash scripts/verify.sh 11`. The pin tests check the shape; CI builds the images.

## SBOM

The CI `build-images` job builds each target with `docker buildx build --sbom=true` and uploads
the SPDX documents as the `image-sboms` artifact (`reports/sbom-<target>/`). The `supply-chain`
job produces the Python dependency SBOM (CycloneDX) from `requirements.lock`. Neither runs on the
development laptop, which has no Docker.

## Publishing

The chart and the overlays refer to images by repository and digest
(`image.<target>.repository`, `image.<target>.digest` in Helm; `images:` in a kustomize overlay).
Push the three images to your registry, then pin the digests your registry reports.
