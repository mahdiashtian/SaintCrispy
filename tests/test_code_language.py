"""Keep developer comments English while preserving Persian bot messages."""

import ast
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def contains_persian(text: str) -> bool:
    return any("\u0600" <= character <= "\u06ff" for character in text)


def test_python_comments_and_docstrings_are_english():
    paths = [ROOT / "main.py"]
    for folder in ("src", "tests", "tools"):
        paths.extend((ROOT / folder).rglob("*.py"))
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                assert not contains_persian(token.string), f"{path}:{token.start[0]}"
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert not contains_persian(ast.get_docstring(node) or ""), path


def test_public_readme_and_configuration_comments_are_english():
    assert not contains_persian((ROOT / "README.md").read_text(encoding="utf-8"))
    paths = [ROOT / "compose.yaml", ROOT / ".env.example", ROOT / ".gitignore"]
    paths.extend((ROOT / "scripts").glob("*.ps1"))
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                assert not contains_persian(line), f"{path}:{number}"
