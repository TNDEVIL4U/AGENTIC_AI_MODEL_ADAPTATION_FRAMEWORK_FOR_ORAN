# ADR-0001: Ports and adapters for every unknown stack choice

- **Status:** Accepted

## Context

The target deployment's stack (registry, serving, queue, identity, ...) was not fixed when the framework was hardened, and the pilot hardcoded MLflow and local files.

## Decision

Every external dependency is a port (`oran_adapt.ports`) with adapters registered as entry points (`oran_adapt.<port>` in `pyproject.toml`). A selector key picks the adapter; each adapter declares a `Capability` (features, config keys, required keys, production keys, packages). Core code never branches on a vendor, and vendor SDKs are imported only under `oran_adapt/adapters` (checked by the import-boundary step of `scripts/verify.sh`).

## Consequences

Adding a stack is adding an adapter, not editing core; `docs/capability-matrix.md` is generated from the descriptors. Every port ships at least two adapters, a conformance suite and an authoring path (`docs/adapter-authoring.md`).
