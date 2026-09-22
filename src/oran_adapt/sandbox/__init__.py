"""Member 3 - sandbox: isolates LLM-generated adaptation code from the rest of the pipeline via
static AST security checks (sandbox.security) and subprocess execution with resource limits
(sandbox.runner)."""

from oran_adapt.sandbox.runner import run_in_sandbox
from oran_adapt.sandbox.security import check_code_safety

__all__ = ["check_code_safety", "run_in_sandbox"]
