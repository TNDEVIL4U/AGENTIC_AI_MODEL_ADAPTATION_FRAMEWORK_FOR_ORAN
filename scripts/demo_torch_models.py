"""Torch model classes for scripts/demo_models.py.

They live in their own importable module rather than in the demo script: adaptation jobs run in
"spawn" worker processes, which re-import the script under another name, so a class defined in
``__main__`` there is not the object pickle expects and the adapted model cannot be saved.
"""

from __future__ import annotations

import torch
from torch import nn


class BeamMLP(nn.Module):
    """Two-class beam-selection classifier (logits out)."""

    def __init__(self) -> None:
        super().__init__()
        self.net = nn.Sequential(nn.Linear(3, 16), nn.ReLU(), nn.Linear(16, 2))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class EnergyMLP(nn.Module):
    """Scalar cell-energy regressor."""

    def __init__(self) -> None:
        super().__init__()
        self.net = nn.Sequential(nn.Linear(3, 16), nn.ReLU(), nn.Linear(16, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)
