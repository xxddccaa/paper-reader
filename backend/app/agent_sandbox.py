"""Fail-closed, networkless execution of derived document calculations.

The interpreter and a small document-library allowlist are the only executable
inputs.  The container cannot see the worker's environment, project, or runtime.
Without delegated cgroups, Linux's per-UID NPROC limit is ineffective in some
user namespaces.  A seccomp filter therefore disallows child processes/threads;
this also makes the 1 GiB address-space limit apply to the entire calculation.
Scripts should use serial Python, PyMuPDF, Pillow, BeautifulSoup, and the stdlib.
"""
from __future__ import annotations

import errno
import hashlib
import math
import mimetypes
import os
from pathlib import Path, PurePosixPath
import platform
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import sysconfig
from threading import Event
import time
from uuid import uuid4


TIMEOUT_SECONDS = 60
MEMORY_BYTES = 1024 * 1024 * 1024
MAX_PROCESSES = 16
OUTPUT_BYTES = 50 * 1024 * 1024
STREAM_BYTES = 64 * 1024
MAX_OUTPUT_ENTRIES = 512
MAX_CODE_BYTES = 256 * 1024
MAX_INPUTS = 32
POLL_SECONDS = 0.025
DEPENDENCIES = ("fitz", "pymupdf", "PIL", "pillow.libs", "bs4", "soupsieve", "typing_extensions.py")
_FORBIDDEN_PARTS = {"config", "configs", "credentials", "secrets", "node_modules", "checkpoints"}
_FORBIDDEN_NAMES = {"run.json", "run-spec.json", "spec.json", "checkpoint.json", "session.json", "state.json"}


def _error(message: str, *, available: bool = False, **extra) -> dict:
    return {"available": available, "error": message, "stdout": "", "stderr": "",
            "exit_code": None, "files": [], "derived": True, **extra}


def _input_files(work_dir: Path, inputs: list[str]) -> list[tuple[Path, str]]:
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        raise ValueError(f"Select at most {MAX_INPUTS} task input files")
    selected = []
    for value in inputs:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ValueError("Sandbox inputs must be relative task file paths")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(p in (".", "..") for p in value.split("/")):
            raise ValueError("Sandbox inputs must be relative task file paths")
        if any(p.startswith(".") or p.lower() in _FORBIDDEN_PARTS for p in relative.parts):
            raise ValueError("Configuration, credentials, and hidden files are not sandbox inputs")
        if relative.name.lower() in _FORBIDDEN_NAMES:
            raise ValueError("Task control files are not sandbox inputs")
        path = work_dir
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("Sandbox input symlinks are not allowed")
        if not path.is_file() or not path.resolve().is_relative_to(work_dir):
            raise ValueError("Sandbox input must be an existing regular task file")
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Sandbox input must be an existing regular task file")
        item = (path, relative.as_posix())
        if item not in selected:
            selected.append(item)
    return selected


def _seccomp_filter() -> bytes:
    """Return classic BPF; unrecognized architectures fail closed.

    No subprocesses means no aggregate memory/NPROC loophole.  Anonymous kernel
    file/shared-memory allocations are excluded too, as they evade RLIMIT_AS and
    the designated output-directory accounting.
    """
    machine = platform.machine()
    if machine == "x86_64":
        audit_arch = 0xC000003E
        denied = [29, 30, 56, 57, 58, 101, 155, 165, 166, 272, 304, 308, 319, 321, 323, 425, 435]
    elif machine in ("aarch64", "arm64"):
        audit_arch = 0xC00000B7
        denied = [39, 40, 41, 97, 117, 194, 196, 220, 265, 268, 279, 280, 282, 425, 435]
    else:
        raise OSError("Sandbox seccomp architecture is unsupported")
    # LD arch; JEQ expected (skip kill); RET KILL_PROCESS; LD syscall number.
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, audit_arch), (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0)]
    if machine == "x86_64":
        # Refuse the alternate x32 syscall ABI as well as native fork/clone.
        instructions += [(0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    for syscall in denied:
        instructions += [(0x15, 0, 1, syscall), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    instructions.append((0x06, 0, 0, 0x7FFF0000))  # SECCOMP_RET_ALLOW
    return b"".join(struct.pack("HBBI", *instruction) for instruction in instructions)


def _runtime_mounts() -> tuple[list[str], str]:
    # sys.base_prefix is a deployment-selected interpreter, never supplied by
    # the model.  Mount its runtime, not the project's virtualenv/site-packages.
    base = Path(sys.base_prefix).resolve()
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    binary = base / "bin" / version
    stdlib = base / "lib" / version
    if not binary.is_file() or not stdlib.is_dir():
        raise OSError("Fixed sandbox Python runtime is unavailable")
    mounts = ["--ro-bind", str(binary.resolve()), f"/opt/python/bin/{version}",
              "--ro-bind", str(stdlib), f"/opt/python/lib/{version}"]
    if (stdlib / "site-packages").is_dir():
        # -S suppresses startup hooks; the empty overlay also stops an explicit
        # sys.path modification from importing unrelated base installations.
        hidden_site = f"/opt/python/lib/{version}/site-packages"
        mounts += ["--tmpfs", hidden_site, "--remount-ro", hidden_site]
    for library in sorted((base / "lib").glob("libpython*.so*")):
        mounts += ["--ro-bind", str(library.resolve()), f"/opt/python/lib/{library.name}"]
    # Never expose /usr (which can contain application installations/config).
    machine = platform.machine()
    triplet = {"x86_64": "x86_64-linux-gnu", "aarch64": "aarch64-linux-gnu", "arm64": "aarch64-linux-gnu"}.get(machine)
    if not triplet or not (Path("/lib") / triplet).is_dir():
        raise OSError("Fixed sandbox system libraries are unavailable")
    mounts += ["--ro-bind", f"/lib/{triplet}", f"/lib/{triplet}"]
    if Path("/lib64").is_dir():
        mounts += ["--ro-bind", "/lib64", "/lib64"]
    if machine in ("aarch64", "arm64") and Path("/lib/ld-linux-aarch64.so.1").exists():
        mounts += ["--ro-bind", "/lib/ld-linux-aarch64.so.1", "/lib/ld-linux-aarch64.so.1"]
    site = Path(sysconfig.get_paths()["purelib"]).resolve()
    for name in DEPENDENCIES:
        dependency = site / name
        if not dependency.exists() or not dependency.resolve().is_relative_to(site):
            raise OSError(f"Sandbox document dependency unavailable: {name}")
        mounts += ["--ro-bind", str(dependency), f"/opt/deps/{name}"]
    return mounts, f"/opt/python/bin/{version}"


def _scan_outputs(directory: Path) -> tuple[list[Path], int]:
    files: list[Path] = []
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    pending = [(os.open(directory, directory_flags), directory)]
    total = 0
    entries = 0
    try:
        while pending:
            directory_fd, parent = pending.pop()
            try:
                with os.scandir(directory_fd) as children:
                    for entry in children:
                        entries += 1
                        if entries > MAX_OUTPUT_ENTRIES:
                            raise ValueError("Sandbox output file/directory count exceeded")
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:  # Scripts can rename live files.
                            continue
                        path = parent / entry.name
                        if stat.S_ISDIR(info.st_mode):
                            # Never traverse a directory replaced by a symlink
                            # between stat and scan while the script is running.
                            child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                            pending.append((child_fd, path))
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            total += info.st_size
                            if total > OUTPUT_BYTES:
                                raise ValueError("Sandbox total output exceeded 50 MiB")
                            files.append(path)
                        else:
                            raise ValueError("Sandbox outputs must be regular files, without links or devices")
            finally:
                os.close(directory_fd)
    finally:
        for directory_fd, _ in pending:
            os.close(directory_fd)
    return files, total


def _kill(process: subprocess.Popen) -> None:
    # Killing Bubblewrap tears down its private PID namespace (including any
    # process that managed to change session), in addition to the host group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_python(work_dir: Path, code: str, inputs: list[str], cancelled: Event) -> dict:
    """Execute a bounded script, returning only validated derived output files.

    The caller must additionally select inputs from its registered task sources
    and artifacts.  The filesystem checks here are a second boundary; callers
    must never offer task checkpoints or other control files as source material.
    A nonzero exit, cancellation, timeout or output violation publishes no files.
    """
    if cancelled.is_set():
        return _error("Sandbox execution cancelled", cancelled=True)
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return _error("Sandbox code must contain at most 256 KiB of Python")
    try:
        root = Path(work_dir).resolve(strict=True)
        project = Path(__file__).resolve().parents[2]
        if not root.is_dir() or root.is_relative_to(project):
            raise ValueError("Sandbox work directory must be a task runtime directory")
        selected = _input_files(root, inputs)
        bwrap = shutil.which("bwrap")
        if not bwrap:
            return _error("Bubblewrap is unavailable; isolated Python cannot run")
        mounts, interpreter = _runtime_mounts()
        filter_bytes = _seccomp_filter()
    except (OSError, ValueError) as exc:
        return _error(str(exc))

    run_relative = Path("sandbox") / uuid4().hex
    run_dir = root / run_relative
    outputs = run_dir / "outputs"
    process = None
    filter_fd = None
    selector = selectors.DefaultSelector()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    marker = f"paper-reader-sandbox-ready-{uuid4().hex}"
    failure = None
    started = False
    began = time.monotonic()
    try:
        # Reject a forged preexisting sandbox parent before creating or mounting
        # anything.  No untrusted process ever receives a writable parent mount.
        if (root / "sandbox").is_symlink():
            raise ValueError("Sandbox output parent cannot be a symlink")
        outputs.mkdir(parents=True, mode=0o700)
        (outputs / ".tmp").mkdir(mode=0o700)
        script = run_dir / "script.py"
        script.write_text(code, encoding="utf-8")
        script.chmod(0o600)
        bootstrap = run_dir / "bootstrap.py"
        bootstrap.write_text(
            "import resource,runpy,sys\n"
            f"resource.setrlimit(resource.RLIMIT_AS,({MEMORY_BYTES},{MEMORY_BYTES}))\n"
            f"resource.setrlimit(resource.RLIMIT_NPROC,({MAX_PROCESSES},{MAX_PROCESSES}))\n"
            f"resource.setrlimit(resource.RLIMIT_FSIZE,({OUTPUT_BYTES},{OUTPUT_BYTES}))\n"
            f"resource.setrlimit(resource.RLIMIT_CPU,({math.ceil(TIMEOUT_SECONDS)},{math.ceil(TIMEOUT_SECONDS)}))\n"
            "resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))\n"
            "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
            "sys.path.insert(0,'/opt/deps')\n"
            f"print({marker!r},file=sys.stderr,flush=True)\n"
            "runpy.run_path('/script.py',run_name='__main__')\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        filter_path = run_dir / "seccomp.bpf"
        filter_path.write_bytes(filter_bytes)
        filter_path.chmod(0o600)
        filter_fd = os.open(filter_path, os.O_RDONLY | os.O_CLOEXEC)
        filter_path.unlink()
        command = [bwrap, "--unshare-all", "--die-with-parent", "--new-session",
                   "--uid", "65534", "--gid", "65534", "--cap-drop", "ALL", *mounts,
                   "--dir", "/proc", "--dev", "/dev", "--remount-ro", "/dev",
                   "--dir", "/inputs", "--bind", str(outputs), "/outputs",
                   "--symlink", "/outputs/.tmp", "/tmp",
                   "--ro-bind", str(script), "/script.py",
                   "--ro-bind", str(bootstrap), "/bootstrap.py"]
        for path, relative in selected:
            command += ["--ro-bind", str(path), f"/inputs/{relative}"]
        command += ["--remount-ro", "/", "--clearenv", "--setenv", "PATH", "/opt/python/bin",
                    "--setenv", "LC_ALL", "C.UTF-8", "--setenv", "TMPDIR", "/outputs/.tmp",
                    "--chdir", "/outputs", "--seccomp", str(filter_fd),
                    interpreter, "-I", "-S", "-B", "-u", "/bootstrap.py"]
        # Do not use preexec_fn: the worker is multithreaded.  Hard limits are
        # installed by the trusted bootstrap before any model-authored code.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=run_dir, env={},
                                   start_new_session=True, pass_fds=(filter_fd,))
        os.close(filter_fd)
        filter_fd = None
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            if cancelled.is_set():
                failure = "Sandbox execution cancelled"
            elif time.monotonic() - began >= TIMEOUT_SECONDS:
                failure = "Sandbox execution exceeded 60 seconds"
            else:
                try:
                    _scan_outputs(outputs)
                except (ValueError, OSError):
                    failure = "Sandbox output limit or regular-file safety check failed"
            if failure:
                _kill(process)
                break
            for key, _ in selector.select(POLL_SECONDS):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = STREAM_BYTES - len(streams[key.data])
                streams[key.data].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[key.data] = True
        process.wait(timeout=3)
        stderr = bytes(streams["stderr"]).decode("utf-8", errors="replace")
        started = marker in stderr
        stderr = stderr.replace(marker + "\n", "", 1)
        result = {"available": started, "stdout": bytes(streams["stdout"]).decode("utf-8", errors="replace"),
                  "stderr": stderr, "exit_code": process.returncode, "files": [], "derived": True,
                  "stdout_truncated": truncated["stdout"], "stderr_truncated": truncated["stderr"],
                  "elapsed_seconds": round(time.monotonic() - began, 3),
                  "limits": {"timeout_seconds": TIMEOUT_SECONDS, "memory_bytes": MEMORY_BYTES,
                             "max_processes": MAX_PROCESSES, "child_processes_allowed": False,
                             "output_bytes": OUTPUT_BYTES}}
        if cancelled.is_set():
            failure = "Sandbox execution cancelled"
        if not started:
            result["error"] = "Isolated Python sandbox could not start; no host fallback was attempted"
            # Bubblewrap can print host mount paths on startup failure.  The
            # tool reports a safe diagnosis without disclosing those paths.
            result["stderr"] = "Sandbox prerequisites, mounts, or kernel isolation are unavailable"
        elif failure:
            result["error"] = failure
            result["cancelled"] = cancelled.is_set()
        elif process.returncode:
            result["error"] = "Sandbox Python exited unsuccessfully"
        else:
            files, total = _scan_outputs(outputs)
            for file in sorted(files):
                # Temporary files are included in quota accounting but aren't
                # registered as reusable artifacts.
                if file.relative_to(outputs).parts[0] == ".tmp":
                    continue
                digest = hashlib.sha256()
                with file.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                result["files"].append({"name": file.relative_to(outputs).as_posix(),
                                        "path": file.relative_to(root).as_posix(), "size": file.stat().st_size,
                                        "sha256": digest.hexdigest(), "mime_type": mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                                        "derived": True})
            result["output_bytes"] = total
        if cancelled.is_set():
            result.update(error="Sandbox execution cancelled", cancelled=True, files=[])
        if result.get("error"):
            shutil.rmtree(outputs, ignore_errors=True)
        else:
            shutil.rmtree(outputs / ".tmp", ignore_errors=True)
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        if process is not None and process.poll() is None:
            _kill(process)
        shutil.rmtree(outputs, ignore_errors=True)
        return _error("Sandbox execution unavailable or output validation failed", available=started)
    finally:
        selector.close()
        if filter_fd is not None:
            os.close(filter_fd)
        if process is not None:
            if process.poll() is None:
                _kill(process)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
