"""Keep the Couplyo layer direction explicit during future edits."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/downloader_bot"


def test_application_layers_have_one_canonical_implementation():
    for folder in (
        "core",
        "db/postgres",
        "db/redis",
        "repositories/interfaces",
        "repositories/postgres",
        "repositories/redis",
        "schemas",
        "services",
        "bot/handlers",
        "bot/state",
        "bot/jobs",
    ):
        assert (ROOT / folder / "__init__.py").is_file()
    assert {p.name for p in ROOT.glob("*.py")} == {"__init__.py", "__main__.py", "container.py"}
    assert len((ROOT / "__main__.py").read_text().splitlines()) < 65


def test_handlers_do_not_execute_queries_or_import_concrete_repositories():
    for path in (ROOT / "bot/handlers").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("downloader_bot.db", "downloader_bot.repositories")
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"execute", "fetchrow", "fetchval", "transaction"}


def test_postgres_repositories_do_not_depend_on_telegram_or_redis():
    for path in (ROOT / "repositories/postgres").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("telethon", "redis", "downloader_bot.bot")
                )


def test_business_services_do_not_import_the_telegram_framework():
    for path in (ROOT / "services").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("telethon", "downloader_bot.bot"))
