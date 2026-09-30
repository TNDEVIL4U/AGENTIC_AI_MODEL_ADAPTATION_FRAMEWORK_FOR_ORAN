"""Hardening Phase 8 acceptance: model-type plugins, end to end.

Run by scripts/verify.sh 8 after lint, the import-boundary test and the scoped tests.

1. A small sequence model (an LSTM over sliding windows) completes inspect -> retrain ->
   evaluate -> gate through the model type registry, and the gate accepts the adapted model.
2. An unsupported model (Holt-Winters, served by no plugin) produces the typed
   unsupported_model_type result; the error's recorded form (code, message, context) carries no
   stack trace; a plugin that crashes while inspecting is reported, not raised.
3. No random split on temporal tasks: sequence and forecaster adaptation runs with every
   random reordering disabled, and a spy sees the time-ordered split used.
4. A fixture plugin, installed as a distribution with an entry point, serves a new framework
   with zero core changes.
5. Every built-in plugin passes the model type conformance suite, vendor SDKs stay inside their
   adapters, and every plugin is documented in docs/adapters/model_type.md (LightGBM, CatBoost
   and Keras run against test doubles; onnxruntime is not installed - all unverified locally).

Checks 1-4 are pytest selections over tests/unit/test_phase8_model_types.py, which drive the
real registry, engines and plugins on small in-memory data. Exit status 0 means every check
passed.
"""

from __future__ import annotations

import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests" / "unit"
sys.path.insert(0, str(TESTS))  # the phase 8 test helpers and the import-boundary check

PHASE8 = TESTS / "test_phase8_model_types.py"


def _run(selection: str) -> None:
    import pytest

    code = pytest.main(["-q", "-p", "no:cacheprovider", "-W", "ignore", str(PHASE8), "-k",
                        selection])
    assert code == 0, f"pytest -k {selection!r} failed with exit code {code}"


def sequence_model_end_to_end(tmp: Path) -> str:
    _run("test_sequence_model_inspect_retrain_evaluate_gate")
    return "LSTM: inspect (temporal, window 8) -> retrain -> fine-tune -> evaluate -> gate ACCEPT"


def unsupported_model_typed(tmp: Path) -> str:
    from oran_adapt.adaptation.model_types import build_model_types
    from oran_adapt.core.config import Settings
    from oran_adapt.core.errors import UnsupportedModelTypeError

    types = build_model_types(Settings(_env_file=None))
    result = types.inspect(object(), "holt-winters")
    assert getattr(result, "kind", None) == "unsupported_model_type", result
    try:
        types.require(object(), "holt-winters")
    except UnsupportedModelTypeError as exc:
        recorded = exc.to_dict()
    else:
        raise AssertionError("require() accepted a model no plugin serves")
    assert recorded["code"] == "UNSUPPORTED_MODEL_TYPE", recorded
    assert "Traceback" not in repr(recorded), recorded
    _run("holt_winters or typed_result or crashes_while_inspecting")
    return f"typed result {result.kind}; recorded error code {recorded['code']}, no traceback"


def no_random_split_on_temporal(tmp: Path) -> str:
    _run("no_random_split or time_order or shuffles_a_temporal or time_ordered_split")
    return ("sequence and forecaster plugins adapt with shuffling disabled; "
            "time_ordered_split holds out the newest rows")


def fixture_plugin_zero_core_change(tmp: Path) -> str:
    _run("fixture_plugin_needs_no_core_change or template_plugin")
    return "entry-point fixture plugin 'meanfw' and the template plugin served without core edits"


def conformance_boundaries_docs(tmp: Path) -> str:
    from test_import_boundary import violations

    from oran_adapt import plugins

    found = violations()
    assert not found, f"vendor imports outside their adapters: {found}"
    guide = (ROOT / "docs" / "adapters" / "model_type.md").read_text(encoding="utf-8")
    names = plugins.adapters("model_type")
    undocumented = [a for a in names if f"`{a}`" not in guide]
    assert not undocumented, f"not in docs/adapters/model_type.md: {undocumented}"
    _run("builtin_model_type_conformance or conformance_catches or no_vendor_sdk")
    return (f"{len(names)} plugins conformant and documented; LightGBM, CatBoost and Keras "
            "against test doubles, ONNX with the reference evaluator (unverified locally)")


CHECKS: list[tuple[str, Callable[[Path], str]]] = [
    ("small sequence model: inspect -> retrain -> evaluate -> gate", sequence_model_end_to_end),
    ("unsupported model: typed error, no stack trace", unsupported_model_typed),
    ("no random split on temporal tasks", no_random_split_on_temporal),
    ("fixture plugin with zero core changes", fixture_plugin_zero_core_change),
    ("conformance, boundaries and documentation", conformance_boundaries_docs),
]


def main() -> int:
    failed = 0
    for name, check in CHECKS:
        with tempfile.TemporaryDirectory(prefix="oran-phase8-", ignore_cleanup_errors=True) as tmp:
            started = time.monotonic()
            try:
                detail = check(Path(tmp))
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {name}\n      {exc}", flush=True)
            else:
                print(f"PASS  {name} ({time.monotonic() - started:.0f}s)\n      {detail}",
                      flush=True)
    print(f"\nphase 8 acceptance: {len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
