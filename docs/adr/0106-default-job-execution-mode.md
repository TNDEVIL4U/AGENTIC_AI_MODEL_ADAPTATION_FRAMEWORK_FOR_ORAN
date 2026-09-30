# ADR-0106: Default JOB_EXECUTION_MODE is `process`

- **Status:** Accepted
- **Selector:** `JOB_EXECUTION_MODE`
- **Default:** `process`
- **Alternatives shipped:** `thread`
- **Open question:** [OPEN-QUESTIONS.md](../OPEN-QUESTIONS.md#job-queue-adapters)

## Context

An adaptation attempt can run long or hang in a native library; the worker must be able to stop it.

## Decision

`process`: each attempt runs in a child process with a hard timeout, and the whole process tree is killed on expiry. `thread` exists for tests and debugging.

## Consequences

Timeouts are enforced for real at the cost of a process start per attempt.

## Revisit when

Settled; revisit only if process start-up becomes the bottleneck.
