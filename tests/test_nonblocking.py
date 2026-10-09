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

    for path in (Path(__file__).parents[1] / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if name(node.func) == "asyncio.to_thread":
                    # Only system sampling and log-thread shutdown may use an I/O worker.
                    assert path.relative_to(Path(__file__).parents[1] / "src").as_posix() in {
                        "downloader_bot/services/observability.py",
                        "downloader_bot/core/logging_config.py",
                    }
                    assert len(node.args) == 1 and not node.keywords
                    assert name(node.args[0]) in {
                        "self.sampler.sample",
                        "self.writer.close",
                        "writer.close",
                    }
                    continue
                assert name(node.func) not in banned, f"{path.name}:{node.lineno}"
