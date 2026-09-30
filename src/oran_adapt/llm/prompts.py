"""Versioned system prompts. Every LLM call names a prompt by id and version, and the job
record stamps both with the prompt's SHA-256, so a decision can always be traced to the exact
text that produced it.

Built-in prompts live in BUILTIN. ``LLM_PROMPT_DIR`` adds versions from files named
``<id>@<version>.txt`` (a file may also replace a built-in version, and its hash shows it), and
``LLM_PROMPT_VERSIONS`` picks the version per id. Without a choice the newest version is used:
versions are compared as dotted numbers when they are numeric, otherwise as text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from oran_adapt.core.errors import ConfigurationError

if TYPE_CHECKING:
    from oran_adapt.core.config import Settings

STRATEGY_SELECTION = "strategy-selection"
ADAPTATION_CODE = "adaptation-code"
_FILE_SUFFIX = ".txt"
_SEPARATOR = "@"


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.system.encode("utf-8")).hexdigest()

    @property
    def ref(self) -> str:
        return f"{self.id}{_SEPARATOR}{self.version}"

    def stamp(self) -> dict[str, str]:
        return {"prompt_id": self.id, "prompt_version": self.version,
                "prompt_sha256": self.sha256}


BUILTIN: tuple[Prompt, ...] = (
    Prompt(STRATEGY_SELECTION, "1", (
        "You are the strategy-selection component of an O-RAN model adaptation pipeline. "
        "You are given evidence that a deployed model has drifted and a closed list of "
        "strategies that are technically compatible with this situation. Choose exactly one "
        "strategy from that list and justify it briefly using the evidence given. "
        "Respond with ONLY a single JSON object, no markdown fences, no prose outside the JSON, "
        'matching this shape: {"strategy": "<one of the compatible strategies>", '
        '"confidence": <float 0..1>, "rationale": "<one or two sentences>"}.'
    )),
    Prompt(ADAPTATION_CODE, "1", (
        "You write Python adaptation code for an O-RAN model-adaptation pipeline. "
        "Define exactly one top-level function `adapt(current_model, X, y)` that returns a "
        "trained model object usable the same way as current_model, trained on X (a pandas "
        "DataFrame of features) and y (a pandas Series of the target). Continue training "
        "current_model in place when that is reasonable for its framework; otherwise fit a fresh "
        "model of the same kind. "
        "You may only import from: numpy, pandas, sklearn, xgboost, torch, math, json. "
        "Never use eval, exec, compile, __import__, open, input, os, sys, subprocess, socket, or "
        "any dunder attribute such as __globals__ or __subclasses__, no str.format (use "
        "f-strings), no file reads or writes, and no dataset downloads - the code runs in a "
        "restricted sandbox that rejects all of these before execution. "
        "Respond with ONLY the Python code defining `adapt`, no markdown fences, no prose."
    )),
)


def _order(version: str) -> tuple[int, tuple[int, ...], str]:
    parts = version.split(".")
    if all(p.isdigit() for p in parts):
        return (1, tuple(int(p) for p in parts), version)
    return (0, (), version)


def _from_dir(directory: str) -> list[Prompt]:
    root = Path(directory)
    if not root.is_dir():
        raise ConfigurationError("LLM_PROMPT_DIR is not a directory", key="LLM_PROMPT_DIR",
                                 path=str(root))
    found = []
    for path in sorted(root.glob(f"*{_SEPARATOR}*{_FILE_SUFFIX}")):
        prompt_id, _, version = path.name.removesuffix(_FILE_SUFFIX).partition(_SEPARATOR)
        text = path.read_text(encoding="utf-8").strip()
        if not prompt_id or not version or not text:
            raise ConfigurationError("a prompt file needs an id, a version and some text",
                                     key="LLM_PROMPT_DIR", file=path.name)
        found.append(Prompt(prompt_id, version, text))
    return found


def available(settings: Settings | None = None) -> dict[str, dict[str, Prompt]]:
    """Every prompt version by id: the built-ins, then LLM_PROMPT_DIR's files over them."""
    table: dict[str, dict[str, Prompt]] = {}
    extra = _from_dir(settings.llm_prompt_dir) if settings and settings.llm_prompt_dir else []
    for prompt in (*BUILTIN, *extra):
        table.setdefault(prompt.id, {})[prompt.version] = prompt
    return table


def get_prompt(prompt_id: str, settings: Settings | None = None) -> Prompt:
    """The version of ``prompt_id`` LLM_PROMPT_VERSIONS selects, else the newest one."""
    versions = available(settings).get(prompt_id)
    if not versions:
        raise ConfigurationError(f"no prompt named {prompt_id!r}", key="LLM_PROMPT_VERSIONS")
    chosen = settings.llm_prompt_versions.get(prompt_id) if settings else None
    if chosen is None:
        return versions[max(versions, key=_order)]
    if chosen not in versions:
        raise ConfigurationError(
            f"LLM_PROMPT_VERSIONS asks for {prompt_id}{_SEPARATOR}{chosen}, which does not exist",
            key="LLM_PROMPT_VERSIONS", available=sorted(versions, key=_order),
        )
    return versions[chosen]
