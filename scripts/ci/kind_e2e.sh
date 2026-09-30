#!/usr/bin/env bash
# Hardening Phase 11 heavy gate: the Helm chart on a real cluster (kind).
#
#   1. build the api, worker and migrator images and load them into the kind node;
#   2. a throwaway PostgreSQL and a Secret with random credentials;
#   3. helm install --wait: the pre-install migration hook, then the API and worker pods
#      (each waiting for the schema in its init container);
#   4. checks: /health, /ready, `oran-adapt db status` = at_head, the worker's liveness probe;
#   5. helm upgrade with changed settings: the pre-upgrade hook runs again, pods roll;
#   6. helm rollback to revision 1: the schema stays (migrations only expand), pods roll back;
#   7. the same checks after each step, then uninstall.
#
# Runs in .github/workflows/k8s-e2e.yml (scheduled and on demand) on a runner with kind, kubectl
# and helm. Never on the development laptop. Expects the current kube context to be the kind
# cluster named by KIND_CLUSTER.
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-oran-e2e}"
NAMESPACE="${E2E_NAMESPACE:-oran-e2e}"
RELEASE="${E2E_RELEASE:-e2e}"
CHART=deploy/helm/oran-adapt
VALUES="$CHART/ci/kind-values.yaml"
POSTGRES_IMAGE="postgres:16.4-bookworm@sha256:e62fbf9d3e2b49816a32c400ed2dba83e3b361e6833e624024309c35d334b412"
TIMEOUT="${E2E_TIMEOUT:-10m}"

dump() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    kubectl -n "$NAMESPACE" get pods,jobs,events -o wide || true
    kubectl -n "$NAMESPACE" logs -l app.kubernetes.io/instance="$RELEASE" --all-containers \
      --tail=80 --prefix || true
  fi
  exit $rc
}
trap dump EXIT

for target in api worker migrator; do
  docker build --target "$target" --tag "oran-adapt-$target:e2e" .
  kind load docker-image --name "$CLUSTER" "oran-adapt-$target:e2e"
done

kubectl create namespace "$NAMESPACE"
PG_PASSWORD="$(openssl rand -hex 16)"
API_KEY="$(openssl rand -hex 24)"
API_KEY_SHA256="$(printf '%s' "$API_KEY" | sha256sum | cut -d' ' -f1)"
kubectl -n "$NAMESPACE" create secret generic postgres-auth --from-literal=password="$PG_PASSWORD"
kubectl -n "$NAMESPACE" apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: postgres}
spec:
  selector: {matchLabels: {app: postgres}}
  template:
    metadata: {labels: {app: postgres}}
    spec:
      containers:
        - name: postgres
          image: ${POSTGRES_IMAGE}
          env:
            - {name: POSTGRES_USER, value: oran}
            - {name: POSTGRES_DB, value: oran_adapt}
            - name: POSTGRES_PASSWORD
              valueFrom: {secretKeyRef: {name: postgres-auth, key: password}}
          ports: [{containerPort: 5432}]
          readinessProbe:
            exec: {command: ["pg_isready", "-U", "oran", "-d", "oran_adapt"]}
            periodSeconds: 3
---
apiVersion: v1
kind: Service
metadata: {name: postgres}
spec:
  selector: {app: postgres}
  ports: [{port: 5432}]
EOF
kubectl -n "$NAMESPACE" rollout status deployment/postgres --timeout=5m
# MLflow's store gets its own database: it keeps its own alembic_version table.
kubectl -n "$NAMESPACE" exec deployment/postgres -- createdb -U oran mlflow

DB_URL="postgresql+psycopg://oran:${PG_PASSWORD}@postgres:5432/oran_adapt"
kubectl -n "$NAMESPACE" create secret generic oran-adapt-e2e-env \
  --from-literal=DATABASE_URL="$DB_URL" \
  --from-literal=MLFLOW_TRACKING_URI="postgresql+psycopg://oran:${PG_PASSWORD}@postgres:5432/mlflow" \
  --from-literal=API_KEYS="{\"${API_KEY_SHA256}\": \"ADMIN:e2e\"}"

check() {  # check STEP: the release is up, migrated and live
  local fullname="$RELEASE-oran-adapt"
  kubectl -n "$NAMESPACE" rollout status "deployment/$fullname-api" --timeout="$TIMEOUT"
  kubectl -n "$NAMESPACE" rollout status "deployment/$fullname-worker-default" --timeout="$TIMEOUT"
  kubectl -n "$NAMESPACE" exec "deployment/$fullname-api" -- oran-adapt db status \
    | python3 -c 'import json,sys; b=json.load(sys.stdin); assert b["state"]=="at_head", b; print("db:", b)'
  kubectl -n "$NAMESPACE" exec "deployment/$fullname-worker-default" -- oran-adapt worker health
  kubectl -n "$NAMESPACE" exec "deployment/$fullname-api" -- python -c \
    'import urllib.request as u; [print(p, u.urlopen("http://127.0.0.1:8000/api/v1/" + p, timeout=5).status) for p in ("health", "ready")]'
  echo "$1: OK"
}

helm install "$RELEASE" "$CHART" -n "$NAMESPACE" --values "$VALUES" --wait --timeout "$TIMEOUT"
kubectl -n "$NAMESPACE" get job "$RELEASE-oran-adapt-migrate" \
  -o jsonpath='{.status.succeeded}' | grep -qx 1
check install

helm upgrade "$RELEASE" "$CHART" -n "$NAMESPACE" --values "$VALUES" \
  --set config.env.LOG_LEVEL=DEBUG --wait --timeout "$TIMEOUT"
kubectl -n "$NAMESPACE" get job "$RELEASE-oran-adapt-migrate" \
  -o jsonpath='{.status.succeeded}' | grep -qx 1
check upgrade

helm rollback "$RELEASE" 1 -n "$NAMESPACE" --wait --timeout "$TIMEOUT"
check rollback

helm uninstall "$RELEASE" -n "$NAMESPACE" --wait
echo "kind e2e (install, upgrade, rollback): PASS"
