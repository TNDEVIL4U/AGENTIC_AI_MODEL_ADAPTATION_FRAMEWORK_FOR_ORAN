"""Row order for temporal models: the time-ordered split and the sliding windows that model
type plugins for sequence models and forecasters build their inputs from.

Rows arrive oldest first (the pipeline's training pool and hold-out are in time order). A
temporal model is never trained on a random split: the newest rows are held back for
validation and every window only looks back in time.
"""

from __future__ import annotations

import numpy as np

from oran_adapt.core.errors import ArtifactError


def time_ordered_split(n: int, fraction: float) -> tuple[range, range]:
    """Positions of the rows to train on and to validate on: the newest ``fraction`` of the
    ``n`` rows (at least one, and never all of them) validate, everything older trains. No row
    is shuffled; with fewer than two rows or a zero fraction nothing is held back."""
    if n < 2 or fraction <= 0:
        return range(n), range(n, n)
    held = min(n - 1, max(1, round(n * fraction)))
    return range(n - held), range(n - held, n)


def sliding_windows(X: np.ndarray, window: int) -> np.ndarray:
    """``(n, window, features)``: window ``i`` holds rows ``i - window + 1`` to ``i``, so each
    prediction reads only the row it is for and the ones before it. The first ``window - 1``
    windows reach before the first row and repeat it there (edge padding), so there is one
    window per row."""
    if window < 1:
        raise ArtifactError("a sequence window must hold at least one row", window=window)
    rows = np.asarray(X, dtype=np.float32)
    if rows.ndim != 2:
        raise ArtifactError("sequence input must be a table of rows", shape=list(rows.shape))
    n = len(rows)
    if n == 0:
        return np.zeros((0, window, rows.shape[1]), dtype=np.float32)
    padded = np.concatenate([np.repeat(rows[:1], window - 1, axis=0), rows])
    positions = np.arange(n)[:, None] + np.arange(window)[None, :]
    return padded[positions]


def training_windows(
    X: np.ndarray, y: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray]:
    """The windows to train on: only those whose history is all real rows (no padding), with
    the target of each window's last row."""
    if len(X) < window:
        raise ArtifactError(
            f"a sequence model with a window of {window} rows needs at least {window} training "
            f"rows, got {len(X)}",
            window=window,
            rows=len(X),
        )
    return sliding_windows(X, window)[window - 1 :], np.asarray(y)[window - 1 :]
