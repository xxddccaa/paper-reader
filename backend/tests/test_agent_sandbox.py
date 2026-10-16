"""Real Bubblewrap checks, including the limits user namespaces can bypass."""
import hashlib
import json
from pathlib import Path
import socket
import threading
import time

import fitz
import pytest

from app import agent_sandbox as sandbox


@pytest.fixture
def working_sandbox(tmp_path):
    result = sandbox.run_python(tmp_path, "print('sandbox-ready')", [], threading.Event())
    if not result["available"]:
        pytest.skip("Kernel/Bubblewrap isolation unavailable: " + result["error"])
    assert result["exit_code"] == 0, result
    return tmp_path


def test_document_dependencies_actual_pdf_and_image_outputs(working_sandbox):
    root = working_sandbox
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A verified original paper. Accuracy is 91.5 percent.")
    document.save(root / "paper.pdf")
    document.close()
    before = hashlib.sha256((root / "paper.pdf").read_bytes()).hexdigest()
    result = sandbox.run_python(root, """
from pathlib import Path
import fitz
from PIL import Image
from bs4 import BeautifulSoup
document = fitz.open('/inputs/paper.pdf')
page = document[0]
page.get_pixmap().save('/outputs/page.png')
image = Image.open('/outputs/page.png')
assert image.width > 100 and image.height > 100
text = BeautifulSoup('<p>' + page.get_text() + '</p>', 'html.parser').get_text()
Path('/outputs/summary.txt').write_text(text)
print('page_count', len(document))
""", ["paper.pdf"], threading.Event())
    assert result["available"] and result["exit_code"] == 0, result
    assert "page_count 1" in result["stdout"]
    assert len(result["files"]) == 2
    summary = next(item for item in result["files"] if item["name"] == "summary.txt")
    assert "91.5 percent" in (root / summary["path"]).read_text()
    for item in result["files"]:
        output = root / item["path"]
        assert output.is_relative_to(root)
        assert item["derived"] is True
        assert item["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert hashlib.sha256((root / "paper.pdf").read_bytes()).hexdigest() == before


def test_filesystem_network_environment_and_readonly_input_isolation(working_sandbox, monkeypatch):
    root = working_sandbox
    (root / "source").mkdir()
    (root / "source" / "paper.txt").write_text("approved input")
    (root / "unselected.txt").write_text("task secret")
    monkeypatch.setenv("PAPER_READER_SANDBOX_TEST_SECRET", "must-not-be-inherited")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        result = sandbox.run_python(root, f"""
from pathlib import Path
import json, os, socket
assert Path('/inputs/source/paper.txt').read_text() == 'approved input'
for hidden in ['/mnt', '/root', '/etc', '/sys', '/usr', '/inputs/unselected.txt', {str(root)!r}]:
    assert not Path(hidden).exists(), hidden
assert list(Path('/proc').iterdir()) == []
assert 'PAPER_READER_SANDBOX_TEST_SECRET' not in os.environ
for target in ['/inputs/source/paper.txt', '/inputs/extra', '/opt/extra', '/dev/shm/extra', '/proc/extra']:
    try:
        Path(target).write_text('should not be writable')
    except OSError:
        pass
    else:
        raise AssertionError(target + ' was writable')
sock = socket.socket()
sock.settimeout(0.2)
assert sock.connect_ex(('127.0.0.1', {port})) != 0
assert sock.connect_ex(('1.1.1.1', 443)) != 0
print(json.dumps(dict(os.environ)))
Path('/outputs/calculation.txt').write_text('derived')
""", ["source/paper.txt"], threading.Event())
    finally:
        listener.close()
    assert result["available"] and result["exit_code"] == 0, result
    assert "must-not-be-inherited" not in json.dumps(result)
    assert (root / "source" / "paper.txt").read_text() == "approved input"


def test_hard_memory_and_process_limits_cannot_be_raised(working_sandbox):
    result = sandbox.run_python(working_sandbox, f"""
import errno, os, resource, threading
assert resource.getrlimit(resource.RLIMIT_AS) == ({sandbox.MEMORY_BYTES}, {sandbox.MEMORY_BYTES})
assert resource.getrlimit(resource.RLIMIT_NPROC) == (16, 16)
try:
    resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
except (ValueError, OSError):
    pass
else:
    raise AssertionError('memory hard limit was raised')
try:
    bytearray({sandbox.MEMORY_BYTES + 1})
except MemoryError:
    print('memory allocation refused')
else:
    raise AssertionError('memory cap was bypassed')
try:
    child = os.fork()
except OSError as error:
    assert error.errno == errno.EPERM
    print('process creation refused')
else:
    if child == 0:
        os._exit(123)
    os.waitpid(child, 0)
    raise AssertionError('seccomp allowed child process')
try:
    threading.Thread(target=lambda: None).start()
except RuntimeError:
    print('thread creation refused')
else:
    raise AssertionError('seccomp allowed thread creation')
""", [], threading.Event())
    assert result["available"] and result["exit_code"] == 0, result
    assert "memory allocation refused" in result["stdout"]
    assert "process creation refused" in result["stdout"]
    assert "thread creation refused" in result["stdout"]


def test_anonymous_kernel_storage_cannot_bypass_memory_accounting(working_sandbox):
    result = sandbox.run_python(working_sandbox, """
import ctypes, errno, platform
libc = ctypes.CDLL(None, use_errno=True)
calls = (319, 29) if platform.machine() == 'x86_64' else (279, 194)
for number, args in [(calls[0], (b'anonymous-output', 0)), (calls[1], (0, 4096, 0o1600))]:
    assert libc.syscall(number, *args) == -1
    assert ctypes.get_errno() == errno.EPERM
print('kernel storage refused')
""", [], threading.Event())
    assert result["available"] and result["exit_code"] == 0, result
    assert "kernel storage refused" in result["stdout"]


def test_output_streams_are_bounded_and_keep_draining(working_sandbox):
    result = sandbox.run_python(working_sandbox, """
import sys
sys.stdout.write('a' * 300000)
sys.stderr.write('b' * 300000)
""", [], threading.Event())
    assert result["available"] and result["exit_code"] == 0, result
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert len(result["stdout"].encode()) <= sandbox.STREAM_BYTES
    assert len(result["stderr"].encode()) <= sandbox.STREAM_BYTES


@pytest.mark.parametrize("mode", ["one_file", "many_files", "temporary_files"])
def test_output_quota_rejects_single_aggregate_and_temp_files(working_sandbox, monkeypatch, mode):
    monkeypatch.setattr(sandbox, "OUTPUT_BYTES", 1024 * 1024)
    scripts = {
        "one_file": "from pathlib import Path; Path('/outputs/large').write_bytes(b'x' * (2 * 1024 * 1024))",
        "many_files": "from pathlib import Path\nfor i in range(4): Path(f'/outputs/part{i}').write_bytes(b'x' * (400 * 1024))",
        "temporary_files": "from pathlib import Path\nfor i in range(4): Path(f'/tmp/part{i}').write_bytes(b'x' * (400 * 1024))",
    }
    before = set(working_sandbox.glob("sandbox/*/outputs"))
    result = sandbox.run_python(working_sandbox, scripts[mode], [], threading.Event())
    assert result["error"], result
    assert result["files"] == []
    assert set(working_sandbox.glob("sandbox/*/outputs")) == before


@pytest.mark.parametrize("script", [
    "from pathlib import Path; Path('/outputs/escape').symlink_to('/inputs/source.txt')",
    "import os; os.mkfifo('/outputs/fifo')",
    "from pathlib import Path; import os; Path('/outputs/a').write_text('a'); os.link('/outputs/a', '/outputs/b')",
])
def test_unsafe_outputs_are_not_registered(working_sandbox, script):
    (working_sandbox / "source.txt").write_text("original")
    result = sandbox.run_python(working_sandbox, script, ["source.txt"], threading.Event())
    assert result["error"], result
    assert result["files"] == []
    assert (working_sandbox / "source.txt").read_text() == "original"


def test_nonzero_exit_does_not_publish_partial_files(working_sandbox):
    result = sandbox.run_python(working_sandbox,
        "from pathlib import Path; Path('/outputs/partial').write_text('unaccepted'); raise ValueError('bad calculation')",
        [], threading.Event())
    assert result["available"] and result["exit_code"] != 0
    assert "bad calculation" in result["stderr"]
    assert result["files"] == []
    assert result["error"]


def test_timeout_kills_execution_and_removes_unaccepted_output(working_sandbox, monkeypatch):
    monkeypatch.setattr(sandbox, "TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    result = sandbox.run_python(working_sandbox,
        "from pathlib import Path; import time; Path('/outputs/partial').write_text('partial'); time.sleep(10)",
        [], threading.Event())
    assert time.monotonic() - started < 3
    assert result["error"] and result["files"] == []
    assert result["exit_code"] != 0


def test_cancellation_during_execution_and_before_start(working_sandbox, monkeypatch):
    cancelled = threading.Event()
    timer = threading.Timer(0.3, cancelled.set)
    timer.start()
    try:
        result = sandbox.run_python(working_sandbox, "import time; time.sleep(10)", [], cancelled)
    finally:
        timer.cancel()
        timer.join()
    assert result["error"] and result["cancelled"] is True
    assert result["files"] == []
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **kw: pytest.fail("Cancelled task started a process"))
    result = sandbox.run_python(working_sandbox, "print('must not execute')", [], cancelled)
    assert result["cancelled"] is True


@pytest.mark.parametrize("value", ["../outside", "/etc/passwd", ".env", "config/app.env", "checkpoints/state.json", "spec.json", "folder\\file", "missing.pdf"])
def test_untrusted_input_paths_fail_before_process_start(tmp_path, monkeypatch, value):
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **kw: pytest.fail("Invalid input started a process"))
    result = sandbox.run_python(tmp_path, "pass", [value], threading.Event())
    assert result["available"] is False
    assert result["error"] and result["files"] == []


def test_symlink_inputs_and_output_parent_fail_closed(tmp_path):
    (tmp_path / "original.txt").write_text("original")
    (tmp_path / "link.txt").symlink_to(tmp_path / "original.txt")
    result = sandbox.run_python(tmp_path, "pass", ["link.txt"], threading.Event())
    assert result["available"] is False and "symlink" in result["error"]
    (tmp_path / "sandbox").symlink_to(tmp_path.parent)
    result = sandbox.run_python(tmp_path, "pass", [], threading.Event())
    assert result["available"] is False and result["error"]


def test_output_scan_never_follows_directory_swapped_for_host_symlink(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    (root / "nested").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must not scan")
    original_open = sandbox.os.open
    def swap_before_open(path, flags, *args, **kwargs):
        if path == "nested" and "dir_fd" in kwargs:
            (root / "nested").rename(root / "old-nested")
            (root / "nested").symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(sandbox.os, "open", swap_before_open)
    with pytest.raises(OSError):
        sandbox._scan_outputs(root)


def test_cancellation_during_final_hashing_discards_output(working_sandbox, monkeypatch):
    cancelled = threading.Event()
    original_hash = sandbox.hashlib.sha256
    def cancel_before_hash(*args, **kwargs):
        cancelled.set()
        return original_hash(*args, **kwargs)
    monkeypatch.setattr(sandbox.hashlib, "sha256", cancel_before_hash)
    result = sandbox.run_python(working_sandbox,
        "from pathlib import Path; Path('/outputs/result').write_text('unaccepted')", [], cancelled)
    assert result["cancelled"] is True
    assert result["error"] and result["files"] == []


def test_missing_bubblewrap_never_runs_code_on_host(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **kw: pytest.fail("Host fallback attempted"))
    result = sandbox.run_python(tmp_path, "print('must not execute')", [], threading.Event())
    assert result["available"] is False and "Bubblewrap" in result["error"]
    assert result["files"] == []


def test_kernel_or_launcher_failure_does_not_fall_back(tmp_path, monkeypatch):
    # An inert failed launcher simulates an unavailable user namespace.  A host
    # Python fallback would create the marker and make this test fail.
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/bin/false")
    marker = tmp_path / "host-fallback-marker"
    result = sandbox.run_python(tmp_path, f"open({str(marker)!r}, 'w').write('unsafe')", [], threading.Event())
    assert result["available"] is False
    assert "no host fallback" in result["error"]
    assert not marker.exists()
