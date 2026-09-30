#!/usr/bin/env bash
# Local phase gate:  scripts/verify.sh <phase>   (alias: make verify PHASE=<phase>)
#
# A hardening phase is complete when this passes locally. Fails fast, in order:
#   1. ruff + mypy (the mypy baseline is 0 errors, so any error is a new one)
#   2. import-boundary test (no vendor SDK imports outside oran_adapt.adapters)
#   3. no-gaps lint (TODO/FIXME, NotImplementedError, bare except, stub bodies)
#   4. pytest -m "not heavy" on the test files that import the packages this phase touches,
#      then the full smoke tier
#   5. the phase acceptance script scripts/acceptance/phase<N>.py
# The whole run has a 300 s budget; exceeding it fails the gate (move slow tests to `heavy`).
#
# Resource limits (CLAUDE.md): pytest-xdist is capped at 2 workers; override with VERIFY_WORKERS.
set -euo pipefail

BUDGET_S=300
phase="${1:-}"
if [[ -z "$phase" ]]; then
    echo "usage: scripts/verify.sh <phase 1-15>" >&2
    exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [[ -n "${PYTHON:-}" ]]; then
    py="$PYTHON"
elif [[ -x .venv/Scripts/python.exe ]]; then
    py=.venv/Scripts/python.exe
elif [[ -x .venv/bin/python ]]; then
    py=.venv/bin/python
else
    py=python3
fi
workers="${VERIFY_WORKERS:-2}"
if (( workers > 2 )); then
    echo "VERIFY_WORKERS=$workers exceeds the 2-worker cap" >&2
    exit 2
fi

# Packages (under oran_adapt) each phase touches; step 4 runs the tests importing any of them.
case "$phase" in
    1) scope="core api ports adapters plugins" ;;        # hexagonal core and configuration
    2) scope="registry adapters ports bootstrap" ;;       # model registry abstraction
    3) scope="registry orchestrator adapters ports" ;;    # deployment and serving propagation
    4) scope="orchestrator adapters api notifications" ;; # outbound notifications
    5) scope="datastore cdc analysis adaptation" ;;       # data access by reference
    6) scope="orchestrator api" ;;                        # real execution layer
    7) scope="validation registry orchestrator" ;;        # validation gate, progressive delivery
    8) scope="adaptation.model_types adaptation.sequence adaptation.torch_engine adaptation.inspector validation conformance.model_types adapters.model_types" ;;  # model-type plugins
    9) scope="api.security api.ratelimit adapters.auth adapters.jwt adapters.vault adapters.access core.outbound" ;;  # security
    10) scope="llm adapters.llm_providers conformance.llm decision.llm_selector" ;;  # LLM paths
    11) scope="core.liveness db.migrate" ;;  # packaging: liveness, schema status, migrations
    12) scope="core api orchestrator" ;;                  # observability
    13) scope="core api orchestrator registry" ;;         # test and conformance consolidation
    14) scope="core api" ;;                               # documentation, migration, samples
    15) scope="core api orchestrator registry sandbox" ;;
    *) echo "unknown phase '$phase' (expected 1-15)" >&2; exit 2 ;;
esac

acceptance="scripts/acceptance/phase${phase}.py"
if [[ ! -f "$acceptance" ]]; then
    echo "missing acceptance script $acceptance" >&2
    exit 1
fi

# CPU only, and no LLM calls from the gate (tests that need one inject a fake).
export CUDA_VISIBLE_DEVICES="" LLM_PROVIDER=none

start=$SECONDS
step() { printf '\n== [%3ds] %s\n' "$((SECONDS - start))" "$*"; }

step "1/5 ruff"
"$py" -m ruff check src tests scripts
step "1/5 mypy"
"$py" -m mypy

step "2/5 import boundary"
"$py" -m pytest -q -p no:cacheprovider tests/unit/test_import_boundary.py

step "3/5 no-gaps lint"
"$py" scripts/lint_no_gaps.py

step "4/5 scoped tests (phase $phase: $scope)"
pattern="oran_adapt\\.($(tr ' ' '|' <<<"$scope"))\\b"
mapfile -t scoped < <(grep -rlE --include='test_*.py' "$pattern" tests | sort)
if (( ${#scoped[@]} == 0 )); then
    echo "no tests import packages [$scope]; a phase must be covered by tests" >&2
    exit 1
fi
printf '  %s\n' "${scoped[@]}"
# The acceptance script reads this report instead of re-running the same tests (_gate.py).
mkdir -p .pytest_cache/oran-verify
export ORAN_GATE_JUNIT="$root/.pytest_cache/oran-verify/scoped.xml"
export ORAN_GATE_STARTED="$(date +%s)"
"$py" -m pytest -q -m "not heavy" -n "$workers" --ff --junitxml "$ORAN_GATE_JUNIT" "${scoped[@]}"
step "4/5 smoke tier (files not run above)"
# The scoped run already ran the smoke tests in its own files; run the rest of the tier.
ignored=()
for f in "${scoped[@]}"; do ignored+=(--ignore "$f"); done
"$py" -m pytest -q -m smoke -n "$workers" --ff "${ignored[@]}"

step "5/5 acceptance ($acceptance)"
"$py" "$acceptance"

elapsed=$((SECONDS - start))
if (( elapsed > BUDGET_S )); then
    echo "verify phase $phase: checks passed but took ${elapsed}s (> ${BUDGET_S}s budget)" >&2
    exit 1
fi
echo
echo "verify phase $phase: PASS in ${elapsed}s"
