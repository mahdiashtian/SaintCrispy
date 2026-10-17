import ast
from pathlib import Path


def test_runtime_does_not_call_blocking_disk_network_or_subprocess_apis():
    banned = {
        "open",
        "input",
        "time.sleep",
        "subprocess.run",
        "subprocess.Popen",
        "requests.get",
        "requests.post",
        "urllib.request.urlopen",
        "os.stat",
        "os.path.isfile",
        "asyncio.to_thread",
        "logging.basicConfig",
    }

    def name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return name(node.value) + "." + node.attr
        return ""

    root = Path(__file__).parents[1]
    paths = [root / "container.py"]
    for folder in ("src", "core", "db", "schemas", "repositories", "services", "bot"):
        paths.extend((root / folder).rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if name(node.func) == "asyncio.to_thread":
                    if path.relative_to(root).as_posix() == "bot/session.py":
                        assert name(node.args[0]) in {"_create_session", "session.close"}
                        continue
                    if path.relative_to(root).as_posix() == "bot/transfers/faststart.py":
                        # Video staging uses isolated disk workers; FFmpeg stays an
                        # async subprocess. No filesystem operation runs on the loop.
                        assert name(node.args[0]) in {"function", "tempfile.mkdtemp"}
                        assert any(
                            isinstance(function, ast.AsyncFunctionDef)
                            and function.name in {"disk_call", "workspace"}
                            and node in ast.walk(function)
                            for function in ast.walk(tree)
                        )
                        continue
                    cpu_helpers = {
                        "repositories/postgres/admin.py": {"json.dumps", "json.loads"},
                        "services/admin_maintenance.py": {
                            "encode_rows",
                            "serialize_payload",
                            "json.loads",
                            "validate_payload",
                            "restore_values",
                        },
                        "services/management.py": {"render_report"},
                        "services/database_backup.py": {
                            "tempfile.mkstemp",
                            "valid_archive",
                            "path.unlink",
                        },
                    }
                    if path.relative_to(root).as_posix() in cpu_helpers:
                        assert name(node.args[0]) in cpu_helpers[path.relative_to(root).as_posix()]
                        assert any(
                            isinstance(function, ast.AsyncFunctionDef)
                            and node in ast.walk(function)
                            for function in ast.walk(tree)
                        )
                        continue
                    # Only system sampling and log-thread shutdown may use an I/O worker.
                    assert path.relative_to(root).as_posix() in {
                        "services/observability.py",
                        "core/logging_config.py",
                    }
                    assert len(node.args) == 1 and not node.keywords
                    assert name(node.args[0]) in {
                        "self.sampler.sample",
                        "self.writer.close",
                        "writer.close",
                    }
                    continue
                assert name(node.func) not in banned, f"{path.name}:{node.lineno}"


def test_video_staging_disk_helpers_are_only_dispatched_to_io_workers():
    path = Path(__file__).parents[1] / "bot/transfers/faststart.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helpers = {"append", "read_at"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            assert node.func.id not in helpers  # Functions are passed to disk_call.
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "open",
            "read",
            "write",
            "seek",
        }:
            assert (
                any(
                    isinstance(function, ast.FunctionDef)
                    and function.name in helpers
                    and node in ast.walk(function)
                    for function in tree.body
                )
                or node.func.attr == "read"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "stderr"
            )
