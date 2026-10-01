# ADR-0107: Default JOB_QUEUE_BACKEND is `database`

- **Status:** Accepted
- **Selector:** `JOB_QUEUE_BACKEND`
- **Default:** `database`
- **Alternatives shipped:** `celery`, `inline`, `kubernetes`, `rq`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#job-queue-adapters)

## Context

Jobs must survive restarts and be claimed by workers. Whether a broker (RabbitMQ, Redis) or a Kubernetes cluster is available to wake workers is unknown.

## Decision

`database`: workers poll the job table with a row lock (`SELECT ... FOR UPDATE`); no broker is needed. `celery`, `rq` and `kubernetes` wake workers through a broker or a Job; `inline` is for development only and refused in production.

## Consequences

One dependency fewer (the database is already required). Pickup latency is the poll interval; a broker lowers it.

## Revisit when

A broker is provided and pickup latency matters.
