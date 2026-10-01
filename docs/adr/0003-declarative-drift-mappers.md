# ADR-0003: Monitoring payloads are mapped to DriftEvents by mapping files

- **Status:** Accepted

## Context

Drift is detected by monitoring systems the framework does not own (Prometheus/Alertmanager, Evidently, in-house detectors), each with its own payload. Asking every one to post the framework's DriftEvent means glue code at every site.

## Decision

A mapper is a TOML mapping file (`config/mappers/*.toml`), not code: where the records are, which count, the path of each DriftEvent field, constants, value maps, splits, an event-id rule and an optional per-feature table. `DRIFT_MAPPERS` names the files; each is served at `POST /api/v1/adaptation/events/from/{name}` and by `oran-adapt event map`. Mapped events go through the same validation and idempotency as `/adaptation/events`.

## Consequences

New monitoring sources need a file, not a release. Mapping files are validated at startup; payloads that do not fit are refused with the failing field (HTTP 422). Arbitrary transformations (arithmetic, joins across payloads) are out of scope: those need a small relay posting DriftEvents.
