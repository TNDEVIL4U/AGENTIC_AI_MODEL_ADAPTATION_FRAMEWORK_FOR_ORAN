"""Conformance suite for ``ModelHandlerPort`` adapters (native model <-> artifact directory).

Each check takes an adapter and a ``Context`` and raises ConformanceFailure on a deviation::

    ctx = Context(workdir=str(tmp_path), model=fitted, framework="sklearn", X=X,
                  predict=lambda m, X: m.predict(X))
    run(MyHandler(...), ctx)

``model`` is a fitted model of ``framework`` (one the handler declares) and ``predict`` how to
call it on ``X``. Rules: a saved model loads back and predicts the same; the handler detects its
own artifacts and not foreign directories; it never writes into an existing directory; an
undeclared framework is UnsupportedAdaptationError; a directory that holds no loadable model is
ArtifactError.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from oran_adapt.conformance import ConformanceFailure, expect
from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import ModelHandlerPort

_runs = itertools.count(1)
_UNDECLARED = "conformance-no-such-framework"


@dataclass
class Context:
    workdir: str
    model: object
    framework: str
    X: Any
    predict: Callable[[object, Any], Any]
    _run: int = field(default_factory=lambda: next(_runs), repr=False)

    def path(self, label: str) -> str:
        """A path that does not exist yet, under the work directory."""
        return os.path.join(self.workdir, f"handler{self._run:04d}-{label}")


def _raises(action: Callable[[], object], error: type[Exception], what: str) -> None:
    try:
        action()
    except error:
        return
    except Exception as exc:  # any other class is the deviation reported
        raise ConformanceFailure(
            f"{what} must raise {error.__name__}, not {type(exc).__name__}: {exc}") from exc
    raise ConformanceFailure(f"{what} must raise {error.__name__}")


def check_protocol(port: ModelHandlerPort, ctx: Context) -> None:
    expect(isinstance(port, ModelHandlerPort), "does not implement ModelHandlerPort")
    expect(isinstance(port.frameworks, frozenset) and bool(port.frameworks),
           "frameworks must be a non-empty frozenset")
    expect(ctx.framework in port.frameworks, f"the handler does not declare {ctx.framework!r}")
    expect(isinstance(port.format, str) and bool(port.format), "format must be a non-empty str")


def check_roundtrip(port: ModelHandlerPort, ctx: Context) -> None:
    dst = ctx.path("roundtrip")
    saved = port.save(ctx.model, ctx.framework, dst)
    expect(os.path.isdir(saved), "save must return the artifact directory")
    expect(port.detect(saved), "the handler must detect an artifact it saved")
    loaded = port.load(saved, ctx.framework)
    expected = np.asarray(ctx.predict(ctx.model, ctx.X))
    got = np.asarray(ctx.predict(loaded, ctx.X))
    expect(expected.shape == got.shape and np.allclose(expected, got, equal_nan=True),
           "a loaded model must predict what the saved model predicted")


def check_detect_foreign(port: ModelHandlerPort, ctx: Context) -> None:
    empty = ctx.path("empty")
    os.makedirs(empty)
    expect(port.detect(empty) is False, "an empty directory is not this handler's artifact")
    other = ctx.path("other")
    os.makedirs(other)
    with open(os.path.join(other, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write("not a model\n")
    expect(port.detect(other) is False, "a directory of unrelated files is not an artifact")


def check_never_overwrites(port: ModelHandlerPort, ctx: Context) -> None:
    dst = ctx.path("existing")
    os.makedirs(dst)
    marker = os.path.join(dst, "keep.txt")
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("keep")
    _raises(lambda: port.save(ctx.model, ctx.framework, dst), ArtifactError,
            "saving into an existing directory")
    with open(marker, encoding="utf-8") as fh:
        expect(fh.read() == "keep", "a refused save must leave the directory as it was")


def check_undeclared_framework(port: ModelHandlerPort, ctx: Context) -> None:
    expect(_UNDECLARED not in port.frameworks, "test framework name must be undeclared")
    _raises(lambda: port.save(ctx.model, _UNDECLARED, ctx.path("undeclared")),
            UnsupportedAdaptationError, "saving with an undeclared framework")
    empty = ctx.path("undeclared-load")
    os.makedirs(empty)
    _raises(lambda: port.load(empty, _UNDECLARED), UnsupportedAdaptationError,
            "loading with an undeclared framework")


def check_not_loadable(port: ModelHandlerPort, ctx: Context) -> None:
    empty = ctx.path("nothing")
    os.makedirs(empty)
    _raises(lambda: port.load(empty, ctx.framework), ArtifactError,
            "loading a directory that holds no model")


CHECKS: dict[str, Callable[[ModelHandlerPort, Context], None]] = {
    "protocol": check_protocol,
    "roundtrip": check_roundtrip,
    "detect_foreign": check_detect_foreign,
    "never_overwrites": check_never_overwrites,
    "undeclared_framework": check_undeclared_framework,
    "not_loadable": check_not_loadable,
}


def run(port: ModelHandlerPort, ctx: Context) -> list[str]:
    """Run every check in order; returns their names. Stops at the first failure."""
    for check in CHECKS.values():
        check(port, ctx)
    return list(CHECKS)


__all__ = ["CHECKS", "ConformanceFailure", "Context", "run"]
