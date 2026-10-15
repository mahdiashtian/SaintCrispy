"""Safe runtime checks, optionally followed by full downloads of the reported links.

Run with the bot's Python and environment on the bot host. No credential values,
cookie paths, proxy addresses or signed URLs are printed. Sends no Telegram messages.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import tomllib
from contextlib import AsyncExitStack
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def package_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def cookie_status(path):
    result = {"configured": bool(path), "readable": False, "writable": False, "netscape": False}
    if not path:
        return result
    try:
        with Path(path).open(encoding="utf-8-sig") as stream:
            first_line = stream.readline(256).strip()
        result.update(
            readable=True,
            writable=os.access(path, os.W_OK),
            netscape=first_line in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"},
        )
    except (OSError, UnicodeError):
        pass
    return result


async def runtime_status(spec):
    name, _, path = spec.partition(":")
    executable = path or shutil.which(name)
    result = {"name": name if name in {"node", "deno", "bun", "quickjs"} else "unknown"}
    result["available"] = bool(executable)
    if not executable or result["name"] == "unknown":
        return result
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        async with asyncio.timeout(5):
            output, _ = await process.communicate()
        match = re.search(rb"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output[:256])
        result["available"] = process.returncode == 0
        if match:
            result["version"] = match[1].decode("ascii")
    except (OSError, TimeoutError):
        result["available"] = False
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
    return result


async def helper_status(base_url):
    result = {"configured": bool(base_url), "reachable": False}
    if not base_url:
        return result
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
            response = await http.get(base_url.rstrip("/") + "/ping")
            result["http_status"] = response.status_code
            data = response.raise_for_status().json()
            helper_version = data.get("version") if isinstance(data, dict) else None
            if isinstance(helper_version, str) and re.fullmatch(r"\d+\.\d+\.\d+", helper_version):
                result.update(reachable=True, version=helper_version)
                result["matches_plugin"] = helper_version == package_version(
                    "bgutil-ytdlp-pot-provider"
                )
    except (httpx.HTTPError, ValueError):
        pass
    return result


async def configuration():
    runtime, helper = await asyncio.gather(
        runtime_status(os.environ.get("YOUTUBE_JS_RUNTIME") or "node"),
        helper_status(os.environ.get("YOUTUBE_POT_BASE_URL")),
    )
    packages = {
        name: package_version(name)
        for name in ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider", "curl-cffi")
    }
    pins = dependency_pins()
    issues = [
        {"code": "package_pin_mismatch", "package": name, "expected": expected}
        for name, expected in pins.items()
        if packages.get(name) != expected
    ]
    if not runtime["available"]:
        issues.append({"code": "youtube_js_runtime_unavailable"})
    return {
        "account_scope": "environment_defaults",
        "database_accounts_checked": False,
        "python": ".".join(map(str, sys.version_info[:3])),
        "packages": packages,
        "deployment_issues": issues,
        "youtube": {
            "js_runtime": runtime,
            "cookies": cookie_status(os.environ.get("YOUTUBE_COOKIES_FILE")),
            "proxy_configured": bool(os.environ.get("YOUTUBE_PROXY")),
            "doh_configured": bool(os.environ.get("YOUTUBE_DOH_URL")),
            "player_override_configured": bool(os.environ.get("YOUTUBE_PLAYER_CLIENTS")),
            "pot_helper": helper,
        },
        "instagram": {
            "cookies_configured": bool(os.environ.get("INSTAGRAM_COOKIE")),
            "proxy_configured": bool(os.environ.get("INSTAGRAM_PROXY")),
            "doh_configured": bool(os.environ.get("INSTAGRAM_DOH_URL")),
            "http3_requested": os.environ.get("INSTAGRAM_HTTP_VERSION") == "3",
            "public_resolver_enabled": bool(os.environ.get("INSTAGRAM_RESOLVER_URL", "default")),
        },
    }


def dependency_pins():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        dependencies = tomllib.load(stream)["project"]["dependencies"]
    pins = {}
    for dependency in dependencies:
        match = re.fullmatch(
            r"(yt-dlp|curl-cffi|bgutil-ytdlp-pot-provider)(?:\[[^]]+\])?==(.+)", dependency
        )
        if match:
            pins[match[1]] = match[2]
    return pins


def network_failure(error):
    """Keep bounded exception identities/errno, never exception text or request data."""
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 6:
        seen.add(id(error))
        row = {"type": type(error).__name__}
        for field in ("errno", "code"):
            value = getattr(error, field, None)
            if type(value) is int:
                row[field] = value
        chain.append(row)
        error = error.__cause__ or error.__context__
    return chain


async def connection_probe(http, url):
    started = time.monotonic()
    phase = "request"

    async def trace(event, details):
        nonlocal phase
        for marker, label in (
            ("connect_tcp.started", "tcp_connect"),
            ("connect_tcp.complete", "http_headers"),
            ("start_tls.started", "tls"),
            ("start_tls.complete", "http_headers"),
        ):
            if event.endswith(marker):
                phase = label

    try:
        async with asyncio.timeout(15):
            async with http.stream(
                "GET", url, follow_redirects=False, extensions={"trace": trace}
            ) as response:
                # Headers are enough: do not consume media/page bodies or follow login redirects.
                return {
                    "http_response_received": True,
                    "http_status": response.status_code,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
    except (httpx.HTTPError, TimeoutError) as error:
        return {
            "http_response_received": False,
            "failed_phase": phase,
            "failure_chain": network_failure(error),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }


async def network_status():
    from core.dns_tunnel import configured_proxy
    from downloader_bot.downloaders.instagram.client import instagram_session
    from downloader_bot.downloaders.youtube.urls import network_host

    # Separate owning transports and routes. No account cookies are needed for connectivity.
    async def probe(site):
        try:
            if site == "youtube":
                async with AsyncExitStack() as stack:
                    proxy = await configured_proxy(stack, os.environ, "YOUTUBE", network_host)
                    http = await stack.enter_async_context(
                        httpx.AsyncClient(proxy=proxy or None, trust_env=False, timeout=12)
                    )
                    return await connection_probe(
                        http, "https://www.youtube.com/watch?v=l6zflbTGNFQ"
                    )
            async with instagram_session(os.environ.get("INSTAGRAM_PROXY", "")) as http:
                return await connection_probe(http, "https://www.instagram.com/reel/DeR9eU8idi0/")
        except Exception as error:
            # Invalid configuration in one provider must not suppress the other's report.
            return {
                "http_response_received": False,
                "failed_phase": "transport_setup",
                "failure_chain": network_failure(error),
            }

    yt, ig = await asyncio.gather(probe("youtube"), probe("instagram"))
    return {"scope": "unauthenticated_connectivity_only", "youtube": yt, "instagram": ig}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Verify full origin streams to EOF")
    parser.add_argument(
        "--network",
        action="store_true",
        help="Probe provider connections without downloading media",
    )
    parser.add_argument("--output", default=".runtime/video-access.json")
    parser.add_argument("--youtube-url", default="https://www.youtube.com/watch?v=l6zflbTGNFQ")
    parser.add_argument("--instagram-url", default="https://www.instagram.com/reel/DeR9eU8idi0/")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=True, interpolate=False, encoding="utf-8-sig")
    report = await configuration()
    if args.network:
        report["network"] = await network_status()
    print(json.dumps(report, indent=2), flush=True)
    if args.live:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "tools/audit_providers.py"),
            "--sites",
            "youtube",
            "instagram",
            "--url",
            "youtube=" + args.youtube_url,
            "--url",
            "instagram=" + args.instagram_url,
            "--stream",
            "--strict",
            "--case-timeout",
            "180",
            "--output",
            args.output,
        )
        try:
            return await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
