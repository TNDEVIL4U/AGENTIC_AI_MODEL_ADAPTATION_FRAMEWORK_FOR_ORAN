"""Every installed model handler behind one ``ModelHandlerPort``.

Loading goes to the handler that recognises the artifact directory (``detect``), so versions
saved in any installed format keep loading after MODEL_FORMAT changes. Saving always uses the
handler MODEL_FORMAT names. Built by ``oran_adapt.bootstrap.build_model_handler``.
"""

from __future__ import annotations

from collections.abc import Mapping

from oran_adapt.core.errors import ArtifactError, UnsupportedAdaptationError
from oran_adapt.ports import ModelHandlerPort


class ModelHandlers:
    def __init__(self, handlers: Mapping[str, ModelHandlerPort], save_with: str) -> None:
        if save_with not in handlers:
            raise UnsupportedAdaptationError(
                f"no model handler named {save_with!r} is installed", installed=sorted(handlers)
            )
        self.handlers = dict(handlers)
        self.save_with = save_with

    @property
    def frameworks(self) -> frozenset[str]:
        return self.handlers[self.save_with].frameworks

    @property
    def format(self) -> str:
        return self.handlers[self.save_with].format

    def detect(self, local_path: str) -> bool:
        return any(h.detect(local_path) for h in self.handlers.values())

    def save(self, model: object, framework: str, dst_dir: str) -> str:
        return self.handlers[self.save_with].save(model, framework, dst_dir)

    def load(self, local_path: str, framework: str) -> object:
        recognised = [name for name, h in self.handlers.items() if h.detect(local_path)]
        if not recognised:
            raise ArtifactError(
                "no installed model handler recognises the artifact",
                path=local_path,
                handlers=sorted(self.handlers),
            )
        for name in recognised:
            if framework.lower() in self.handlers[name].frameworks:
                return self.handlers[name].load(local_path, framework)
        raise UnsupportedAdaptationError(
            f"no model handler loads {framework!r} from this artifact",
            path=local_path,
            recognised_by=recognised,
        )
