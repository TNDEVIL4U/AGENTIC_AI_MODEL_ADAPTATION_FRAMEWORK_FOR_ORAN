"""Member 3 - sandbox execution: runs security-checked adaptation code in an isolated
subprocess with a wall-clock timeout and a resident-memory watchdog, and hands
inputs/outputs across the process boundary via pickle files rather than a shared object graph -
the point of a sandbox is that a compromised or merely buggy script's blast radius stops at the
subprocess, never touching this process's memory, environment or open handles.

This is the "restricted subprocess" backend the Phase 0 audit documents as weaker than Docker
isolation: no filesystem/network namespace and no cgroup. Its memory limit is a soft one - the
parent polls the child's resident memory (Linux and Windows; not enforced on other platforms) and
kills it once it passes `memory_mb`, so a very fast spike can briefly overshoot. An address-space
rlimit is not used: numpy/OpenBLAS/torch reserve far more address space than they touch, so any
useful ceiling fails their import before user code runs. The Docker backend is the hard limit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid

import joblib
import pandas as pd

from oran_adapt.core.errors import SandboxExecutionError

# The inputs are pickled by this (trusted) process, so unpickling them inside the sandbox is
# safe. The output travels the other way: it is written by untrusted code, so it is saved in a
# format that cannot execute code when loaded (skops, xgboost's UBJSON, safetensors) plus a small
# JSON manifest, and _load_output reads it back with the matching safe loader. The parent never
# unpickles anything the sandbox wrote.
_SCRIPT_TEMPLATE = """\
import joblib

inputs = joblib.load({inputs_path!r})
current_model = inputs["current_model"]
X = inputs["X"]
y = inputs["y"]

{code}

def _oran_write_output(result, out_dir):
    import json
    import os
    import sys

    torch = sys.modules.get("torch")
    if torch is not None and isinstance(result, torch.nn.Module):
        from safetensors.torch import save_file

        tensors = {{k: v.detach().cpu().contiguous() for k, v in result.state_dict().items()}}
        save_file(tensors, os.path.join(out_dir, "output.safetensors"))
        fmt = "torch_state_dict"
    elif type(result).__module__.split(".")[0] == "xgboost":
        result.save_model(os.path.join(out_dir, "output.ubj"))
        fmt = "xgboost"
    else:
        import skops.io as sio

        sio.dump(result, os.path.join(out_dir, "output.skops"))
        fmt = "skops"
    with open(os.path.join(out_dir, "output.json"), "w", encoding="utf-8") as f:
        json.dump({{"format": fmt, "class": type(result).__name__}}, f)

_oran_write_output(adapt(current_model, X, y), {output_dir!r})
"""

_OUTPUT_FILES = {
    "skops": "output.skops",
    "xgboost": "output.ubj",
    "torch_state_dict": "output.safetensors",
}
_XGBOOST_CLASSES = ("XGBClassifier", "XGBRegressor", "XGBRanker", "Booster")
_MANIFEST_MAX_BYTES = 4096

# Only what the interpreter and its installed packages actually need to start correctly - never
# the parent's full environment, which may hold API keys, DB credentials, etc. VIRTUAL_ENV and
# PYTHONPATH cover venv package resolution; APPDATA/USERPROFILE/LOCALAPPDATA cover Python's user
# site-packages directory (site.getusersitepackages()), which some dependencies here are
# installed into rather than the venv.
_INHERITED_ENV_VARS = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "TEMP",
    "TMP",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "PYTHONPATH",
    "APPDATA",
    "USERPROFILE",
    "LOCALAPPDATA",
)


# One BLAS/OpenMP thread: each extra thread allocates its own buffers, which on a many-core host
# inflates the sandbox's memory for no benefit on the small fits it runs.
_SINGLE_THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
}


def _sandbox_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _INHERITED_ENV_VARS}
    env.update(_SINGLE_THREAD_ENV)
    return env


_MEMORY_POLL_S = 0.05

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TH32CS_SNAPPROCESS = 0x2
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def _parent_map() -> dict[int, int]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == _INVALID_HANDLE:
            return {}
        try:
            parents: dict[int, int] = {}
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return parents
        finally:
            _kernel32.CloseHandle(snapshot)

    def _terminate(pid: int) -> None:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                _kernel32.TerminateProcess(handle, 1)
            finally:
                _kernel32.CloseHandle(handle)

    def _rss_bytes(pid: int) -> int | None:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not _kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)
        finally:
            _kernel32.CloseHandle(handle)

elif sys.platform.startswith("linux"):
    import signal

    def _parent_map() -> dict[int, int]:
        parents: dict[int, int] = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as f:
                    # "pid (comm) state ppid ..." - comm may itself contain spaces or ")".
                    fields = f.read().rpartition(")")[2].split()
                parents[int(name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
        return parents

    def _terminate(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _rss_bytes(pid: int) -> int | None:
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return None
        return None

else:

    def _parent_map() -> dict[int, int]:
        return {}

    def _terminate(pid: int) -> None:
        return None

    def _rss_bytes(pid: int) -> int | None:
        return None  # no portable RSS source without a new dependency: limit not enforced


def _descendants(root: int) -> list[int]:
    """Every live process below ``root``. On Windows a venv's python.exe is a launcher that runs
    the real interpreter as its child, so the sandbox's memory lives one level down."""
    children: dict[int, list[int]] = {}
    for pid, ppid in _parent_map().items():
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    queue = list(children.get(root, []))
    while queue:
        pid = queue.pop()
        found.append(pid)
        queue.extend(children.get(pid, []))
    return found


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill ``proc`` and everything it started: killing only a launcher would leave the real
    interpreter running and holding the output pipes open."""
    for pid in _descendants(proc.pid):
        _terminate(pid)
    proc.kill()


def _watch_memory(
    proc: subprocess.Popen[str], limit_bytes: int, stop: threading.Event, tripped: list[int]
) -> None:
    """Kill ``proc``'s tree once its total resident memory passes ``limit_bytes``; record it."""
    while not stop.wait(_MEMORY_POLL_S):
        if proc.poll() is not None:
            return  # exited: its pid may already belong to another process
        readings = [_rss_bytes(pid) for pid in (proc.pid, *_descendants(proc.pid))]
        rss = sum(r for r in readings if r is not None)
        if rss > limit_bytes:
            tripped.append(rss)
            _kill_tree(proc)
            return


def _trusted(types: tuple[str, ...] | list[str] | None) -> tuple[str, ...] | list[str]:
    """``types``, or else the schema default of MLFLOW_SKOPS_TRUSTED_TYPES (its one definition)."""
    if types is not None:
        return types
    from oran_adapt.core.config import Settings

    default: list[str] = Settings.model_fields["mlflow_skops_trusted_types"].get_default(
        call_default_factory=True
    )
    return default


def _load_output(
    workdir: str, current_model: object, skops_trusted_types: tuple[str, ...] | list[str]
) -> object:
    """Read back the model the sandbox produced, using only loaders that cannot execute code.
    Raises SandboxExecutionError when the output is missing, malformed, or needs a type that is
    not trusted."""
    manifest_path = os.path.join(workdir, "output.json")
    if not os.path.exists(manifest_path):
        raise SandboxExecutionError("sandbox script produced no output artifact")
    if os.path.getsize(manifest_path) > _MANIFEST_MAX_BYTES:
        raise SandboxExecutionError("sandbox output manifest is too large")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        fmt, cls = manifest["format"], manifest["class"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SandboxExecutionError("sandbox output manifest is malformed", cause=str(exc)) from exc
    if fmt not in _OUTPUT_FILES:
        raise SandboxExecutionError("sandbox output has an unknown format", format=str(fmt))
    path = os.path.join(workdir, _OUTPUT_FILES[fmt])
    if not os.path.exists(path):
        raise SandboxExecutionError("sandbox output artifact is missing", format=fmt)

    try:
        if fmt == "skops":
            import skops.io as sio

            untrusted = sio.get_untrusted_types(file=path)
            refused = sorted(set(untrusted) - set(skops_trusted_types))
            if refused:
                raise SandboxExecutionError(
                    "sandbox output needs types that are not trusted", types=refused
                )
            return sio.load(path, trusted=untrusted)
        if fmt == "xgboost":
            import xgboost

            if cls not in _XGBOOST_CLASSES:
                raise SandboxExecutionError("sandbox output is not an xgboost model", cls=cls)
            model = getattr(xgboost, cls)()
            model.load_model(path)
            return model
        # torch_state_dict: weights only, loaded into a copy of the current architecture.
        import copy

        import torch
        from safetensors.torch import load_file

        if not isinstance(current_model, torch.nn.Module):
            raise SandboxExecutionError(
                "sandbox returned torch weights but the current model is not a torch module"
            )
        model = copy.deepcopy(current_model)
        model.load_state_dict(load_file(path), strict=True)
        return model
    except SandboxExecutionError:
        raise
    except Exception as exc:
        raise SandboxExecutionError(
            "sandbox output artifact could not be loaded", format=fmt, cause=str(exc)
        ) from exc


def run_in_sandbox(
    code: str,
    *,
    current_model: object,
    X: pd.DataFrame,
    y: pd.Series,
    timeout_s: int,
    memory_mb: int,
    workdir: str,
    skops_trusted_types: tuple[str, ...] | list[str] | None = None,
) -> object:
    """Runs ``code`` (which must define ``adapt(current_model, X, y) -> model``) in a fresh
    subprocess and returns the model it produced. Raises SandboxExecutionError on timeout,
    resource-limit violation, non-zero exit, or a missing/unreadable/untrusted result - never
    lets a subprocess failure surface as a raw OSError/TimeoutExpired to the caller."""
    os.makedirs(workdir, exist_ok=True)
    inputs_path = os.path.join(workdir, "inputs.joblib")
    script_path = os.path.join(workdir, "script.py")

    joblib.dump({"current_model": current_model, "X": X, "y": y}, inputs_path)
    script = _SCRIPT_TEMPLATE.format(
        code=code, inputs_path=inputs_path, output_dir=os.path.abspath(workdir)
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    proc = subprocess.Popen(
        # -P: the workdir is not put on sys.path, so a planted module cannot shadow imports.
        [sys.executable, "-P", script_path],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_sandbox_env(),
    )
    stop = threading.Event()
    tripped: list[int] = []
    watchdog = threading.Thread(
        target=_watch_memory,
        args=(proc, memory_mb * 1024 * 1024, stop, tripped),
        name="sandbox-memory-watchdog",
        daemon=True,
    )
    watchdog.start()
    try:
        _, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        proc.communicate()
        raise SandboxExecutionError(
            "sandbox execution exceeded the timeout", timeout_s=timeout_s
        ) from exc
    finally:
        stop.set()
        watchdog.join()

    if tripped:
        raise SandboxExecutionError(
            "sandbox execution exceeded the memory limit",
            memory_mb=memory_mb,
            rss_mb=tripped[0] // (1024 * 1024),
        )
    if proc.returncode != 0:
        raise SandboxExecutionError(
            "sandbox script exited with a non-zero status",
            returncode=proc.returncode,
            stderr=stderr[-4000:],
        )

    return _load_output(workdir, current_model, _trusted(skops_trusted_types))


def _docker_user() -> list[str]:
    """Run the container as the host user that owns the bind-mounted workdir, so the sandbox
    can write its output without being root. Docker Desktop (Windows/macOS) maps bind-mount
    ownership itself; there the image's own non-root USER applies."""
    if hasattr(os, "getuid"):
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    return []


def run_in_docker(
    code: str,
    *,
    current_model: object,
    X: pd.DataFrame,
    y: pd.Series,
    timeout_s: int,
    memory_mb: int,
    workdir: str,
    image: str,
    skops_trusted_types: tuple[str, ...] | list[str] | None = None,
) -> object:
    """The Phase 0 audit's "stronger isolation" backend: a real container boundary (its own
    filesystem and network namespace, an enforced memory cgroup on every OS - not just POSIX -
    and no shared PID namespace with this process) instead of the subprocess backend's bare
    resource limits. Same contract as `run_in_sandbox` - same script template, same input/output
    hand-off via files in `workdir` - just executed by `docker run` against `image` instead of a
    local Python subprocess.

    Requires the `docker` CLI and a running daemon on the host. Neither is available on the
    machine this was written on (confirmed via `docker --version` returning "command not
    found"), so this function is written in full per the Phase 0 audit's documented plan but has
    never actually been executed - only `tests/integration/test_docker_sandbox.py`, which skips
    itself when `docker` is absent, exercises it, and only when Docker is installed."""
    os.makedirs(workdir, exist_ok=True)
    inputs_path = os.path.join(workdir, "inputs.joblib")
    script_path = os.path.join(workdir, "script.py")

    joblib.dump({"current_model": current_model, "X": X, "y": y}, inputs_path)
    script = _SCRIPT_TEMPLATE.format(
        code=code, inputs_path="/sandbox/inputs.joblib", output_dir="/sandbox"
    )
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    container = f"oran-sandbox-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container,
        "--network",
        "none",
        "--memory",
        f"{memory_mb}m",
        "--memory-swap",
        f"{memory_mb}m",
        "--pids-limit",
        "128",
        "--cpus",
        "1",
        "--read-only",
        "--tmpfs",
        # The container's own private in-memory /tmp, not a host temp path.
        "/tmp:rw,noexec,nosuid,size=64m",  # nosec B108
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *_docker_user(),
        "-v",
        f"{os.path.abspath(workdir)}:/sandbox",
        "-w",
        "/sandbox",
        image,
        "python",
        "-P",
        "script.py",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        # Killing the docker CLI does not stop the container: remove it explicitly.
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, timeout=60, check=False
        )
        raise SandboxExecutionError(
            "docker sandbox execution exceeded the timeout", timeout_s=timeout_s
        ) from exc
    except OSError as exc:
        raise SandboxExecutionError(
            "docker is not available on this host", cause=str(exc)
        ) from exc

    if proc.returncode != 0:
        raise SandboxExecutionError(
            "docker sandbox script exited with a non-zero status",
            returncode=proc.returncode,
            stderr=proc.stderr[-4000:],
        )

    return _load_output(workdir, current_model, _trusted(skops_trusted_types))


def run_sandboxed(
    code: str,
    *,
    current_model: object,
    X: pd.DataFrame,
    y: pd.Series,
    timeout_s: int,
    memory_mb: int,
    workdir: str,
    backend: str,
    docker_image: str = "",
    skops_trusted_types: tuple[str, ...] | list[str] | None = None,
) -> object:
    """Dispatches to `run_in_docker` or `run_in_sandbox` by `backend` ("docker" or
    "subprocess") - the one entry point `adaptation.llm_adapter` calls, so it never chooses
    between the two backends itself."""
    if backend == "docker":
        return run_in_docker(
            code,
            current_model=current_model,
            X=X,
            y=y,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            workdir=workdir,
            image=docker_image,
            skops_trusted_types=skops_trusted_types,
        )
    return run_in_sandbox(
        code,
        current_model=current_model,
        X=X,
        y=y,
        timeout_s=timeout_s,
        memory_mb=memory_mb,
        workdir=workdir,
        skops_trusted_types=skops_trusted_types,
    )
