"""Member 4 - validation: scores V_current against a candidate model on the same held-out data
(validation.evaluate) and turns that into a pass/fail verdict (validation.engine)."""

from oran_adapt.validation.engine import validate_candidate
from oran_adapt.validation.evaluate import evaluate_model
from oran_adapt.validation.schemas import ValidationReport

__all__ = ["ValidationReport", "evaluate_model", "validate_candidate"]
