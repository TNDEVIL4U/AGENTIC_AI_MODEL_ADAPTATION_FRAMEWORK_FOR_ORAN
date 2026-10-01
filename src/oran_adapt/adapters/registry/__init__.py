"""Model registry and artifact store adapters (ports ``registry`` and ``artifact_store``).

Each registry adapter passes ``oran_adapt.conformance.registry``. The MLflow SDK is imported
only under ``oran_adapt.adapters.registry.mlflow`` (tests/unit/test_import_boundary.py)."""
