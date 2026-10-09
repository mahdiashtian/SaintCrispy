import ast
import importlib
import inspect
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from downloader_bot.__main__ import create_provider_session
from downloader_bot.downloaders.base import Downloader

ROOT = Path(__file__).resolve().parents[1] / "src/downloader_bot/downloaders"
PROVIDERS = ("soundcloud", "xnxx", "xvideos", "pinterest", "youtube", "instagram")
FILES = {"__init__.py", "client.py", "parser.py", "downloader.py", "handler.py", "urls.py"}


def test_no_shared_concrete_provider_package_or_module_is_added():
    packages = {path.name for path in ROOT.iterdir() if path.is_dir() and list(path.glob("*.py"))}
    assert packages == set(PROVIDERS)
    assert {path.name for path in ROOT.glob("*.py")} == {"__init__.py", "base.py", "router.py"}


@pytest.mark.parametrize("site", PROVIDERS)
def test_providers_own_the_same_files_and_implement_the_contract_directly(site):
    assert {path.name for path in (ROOT / site).glob("*.py")} == FILES
    module = importlib.import_module(f"downloader_bot.downloaders.{site}.downloader")
    classes = [
        value
        for value in vars(module).values()
        if inspect.isclass(value)
        and value.__module__ == module.__name__
        and issubclass(value, Downloader)
    ]
    assert len(classes) == 1
    downloader = classes[0]
    assert downloader.__bases__ == (Downloader,)
    for method in ("inspect", "resolve"):
        function = downloader.__dict__[method]
        assert inspect.iscoroutinefunction(function)
        assert function.__module__ == module.__name__
        signature = inspect.signature(function)
        contract = inspect.signature(getattr(Downloader, method))
        assert tuple(signature.parameters) == tuple(contract.parameters)
        assert all(
            parameter.kind == contract.parameters[name].kind
            and parameter.default == contract.parameters[name].default
            for name, parameter in signature.parameters.items()
        )


@pytest.mark.parametrize("site", PROVIDERS)
def test_provider_imports_do_not_depend_on_other_sites_or_shared_concrete_logic(site):
    prefix = f"downloader_bot.downloaders.{site}"
    for path in (ROOT / site).glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level <= 1, f"{path}: imports from outside its provider"
                modules = [node.module or ""] if node.level == 0 else []
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for name in modules:
                if name.startswith("downloader_bot.downloaders"):
                    assert name == "downloader_bot.downloaders.base" or (
                        name == prefix or name.startswith(prefix + ".")
                    ), f"{path}: shares provider implementation through {name}"
                assert name not in {"downloader_bot.urls", "downloader_bot.handlers.media"}
                assert name not in {
                    "downloader_bot.streaming",
                    "downloader_bot.service",
                    "downloader_bot.database",
                    "downloader_bot.contracts",
                    "downloader_bot.downloaders.router",
                }, f"{path}: provider depends on shared concrete infrastructure"


def test_provider_files_are_owned_locally_and_are_not_symlinks_or_hardlinks():
    identities = set()
    for site in PROVIDERS:
        folder = (ROOT / site).resolve()
        for name in FILES:
            path = folder / name
            assert not path.is_symlink() and path.resolve().parent == folder
            identity = (path.stat().st_dev, path.stat().st_ino)
            assert identity not in identities, f"{path}: shares a file with another provider"
            identities.add(identity)


def test_changing_one_parser_cannot_change_another_provider(monkeypatch):
    from downloader_bot.downloaders.pinterest import parser as pinterest
    from downloader_bot.downloaders.xnxx import parser as xnxx
    from downloader_bot.downloaders.xvideos import parser as xvideos

    monkeypatch.setattr(xnxx, "player_url", lambda *args: None)
    page = "setVideoUrlHigh('https://cdn.example/video.mp4')"
    assert xvideos.extract_links(page, "https://www.xvideos.com/video-demo/title")["high"] == (
        "https://cdn.example/video.mp4"
    )
    assert xnxx.extract_links(page, "https://www.xnxx.com/video-demo/title")["high"] is None
    assert len({xnxx.HLSVariant, xvideos.HLSVariant, pinterest.HLSVariant}) == 3
    assert (
        len({xnxx.read_hls_variants, xvideos.read_hls_variants, pinterest.read_hls_variants}) == 3
    )


async def test_provider_and_account_sessions_isolate_cookies_and_lifecycles():
    async with AsyncExitStack() as stack:
        sessions = [await create_provider_session(stack) for _ in range(5)]
        assert len({id(session) for session in sessions}) == 5
        sessions[0].cookies.set("session", "one-account", domain="cdn.example")
        sessions[0].headers["Authorization"] = "Bearer one-account"
        for session in sessions[1:]:
            assert not session.cookies
            assert "Authorization" not in session.headers
        await sessions[0].aclose()
        assert all(not session.is_closed for session in sessions[1:])
    assert all(session.is_closed for session in sessions)
