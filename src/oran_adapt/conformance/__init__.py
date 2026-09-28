"""Conformance suites: the behaviour every adapter of a port must show, as plain functions a
test (ours or a third-party adapter's) runs against an adapter instance."""

from __future__ import annotations


class ConformanceFailure(AssertionError):
    """An adapter behaved differently from what its port promises."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise ConformanceFailure(message)
