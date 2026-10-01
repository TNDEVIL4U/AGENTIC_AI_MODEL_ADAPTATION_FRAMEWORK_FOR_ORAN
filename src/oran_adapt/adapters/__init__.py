"""Adapters: the only package allowed to import vendor SDKs (tests/unit/test_import_boundary.py).

Each module exposes ``AdapterSpec`` objects registered as entry points in pyproject.toml under
``oran_adapt.<port>``; oran_adapt.plugins resolves them once, at the composition root
(oran_adapt.bootstrap). Nothing else imports an adapter module directly."""
