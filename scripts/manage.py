#!/usr/bin/env python3
"""Project-local operations. Runtime paths never depend on the caller's cwd."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
import uuid

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = PROJECT_DIR.parent / "paper-reader-runtime"
RUNTIME_CHILDREN = (
    "config", "postgres", "storage/workspaces", "logs", "cache", "tmp", "backups", "test-runs"
)
APP_DIRS = ("storage", "logs", "cache", "tmp")


class OperationError(Exception):
    pass


def runtime_dir(value: str | None = None) -> Path:
    raw = value if value is not None else os.environ.get("PAPER_READER_RUNTIME_DIR")
    path = Path(raw).expanduser() if raw else DEFAULT_RUNTIME_DIR
    if not path.is_absolute():
        raise OperationError("PAPER_READER_RUNTIME_DIR and --runtime must be absolute paths.")
    path = path.resolve()
    if path == PROJECT_DIR or PROJECT_DIR in path.parents or path == PROJECT_DIR.parent:
        raise OperationError("Runtime must be outside the engineering directory and cannot be its parent.")
    if path == Path("/"):
        raise OperationError("The filesystem root cannot be a runtime directory.")
    return path


def read_config(root: Path) -> dict[str, str]:
    file = root / "config/app.env"
    if not file.is_file():
        raise OperationError("Runtime is not initialized; run init first.")
    values = {}
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise OperationError("Invalid config/app.env entry; use KEY=value without shell commands.")
        if value[:1] in ('"', "'") and value[-1:] == value[:1]:
            value = value[1:-1]
        values[key] = value
    return values


def initialize(root: Path) -> None:
    for child in RUNTIME_CHILDREN:
        directory = root / child
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(root / "config", 0o700)
    config = root / "config/app.env"
    if not config.exists():
        password = secrets.token_urlsafe(36)
        uid = os.getuid() or 10001
        gid = os.getgid() if os.getuid() else 10001
        values = {
            "PAPER_READER_DATABASE_URL": f"postgresql+psycopg://paper_reader:{password}@postgres:5432/paper_reader",
            "PAPER_READER_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "POSTGRES_USER": "paper_reader", "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": "paper_reader", "PAPER_READER_BIND_HOST": "127.0.0.1",
            "PAPER_READER_PORT": "8000", "PAPER_READER_UID": str(uid),
            "PAPER_READER_GID": str(gid), "PAPER_READER_SECURE_COOKIE": "false",
            "PAPER_READER_ALLOW_PRIVATE_MODELS": "false",
            "PAPER_READER_LEASE_SECONDS": "60", "PAPER_READER_JOB_TIMEOUT_SECONDS": "1200",
            "PAPER_READER_MAX_ATTEMPTS": "2", "PAPER_READER_MAX_TOOL_CALLS": "160",
            "PAPER_READER_MAX_RUNNING_JOBS": "2",
            "PAPER_READER_MAX_JOBS_PER_WORKSPACE": "1", "PAPER_READER_INSTALL_DOCLING": "true",
            "PAPER_READER_DOCLING_ENABLED": "0",
            "PAPER_READER_DOCLING_OCR_ENABLED": "0",
            "PAPER_READER_DOCLING_ARTIFACTS_PATH": "/runtime/cache/docling/models",
        }
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("# Generated deployment configuration; keep private.\n")
            stream.writelines(f"{key}={value}\n" for key, value in values.items())
    os.chmod(config, 0o600)
    config_values = read_config(root)
    uid = int(config_values.get("PAPER_READER_UID", os.getuid()))
    gid = int(config_values.get("PAPER_READER_GID", os.getgid()))
    for name in APP_DIRS:
        directory = root / name
        os.chmod(directory, 0o750)
        if os.getuid() == 0:
            os.chown(directory, uid, gid)
            # initialize creates this child before the parent ownership is applied.
            if name == "storage":
                os.chown(directory / "workspaces", uid, gid)
    print(f"Runtime ready: {root}. Existing configuration and data were preserved.")


@contextmanager
def management_lock(root: Path):
    lock_path = root / "config/manage.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationError("Another management operation is active for this runtime.") from exc
        yield
    finally:
        os.close(descriptor)


class Compose:
    def __init__(self, root: Path):
        self.root = root
        self.config = read_config(root)
        self.project = "paper-reader-" + hashlib.sha256(str(root).encode()).hexdigest()[:12]

    def run(self, *arguments: str, capture=False, stdin=None, stdout=None):
        environment = os.environ.copy()
        # Configuration has a single source; ambient database/model secrets cannot override it.
        for key in list(environment):
            if key.startswith("PAPER_READER_") or key.startswith("POSTGRES_"):
                environment.pop(key)
        environment.update(self.config)
        environment["PAPER_READER_RUNTIME_DIR"] = str(self.root)
        command = ["docker", "compose", "--project-name", self.project,
                   "--env-file", str(self.root / "config/app.env"),
                   "--file", str(PROJECT_DIR / "deploy/compose.yml"), *arguments]
        try:
            result = subprocess.run(command, cwd=PROJECT_DIR, env=environment, check=False,
                                    stdin=stdin, stdout=subprocess.PIPE if capture else stdout,
                                    stderr=None if arguments and arguments[0] == "logs" else subprocess.PIPE)
        except FileNotFoundError as exc:
            raise OperationError("Docker with the Compose plugin is required for this operation.") from exc
        if result.returncode:
            # Raw diagnostics may include substituted URLs or other credentials.
            raise OperationError(f"Docker Compose operation failed (exit {result.returncode}); inspect service logs privately.")
        return result.stdout.decode().strip() if capture else result

    def postgres_ready(self):
        self.run("up", "-d", "--wait", "postgres")

    def migrate(self):
        self.postgres_ready()
        running = set(self.run("ps", "--services", "--status", "running", capture=True).splitlines())
        writers = sorted(running.intersection({"api", "worker"}))
        try:
            if writers:
                self.run("stop", "--timeout", "45", *writers)
            self.run("run", "--rm", "--no-deps", "api", "python", "-m", "alembic", "upgrade", "head")
        finally:
            if writers:
                self.run("start", *writers)


def prepare_models(root: Path, compose: Compose, build: bool = False):
    if compose.config.get("PAPER_READER_INSTALL_DOCLING", "true").lower() != "true":
        raise OperationError("Enable PAPER_READER_INSTALL_DOCLING and rebuild before preparing Docling models.")
    if build:
        compose.run("build", "api")
    compose.run("run", "--rm", "--no-deps", "worker", "docling-tools", "models", "download",
                "--output-dir", "/runtime/cache/docling/models")
    models = root / "cache/docling/models"
    if not models.is_dir() or not any(path.is_file() for path in models.rglob("*")):
        raise OperationError("Model preparation did not produce local artifacts; Docling remains disabled.")
    config = root / "config/app.env"
    replacements = {"PAPER_READER_DOCLING_ENABLED": "1",
                    "PAPER_READER_DOCLING_ARTIFACTS_PATH": "/runtime/cache/docling/models"}
    updated = []
    for line in config.read_text().splitlines():
        key = line.partition("=")[0].strip()
        if key in replacements:
            updated.append(f"{key}={replacements.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{key}={value}" for key, value in replacements.items())
    descriptor, temporary = tempfile.mkstemp(prefix=".app.env-", dir=config.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(updated) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, config)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("Docling artifacts prepared and enabled. Run up to apply configuration to services.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular_files(directory: Path):
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise OperationError("Backup or restore encountered a symbolic link; regular files are required.")
        if path.is_file():
            yield path
        elif not path.is_dir():
            raise OperationError("Backup or restore encountered an unsupported special file.")


def backup(root: Path, compose: Compose) -> Path:
    before = set(compose.run("ps", "--services", "--status", "running", capture=True).splitlines())
    if "postgres" not in before:
        raise OperationError("PostgreSQL must be running before backup.")
    writers = sorted(before.intersection({"api", "worker"}))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / "backups" / f"paper-reader-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz"
    # Stopping writers coordinates the logical snapshot with persisted paper files.
    # The finally block resumes precisely the writers that were running on entry.
    try:
        if writers:
            compose.run("stop", "--timeout", "45", *writers)
        remaining = set(compose.run("ps", "--services", "--status", "running", capture=True).splitlines())
        if remaining.intersection({"api", "worker"}):
            raise OperationError("Cannot create consistent backup while API or worker is running.")
        with tempfile.TemporaryDirectory(prefix="backup-", dir=root / "backups") as temp:
            staging = Path(temp)
            os.chmod(staging, 0o700)
            with (staging / "database.dump").open("wb") as stream:
                compose.run("exec", "-T", "postgres", "pg_dump", "-U", compose.config["POSTGRES_USER"],
                            "-d", compose.config["POSTGRES_DB"], "--format=custom", stdout=stream)
            for file in regular_files(root / "storage"):
                relative = file.relative_to(root)
                (staging / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, staging / relative)
            (staging / "storage").mkdir(exist_ok=True)
            list(regular_files(root / "config"))
            shutil.copytree(root / "config", staging / "config", ignore=shutil.ignore_patterns("manage.lock"))
            files = {str(path.relative_to(staging)): sha256_file(path) for path in regular_files(staging)}
            (staging / "manifest.json").write_text(json.dumps({
                "format_version": 1, "created_at": stamp, "files": files,
                "postgres_major": 18, "project": "paper-reader", "writers_paused": writers,
            }, indent=2) + "\n")
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
                for child in sorted(staging.iterdir()):
                    archive.add(child, arcname=child.name, recursive=True)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if writers:
            compose.run("start", *writers)
    print(f"Backup created: {destination}. It contains private configuration and encryption keys.")
    return destination


def unpack_backup(archive_path: Path, staging: Path):
    with tarfile.open(archive_path, mode="r:gz") as archive:
        seen = set()
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise OperationError("Backup contains an unsafe path.")
            if path.parts[0] not in {"config", "storage", "database.dump", "manifest.json"}:
                raise OperationError("Backup contains an unexpected top-level entry.")
            if member.name in seen or not (member.isdir() or member.isfile()):
                raise OperationError("Backup contains duplicate paths or unsupported file types.")
            seen.add(member.name)
            destination = staging / path
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise OperationError("Backup member is unreadable.")
                with source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, 0o600)
    try:
        manifest = json.loads((staging / "manifest.json").read_text())
        if manifest["format_version"] != 1 or manifest["project"] != "paper-reader":
            raise ValueError("Unsupported backup")
        expected = manifest["files"]
        actual = {str(path.relative_to(staging)): sha256_file(path)
                  for path in regular_files(staging) if path != staging / "manifest.json"}
        if expected != actual or "database.dump" not in actual or "config/app.env" not in actual:
            raise ValueError("Checksum mismatch")
    except (ValueError, KeyError, OSError) as exc:
        raise OperationError("Backup manifest or file checksums are invalid.") from exc


def restore(source_runtime: Path, archive_path: Path, target: Path) -> None:
    if target == source_runtime or source_runtime in target.parents or target in source_runtime.parents:
        raise OperationError("Restore target must be separate from the active runtime.")
    if target.exists():
        raise OperationError("Restore target already exists; only a new isolated directory is allowed.")
    if not archive_path.is_file():
        raise OperationError("Backup file does not exist.")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".paper-reader-restore-", dir=target.parent) as temp:
        staging = Path(temp)
        unpack_backup(archive_path, staging)
        # All checks pass before creating the target. Never reuse an existing runtime.
        target.mkdir(mode=0o700)
        shutil.copytree(staging / "config", target / "config")
        shutil.copytree(staging / "storage", target / "storage")
        initialize(target)
        values = read_config(target)
        if os.getuid() == 0:
            uid, gid = int(values["PAPER_READER_UID"]), int(values["PAPER_READER_GID"])
            for path in (target / "storage").rglob("*"):
                os.chown(path, uid, gid)
        restored = Compose(target)
        try:
            restored.postgres_ready()
            with (staging / "database.dump").open("rb") as stream:
                restored.run("exec", "-T", "postgres", "pg_restore", "--exit-on-error",
                             "--no-owner", "--no-acl", "--single-transaction", "-U",
                             values["POSTGRES_USER"], "-d", values["POSTGRES_DB"], stdin=stream)
        finally:
            # Leave no restored writers or database running; data remains for review.
            restored.run("stop", "postgres")
    print(f"Restored into isolated runtime: {target}. All restored services are stopped.")
    print("Review its bind port, origin and settings before starting; the original runtime is untouched.")


def run_checked(command: list[str], *, cwd: Path, env: dict[str, str]):
    result = subprocess.run(command, cwd=cwd, env=env, check=False)
    if result.returncode:
        raise OperationError(f"Validation command failed (exit {result.returncode}).")


def run_tests(root: Path, operations_only: bool):
    environment = os.environ.copy()
    run_checked([sys.executable, "-m", "unittest", "discover", "-s", str(PROJECT_DIR / "scripts"),
                 "-p", "test_*.py", "-v"], cwd=PROJECT_DIR, env=environment)
    if operations_only:
        return
    config = read_config(root)
    database = environment.get("PAPER_READER_TEST_DATABASE_URL", "")
    parsed = urlsplit(database)
    production = urlsplit(config["PAPER_READER_DATABASE_URL"])
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.path.endswith("_test"):
        raise OperationError("Full tests require PAPER_READER_TEST_DATABASE_URL naming an isolated PostgreSQL database with an _test suffix.")
    if parsed.path == production.path:
        raise OperationError("Test database name must differ from the configured production database.")
    isolated = root / "test-runs" / str(uuid.uuid4())
    initialize(isolated)
    environment.update({"PAPER_READER_RUNTIME_DIR": str(isolated), "PAPER_READER_DATABASE_URL": database,
                        "PAPER_READER_ENCRYPTION_KEY": read_config(isolated)["PAPER_READER_ENCRYPTION_KEY"]})
    candidates = [PROJECT_DIR / ".venv/bin/python", PROJECT_DIR / "backend/.venv/bin/python"]
    executable = next((str(path) for path in candidates if path.exists()), sys.executable)
    run_checked([executable, "-m", "pytest", "-q", "--basetemp",
                 str(isolated / "test-runs/pytest")], cwd=PROJECT_DIR / "backend", env=environment)
    for name in ("agent-runner", "web"):
        run_checked(["npm", "run", "typecheck"], cwd=PROJECT_DIR / name, env=environment)
        run_checked(["npm", "run", "build"], cwd=PROJECT_DIR / name, env=environment)
    run_checked(["npm", "test"], cwd=PROJECT_DIR / "agent-runner", env=environment)
    print(f"Validation passed. Test artifacts: {isolated}")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--runtime", help="Absolute runtime root; defaults to the project sibling.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create missing directories and private config without overwriting data.")
    up = commands.add_parser("up", help="Start database, migrate, then start API and worker.")
    up.add_argument("--build", action="store_true", help="Build application image first.")
    commands.add_parser("down", help="Stop and remove containers; preserve all runtime data.")
    commands.add_parser("status")
    logs = commands.add_parser("logs")
    logs.add_argument("service", nargs="?", choices=["api", "worker", "postgres"])
    logs.add_argument("--follow", action="store_true")
    commands.add_parser("migrate")
    models = commands.add_parser("prepare-models", help="Download Docling assets before accepting reading jobs.")
    models.add_argument("--build", action="store_true", help="Build application image before model preparation.")
    admin = commands.add_parser("init-admin", help="Create an administrator using the backend's secure prompt.")
    admin.add_argument("--username", required=True)
    commands.add_parser("backup", help="Briefly stop writers, dump PostgreSQL, and archive matched files/config.")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("archive", type=Path)
    restore_parser.add_argument("--target", required=True, help="Absolute, nonexistent isolated runtime directory.")
    tests = commands.add_parser("test")
    tests.add_argument("--operations-only", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        root = runtime_dir(args.runtime)
        if args.command == "init":
            initialize(root)
            return 0
        if args.command == "test":
            run_tests(root, args.operations_only)
            return 0
        read_config(root)
        with (nullcontext() if args.command in {"status", "logs"} else management_lock(root)):
            compose = Compose(root)
            if args.command == "up":
                if args.build:
                    compose.run("build", "api")
                compose.migrate()
                compose.run("up", "-d", "api", "worker")
            elif args.command == "down":
                compose.run("down", "--timeout", "45")
            elif args.command == "status":
                compose.run("ps", "--all")
            elif args.command == "logs":
                command = ["logs", "--tail", "200"]
                if args.follow:
                    command.append("--follow")
                if args.service:
                    command.append(args.service)
                compose.run(*command)
            elif args.command == "migrate":
                compose.migrate()
            elif args.command == "prepare-models":
                prepare_models(root, compose, args.build)
            elif args.command == "init-admin":
                compose.run("run", "--rm", "--no-deps", "api", "python", "-m", "app.cli",
                            "init-admin", "--username", args.username)
            elif args.command == "backup":
                backup(root, compose)
            elif args.command == "restore":
                restore(root, args.archive.resolve(), runtime_dir(args.target))
        return 0
    except (OperationError, OSError, tarfile.TarError, ValueError) as exc:
        print(f"Operation failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; persistent runtime data was not deleted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
