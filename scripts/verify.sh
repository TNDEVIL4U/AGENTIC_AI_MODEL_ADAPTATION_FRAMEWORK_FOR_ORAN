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
    1) scope="core api ports adapters plugins" ;;
    2) scope="core db datastore cdc api" ;;
    3) scope="registry adaptation adapters api" ;;
    4) scope="sandbox adaptation" ;;
    5) scope="analysis decision" ;;
    6) scope="orchestrator api" ;;
    7) scope="validation registry" ;;
    8) scope="llm adapters" ;;
    9) scope="api core" ;;
    10) scope="cdc datastore" ;;
    11) scope="core api orchestrator" ;;
    12) scope="core api" ;;
    13) scope="api core orchestrator" ;;
    14) scope="core api orchestrator registry" ;;
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
"$py" -m pytest -q -m "not heavy" -n "$workers" --ff "${scoped[@]}"
step "4/5 smoke tier"
"$py" -m pytest -q -m smoke -n "$workers" --ff

step "5/5 acceptance ($acceptance)"
"$py" "$acceptance"

elapsed=$((SECONDS - start))
if (( elapsed > BUDGET_S )); then
    echo "verify phase $phase: checks passed but took ${elapsed}s (> ${BUDGET_S}s budget)" >&2
    exit 1
fi
echo
echo "verify phase $phase: PASS in ${elapsed}s"
