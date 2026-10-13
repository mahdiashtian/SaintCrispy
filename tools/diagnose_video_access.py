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
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "packages": {
            name: package_version(name)
            for name in ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider", "curl-cffi")
        },
        "youtube": {
            "js_runtime": runtime,
            "cookies": cookie_status(os.environ.get("YOUTUBE_COOKIES_FILE")),
            "proxy_configured": bool(os.environ.get("YOUTUBE_PROXY")),
            "player_override_configured": bool(os.environ.get("YOUTUBE_PLAYER_CLIENTS")),
            "pot_helper": helper,
        },
        "instagram": {
            "cookies_configured": bool(os.environ.get("INSTAGRAM_COOKIE")),
            "proxy_configured": bool(os.environ.get("INSTAGRAM_PROXY")),
        },
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Verify full origin streams to EOF")
    parser.add_argument("--output", default=".runtime/video-access.json")
    parser.add_argument("--youtube-url", default="https://www.youtube.com/watch?v=l6zflbTGNFQ")
    parser.add_argument("--instagram-url", default="https://www.instagram.com/reel/DeR9eU8idi0/")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    print(json.dumps(await configuration(), indent=2), flush=True)
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
