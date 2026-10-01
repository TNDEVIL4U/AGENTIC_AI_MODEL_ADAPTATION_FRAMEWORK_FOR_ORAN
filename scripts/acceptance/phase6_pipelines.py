"""Pipeline stand-ins for scripts/acceptance/phase6.py. They live in their own module so a
job worker process (JOB_EXECUTION_MODE=process) can import them by name."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from oran_adapt.orchestrator.schemas import JobResult


def tree_pipeline(session, event, settings, *, registry, llm_client, workdir):
    """Starts a grandchild process, records both pids in ``workdir/pids``, then beats for two
    minutes: work that only stops if its whole process tree is killed."""
    os.makedirs(workdir, exist_ok=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(workdir, "pids").write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
    beat = Path(workdir, "heartbeat")
    for i in range(1200):
        beat.write_text(str(i), encoding="utf-8")
        time.sleep(0.1)
    Path(workdir, "finished").write_text("done", encoding="utf-8")
    return JobResult(model_id=event.model_id, outcome="NO_ACTION", reason="ran to the end")


def ok_pipeline(session, event, settings, *, registry, llm_client, workdir):
    return JobResult(model_id=event.model_id, outcome="NO_ACTION", reason="ok")
