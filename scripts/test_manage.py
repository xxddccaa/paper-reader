"""Operations tests run entirely in temporary directories; Docker is mocked."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("manage", Path(__file__).with_name("manage.py"))
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class FakeCompose:
    def __init__(self, running=("postgres", "api", "worker"), fail_dump=False):
        self.running = set(running)
        self.calls = []
        self.config = {"POSTGRES_USER": "test_user", "POSTGRES_DB": "paper_reader_test"}
        self.fail_dump = fail_dump

    def run(self, *args, capture=False, stdout=None, stdin=None):
        self.calls.append(args)
        if args[0] == "ps":
            return "\n".join(sorted(self.running))
        if args[0] == "stop":
            self.running.difference_update(args[3:])
        if args[0] == "start":
            self.running.update(args[1:])
        if "pg_dump" in args:
            if self.fail_dump:
                raise manage.OperationError("simulated dump failure")
            stdout.write(b"synthetic dump for operations test only")


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "runtime"
        with contextlib.redirect_stdout(io.StringIO()):
            manage.initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_path_uses_script_location(self):
        with patch.dict(os.environ, {}, clear=True), patch("os.getcwd", return_value="/unrelated"):
            self.assertEqual(manage.runtime_dir(), manage.PROJECT_DIR.parent / "paper-reader-runtime")

    def test_relative_runtime_is_rejected(self):
        with self.assertRaises(manage.OperationError):
            manage.runtime_dir("../accidental")

    def test_source_directory_cannot_be_runtime(self):
        for path in (manage.PROJECT_DIR, manage.PROJECT_DIR / "runtime", Path("/")):
            with self.assertRaises(manage.OperationError):
                manage.runtime_dir(str(path))

    def test_init_preserves_existing_config_and_database(self):
        config = self.root / "config/app.env"
        initial = config.read_bytes()
        sentinel = self.root / "postgres/do-not-delete"
        sentinel.write_text("important")
        with contextlib.redirect_stdout(io.StringIO()):
            manage.initialize(self.root)
        self.assertEqual(initial, config.read_bytes())
        self.assertEqual(sentinel.read_text(), "important")
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_unique_keys_for_new_runtime(self):
        second = Path(self.temp.name) / "other"
        with contextlib.redirect_stdout(io.StringIO()):
            manage.initialize(second)
        a, b = manage.read_config(self.root), manage.read_config(second)
        self.assertEqual(a["PAPER_READER_MAX_TOOL_CALLS"], "160")
        self.assertEqual(b["PAPER_READER_MAX_TOOL_CALLS"], "160")
        self.assertNotEqual(a["PAPER_READER_ENCRYPTION_KEY"], b["PAPER_READER_ENCRYPTION_KEY"])
        self.assertNotEqual(a["POSTGRES_PASSWORD"], b["POSTGRES_PASSWORD"])

    def test_compose_uses_absolute_runtime_and_does_not_inherit_secrets(self):
        process = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch.dict(os.environ, {"PAPER_READER_DATABASE_URL": "ambient-secret"}), \
                patch("subprocess.run", return_value=process) as run:
            manage.Compose(self.root).run("ps")
        args, kwargs = run.call_args
        self.assertEqual(kwargs["cwd"], manage.PROJECT_DIR)
        self.assertEqual(kwargs["env"]["PAPER_READER_RUNTIME_DIR"], str(self.root))
        self.assertNotEqual(kwargs["env"]["PAPER_READER_DATABASE_URL"], "ambient-secret")
        self.assertNotIn(manage.read_config(self.root)["POSTGRES_PASSWORD"], " ".join(args[0]))

    def test_down_has_no_volume_deletion_flag(self):
        with patch.object(manage.Compose, "run") as run:
            self.assertEqual(manage.main(["--runtime", str(self.root), "down"]), 0)
        self.assertEqual(run.call_args.args, ("down", "--timeout", "45"))
        self.assertTrue((self.root / "config/app.env").exists())

    def test_backup_pauses_and_resumes_only_running_writers(self):
        (self.root / "storage/workspaces/paper.txt").write_text("paper evidence")
        # A nested manifest must be treated as user data, not the archive manifest.
        (self.root / "storage/workspaces/manifest.json").write_text("{}")
        compose = FakeCompose(running=("postgres", "worker"))
        with contextlib.redirect_stdout(io.StringIO()):
            archive = manage.backup(self.root, compose)
        self.assertEqual(compose.running, {"postgres", "worker"})
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertIn(("stop", "--timeout", "45", "worker"), compose.calls)
        staging = Path(self.temp.name) / "unpacked"
        staging.mkdir()
        manage.unpack_backup(archive, staging)
        self.assertEqual((staging / "storage/workspaces/paper.txt").read_text(), "paper evidence")
        self.assertTrue((staging / "database.dump").is_file())
        self.assertFalse((staging / "postgres").exists())

    def test_backup_failure_resumes_writers_and_removes_partial_archive(self):
        compose = FakeCompose(fail_dump=True)
        with self.assertRaises(manage.OperationError):
            manage.backup(self.root, compose)
        self.assertEqual(compose.running, {"postgres", "api", "worker"})
        self.assertEqual(list((self.root / "backups").glob("*.tar.gz")), [])

    def test_backup_rejects_symlinks(self):
        (self.root / "config/external").symlink_to("/etc/passwd")
        with self.assertRaises(manage.OperationError):
            manage.backup(self.root, FakeCompose())

    def test_restore_refuses_existing_or_active_target_without_calling_docker(self):
        existing = Path(self.temp.name) / "existing"
        existing.mkdir()
        for target in (self.root, self.root / "nested", existing):
            with self.assertRaises(manage.OperationError), patch.object(manage.Compose, "run") as run:
                manage.restore(self.root, Path("unneeded"), target)
                run.assert_not_called()
        self.assertTrue((self.root / "config/app.env").exists())

    def test_archive_rejects_traversal_and_links(self):
        for name, kind in (("../escaped", tarfile.REGTYPE), ("storage/link", tarfile.SYMTYPE)):
            archive = Path(self.temp.name) / "malicious.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.linkname = "/etc/passwd"
                tar.addfile(entry)
            staging = Path(self.temp.name) / "staging"
            staging.mkdir(exist_ok=True)
            with self.assertRaises(manage.OperationError):
                manage.unpack_backup(archive, staging)
        self.assertFalse((Path(self.temp.name) / "escaped").exists())

    def test_archive_rejects_checksum_mismatch(self):
        archive = Path(self.temp.name) / "corrupt.tar.gz"
        contents = {"database.dump": b"wrong data", "config/app.env": b"EXAMPLE=value\n"}
        manifest = {"format_version": 1, "project": "paper-reader", "files": {
            "database.dump": "incorrect", "config/app.env": hashlib.sha256(contents["config/app.env"]).hexdigest()}}
        contents["manifest.json"] = json.dumps(manifest).encode()
        with tarfile.open(archive, "w:gz") as tar:
            for name, data in contents.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        staging = Path(self.temp.name) / "corrupt"
        staging.mkdir()
        with self.assertRaises(manage.OperationError):
            manage.unpack_backup(archive, staging)

    def test_full_tests_require_separate_database(self):
        production = manage.read_config(self.root)["PAPER_READER_DATABASE_URL"]
        with patch.dict(os.environ, {"PAPER_READER_TEST_DATABASE_URL": production}), \
                patch.object(manage, "run_checked"):
            with self.assertRaises(manage.OperationError):
                manage.run_tests(self.root, False)

    def test_full_tests_keep_pytest_artifacts_under_isolated_runtime(self):
        database = "postgresql+psycopg://test_user:fixture@localhost:5432/paper_reader_test"
        with patch.dict(os.environ, {"PAPER_READER_TEST_DATABASE_URL": database}), \
                patch.object(manage, "run_checked") as run, contextlib.redirect_stdout(io.StringIO()):
            manage.run_tests(self.root, False)
        invocation = next(call for call in run.call_args_list if "pytest" in call.args[0])
        command = invocation.args[0]
        isolated = Path(invocation.kwargs["env"]["PAPER_READER_RUNTIME_DIR"])
        self.assertEqual(isolated.parent, self.root / "test-runs")
        basetemp = Path(command[command.index("--basetemp") + 1])
        self.assertEqual(basetemp, isolated / "test-runs/pytest")
        self.assertEqual(invocation.kwargs["env"]["PAPER_READER_DATABASE_URL"], database)

    def test_full_tests_reject_test_substring_without_suffix(self):
        for name in ("test_database", "paper_reader_test_backup"):
            database = f"postgresql+psycopg://test_user:fixture@localhost:5432/{name}"
            with patch.dict(os.environ, {"PAPER_READER_TEST_DATABASE_URL": database}), \
                    patch.object(manage, "run_checked") as run, self.assertRaises(manage.OperationError):
                manage.run_tests(self.root, False)
            self.assertFalse(any("pytest" in call.args[0] for call in run.call_args_list))

    def test_restore_uses_isolated_database_and_leaves_it_stopped(self):
        (self.root / "storage/workspaces/paper.txt").write_text("original evidence")
        with contextlib.redirect_stdout(io.StringIO()):
            archive = manage.backup(self.root, FakeCompose())
        target = Path(self.temp.name) / "restored"
        with patch.object(manage.Compose, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            manage.restore(self.root, archive, target)
        self.assertEqual((target / "storage/workspaces/paper.txt").read_text(), "original evidence")
        self.assertEqual((self.root / "storage/workspaces/paper.txt").read_text(), "original evidence")
        self.assertEqual(run.call_args.args, ("stop", "postgres"))
        calls = [call.args for call in run.call_args_list]
        self.assertTrue(any("pg_restore" in args for args in calls))
        self.assertFalse(any("api" in args or "worker" in args for args in calls))
        self.assertNotEqual(manage.Compose(self.root).project, manage.Compose(target).project)

    def test_migration_coordinates_existing_writers(self):
        compose = manage.Compose(self.root)
        with patch.object(compose, "run", side_effect=[None, "postgres\nworker", None, None, None]) as run:
            compose.migrate()
        calls = [call.args for call in run.call_args_list]
        self.assertIn(("stop", "--timeout", "45", "worker"), calls)
        self.assertEqual(calls[-1], ("start", "worker"))

    def test_prepare_models_enables_only_after_artifacts_exist(self):
        compose = manage.Compose(self.root)
        initial = manage.read_config(self.root)
        with patch.object(compose, "run"), self.assertRaises(manage.OperationError):
            manage.prepare_models(self.root, compose)
        self.assertEqual(manage.read_config(self.root)["PAPER_READER_DOCLING_ENABLED"], "0")
        artifacts = self.root / "cache/docling/models"
        artifacts.mkdir(parents=True)
        (artifacts / "test-model").write_bytes(b"fixture")
        with patch.object(compose, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            manage.prepare_models(self.root, compose)
        updated = manage.read_config(self.root)
        self.assertEqual(updated["PAPER_READER_DOCLING_ENABLED"], "1")
        self.assertEqual(updated["PAPER_READER_ENCRYPTION_KEY"], initial["PAPER_READER_ENCRYPTION_KEY"])
        self.assertEqual((self.root / "config/app.env").stat().st_mode & 0o777, 0o600)
        self.assertIn("docling-tools", run.call_args.args)

    def test_management_lock_blocks_concurrent_commands(self):
        with manage.management_lock(self.root), self.assertRaises(manage.OperationError):
            with manage.management_lock(self.root):
                self.fail("should not acquire the lock twice")


if __name__ == "__main__":
    unittest.main()
