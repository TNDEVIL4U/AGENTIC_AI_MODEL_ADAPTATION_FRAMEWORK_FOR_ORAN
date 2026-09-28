#!/usr/bin/env bash
# Hardening Phase 0 exit gate: bring the full compose stack up from a clean checkout, check that
# every service converged, hit health + readiness + metrics, submit one drift event (and its
# duplicate), read the job back, then tear everything down (volumes included).
#
# Runs in CI (.github/workflows/ci.yml, job phase-0-baseline). It writes a throwaway .env with
# random credentials; it refuses to run if a .env already exists, so it never overwrites one.
#
#   SMOKE_BASE_URL   API base URL            (default http://127.0.0.1:8000/api/v1)
#   SMOKE_TIMEOUT_S  wait for API readiness  (default 600)
#   SMOKE_KEEP_UP    set to 1 to skip teardown (debugging only)
set -euo pipefail

BASE_URL="${SMOKE_BASE_URL:-http://127.0.0.1:8000/api/v1}"
TIMEOUT_S="${SMOKE_TIMEOUT_S:-600}"
ONE_SHOT_SERVICES="debezium-init"

if [[ -e .env ]]; then
  echo "compose_smoke: .env already exists; refusing to overwrite it" >&2
  exit 2
fi

# Keep a copy of stderr so a failure can be reported as a GitHub annotation (annotations are
# readable without signing in; job logs are not).
ERR_LOG="$(mktemp)"
exec 2> >(tee -a "$ERR_LOG" >&2)

annotate() {  # annotate TITLE TEXT: one ::error annotation, newlines encoded
  local text
  text="$(printf '%s' "$2" | tail -n 60 | sed -e 's/%/%25/g' -e 's/\r/%0D/g' \
    | sed -e ':a;N;$!ba;s/\n/%0A/g')"
  echo "::error title=$1::${text}"
}

teardown() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "::group::compose ps (failure)"; docker compose ps -a || true; echo "::endgroup::"
    echo "::group::compose logs (failure)"; docker compose logs --no-color --tail=200 || true
    echo "::endgroup::"
    annotate "compose_smoke exit ${rc}" "$(cat "$ERR_LOG")"
    annotate "compose ps" "$(docker compose ps -a --format '{{.Service}} {{.State}} {{.Health}} {{.ExitCode}}' 2>&1)"
    for svc in $(docker compose ps -a --format '{{.Service}} {{.State}} {{.Health}} {{.ExitCode}}' 2>/dev/null \
        | awk '($2!="running" && $NF!="0") || $3=="unhealthy" || $3=="starting" {print $1}'); do
      annotate "logs: ${svc}" "$(docker compose logs --no-color --tail=40 "$svc" 2>&1)"
    done
  fi
  rm -f "$ERR_LOG"
  if [[ "${SMOKE_KEEP_UP:-0}" != "1" ]]; then
    docker compose down --volumes --remove-orphans || true
    rm -f .env
  fi
  exit $rc
}
trap teardown EXIT

# ---- throwaway configuration ----------------------------------------------------------------
API_KEY="$(openssl rand -hex 24)"
API_KEY_SHA256="$(printf '%s' "$API_KEY" | sha256sum | cut -d' ' -f1)"
cat > .env <<EOF
POSTGRES_PASSWORD=$(openssl rand -hex 16)
API_KEYS='{"${API_KEY_SHA256}": "ADMIN:ci-smoke"}'
LLM_PROVIDER=none
SANDBOX_BACKEND=subprocess
LOG_LEVEL=INFO
EOF

# ---- bring up -------------------------------------------------------------------------------
docker compose config --quiet
docker compose up -d --build

echo "waiting up to ${TIMEOUT_S}s for ${BASE_URL}/readiness"
deadline=$((SECONDS + TIMEOUT_S))
until curl -fsS "${BASE_URL}/readiness" >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "API not ready after ${TIMEOUT_S}s" >&2
    curl -sS "${BASE_URL}/readiness" || true
    exit 1
  fi
  sleep 5
done

# debezium-init starts only after the API is healthy; give it time to finish.
for svc in $ONE_SHOT_SERVICES; do
  deadline=$((SECONDS + 180))
  while :; do
    state="$(docker compose ps -a --format '{{.State}} {{.ExitCode}}' "$svc" 2>/dev/null || true)"
    [[ "$state" == exited* ]] && break
    if (( SECONDS >= deadline )); then echo "$svc did not finish: '$state'" >&2; exit 1; fi
    sleep 5
  done
  if [[ "$state" != "exited 0" ]]; then echo "$svc failed: '$state'" >&2; exit 1; fi
  echo "$svc: completed"
done

# ---- every long-running service converged ---------------------------------------------------
docker compose ps -a --format json | python3 -c '
import json, sys
one_shot = set(sys.argv[1].split())
text = sys.stdin.read().strip()
rows = json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l]
bad = []
for r in rows:
    svc, state, health = r["Service"], r["State"], r.get("Health", "")
    if svc in one_shot:
        continue
    if state != "running" or health not in ("", "healthy"):
        bad.append(f"{svc}: state={state} health={health or '-'}")
    print(f"{svc:14} {state:8} {health or '-'}")
if bad:
    sys.exit("not converged:\n  " + "\n  ".join(bad))
' "$ONE_SHOT_SERVICES"

# ---- endpoint checks ------------------------------------------------------------------------
auth=(-H "X-API-Key: ${API_KEY}")

curl -fsS "${BASE_URL}/health" | tee /dev/stderr | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"]=="ok"'
echo
curl -fsS "${BASE_URL}/readiness" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert body["ready"] is True, body
print("readiness:", {c["name"]: c["ok"] for c in body["components"]})'
metrics_text="$(curl -fsS "${BASE_URL}/metrics")"
grep -q "^# TYPE " <<<"$metrics_text" || { echo "metrics: no Prometheus exposition" >&2; exit 1; }
echo "metrics: exposition OK"

# Unauthenticated writes must be refused.
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/adaptation/events" \
  -H 'Content-Type: application/json' -d '{"model_id":"x"}')"
[[ "$code" == "401" ]] || { echo "expected 401 without a key, got $code" >&2; exit 1; }
echo "auth: unauthenticated POST refused (401)"

# One drift event for a model that is not onboarded: the job must be recorded and end FAILED
# with MODEL_NOT_FOUND (201), and the same event_id again must be a duplicate (200).
event='{"model_id":"ci-smoke-unregistered","event_id":"ci-smoke-1","drift_score":0.9}'
resp="$(curl -sS -w '\n%{http_code}' "${auth[@]}" -H 'Content-Type: application/json' \
  -X POST "${BASE_URL}/adaptation/events" -d "$event")"
code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
[[ "$code" == "201" ]] || { echo "first submit: expected 201, got $code: $body" >&2; exit 1; }
job_id="$(printf '%s' "$body" | python3 -c '
import json, sys
b = json.load(sys.stdin)
assert b["status"] == "FAILED" and b["error"]["code"] == "MODEL_NOT_FOUND", b
assert b["duplicate"] is False, b
print(b["job_id"])')"
echo "event: job ${job_id} recorded FAILED/MODEL_NOT_FOUND"

resp="$(curl -sS -w '\n%{http_code}' "${auth[@]}" -H 'Content-Type: application/json' \
  -X POST "${BASE_URL}/adaptation/events" -d "$event")"
code="${resp##*$'\n'}"; body="${resp%$'\n'*}"
[[ "$code" == "200" ]] || { echo "duplicate submit: expected 200, got $code: $body" >&2; exit 1; }
printf '%s' "$body" | python3 -c '
import json, sys
b = json.load(sys.stdin)
assert b["duplicate"] is True and b["job_id"] == sys.argv[1], b' "$job_id"
echo "event: duplicate event_id returned the same job (200)"

curl -fsS "${auth[@]}" "${BASE_URL}/adaptation/jobs/${job_id}" | python3 -c '
import json, sys
b = json.load(sys.stdin)
steps = [t["to_status"] for t in b["transitions"]]
assert steps[0] == "RECEIVED" and steps[-1] == "FAILED", steps
print("job transitions:", " -> ".join(steps))'

metrics_text="$(curl -fsS "${BASE_URL}/metrics")"
grep -q '^adaptation_jobs_total' <<<"$metrics_text" \
  || { echo "metrics: adaptation_jobs_total missing after a job" >&2; exit 1; }
grep -E '^adaptation_jobs_total' <<<"$metrics_text"
echo "phase-0-baseline smoke: PASS"
