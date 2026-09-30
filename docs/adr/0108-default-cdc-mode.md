# ADR-0108: Default CDC_MODE is `disabled`

- **Status:** Accepted
- **Selector:** `CDC_MODE`
- **Default:** `disabled`
- **Alternatives shipped:** `kafka`, `polling`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#dataset-adapters)

## Context

New KPI rows can be picked up as they are written (change data capture). Whether a Kafka/Debezium pipeline exists, or only the database, is unknown.

## Decision

`disabled`: data arrives with events or through dataset adapters. `polling` (a trigger plus an outbox table, generated for review) and `kafka` (Debezium) ship.

## Consequences

No DDL is applied to an operational database without review, and nothing runs that the site did not ask for.

## Revisit when

The site wants continuous ingestion and says which pipeline it runs.
