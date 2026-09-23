"""Run a local MLflow tracking + model-registry server (SQLite backend, proxied artifact store)
under ``data/mlflow_server/`` and stop it cleanly on Ctrl+C.

    python scripts/run_mlflow_server.py                 # serve until Ctrl+C
    python scripts/run_mlflow_server.py --port 5001

Point the framework at it with MLFLOW_TRACKING_URI / MLFLOW_REGISTRY_URI = http://127.0.0.1:5000.
``MlflowServer`` is also used as a context manager by scripts/demo_models.py --start-server.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parents[1]


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class MlflowServer:
    def __init__(self, port: int = 5000, data_dir: Path | None = None) -> None:
        self.port = port
        self.data_dir = data_dir or ROOT / "data" / "mlflow_server"
        self.url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout_s: float = 90.0) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artifacts").mkdir(exist_ok=True)
        db = (self.data_dir / "mlflow.db").as_posix()
        cmd = [
            sys.executable, "-m", "mlflow", "server",
            "--backend-store-uri", f"sqlite:///{db}",
            "--artifacts-destination", (self.data_dir / "artifacts").as_posix(),
            "--serve-artifacts",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--workers", "1",
        ]
        log = open(self.data_dir / "server.log", "ab")  # noqa: SIM115 - closed in stop()
        env = {**os.environ, "MLFLOW_DISABLE_AGENT_HINT": "1"}
        if _port_in_use(self.port):
            log.close()
            raise RuntimeError(f"port {self.port} is already in use - not starting a second server")
        # Own process group, so stop() can signal `mlflow server` *and* the uvicorn child it
        # spawns (terminate() alone would orphan that child on Windows).
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=flags,
            start_new_session=os.name != "nt",
        )
        self._log = log
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"mlflow server exited early (code {self.proc.returncode}); "
                    f"see {self.data_dir / 'server.log'}"
                )
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=2) as r:
                    if r.status == 200:
                        return self.url
            except OSError:
                pass
            time.sleep(1)
        self.stop()
        raise RuntimeError(f"mlflow server did not become healthy within {timeout_s:.0f}s")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=20)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        deadline = time.monotonic() + 10
        while _port_in_use(self.port) and time.monotonic() < deadline:
            time.sleep(0.5)
        if getattr(self, "_log", None):
            self._log.close()
            self._log = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    server = MlflowServer(port=args.port)
    try:
        print(f"MLflow server healthy at {server.start()} (Ctrl+C to stop)", flush=True)
        while server.proc is not None and server.proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        print("MLflow server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
