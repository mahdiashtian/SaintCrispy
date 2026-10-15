"""Replay a local MP4 through both split-track pipelines; never contact Telegram.

The loopback origin adds a configurable delay per HTTP request. This measures
application work and round trips, not live YouTube/CDN or Telegram bandwidth.
"""

import argparse
import asyncio
import json
import re
import tempfile
import time
from collections import Counter
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx

from bot.delivery import TelegramDelivery
from bot.progress import TransferProgress
from bot.transfers import faststart
from downloader_bot.downloaders.youtube.parser import HTTP_CHUNK_SIZE
from schemas.media import Quality, Source
from services.observability import TransferTrace, current_transfer


async def command(*arguments):
    process = await asyncio.create_subprocess_exec(
        *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        output, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError("Local media command failed")
    return output


async def benchmark(args):
    path = await faststart.disk_call(Path(args.input).resolve)
    if (await faststart.disk_call(path.stat)).st_size > 128 * 1024 * 1024:
        raise ValueError("Use a complete MP4 no larger than 128 MiB for this local replay")
    probe = json.loads(
        await command(
            args.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        )
    )
    seconds = float(probe["format"]["duration"])
    quality = Quality("replay", "Replay", "h264", None, "mp4", "video/mp4", "dash", "")
    rows, active = [], set()
    state = Counter()

    with tempfile.TemporaryDirectory(prefix="video-benchmark-") as directory:
        tracks = {}
        for name, kind in (("video", "v"), ("audio", "a")):
            output = Path(directory) / f"{name}.mp4"
            await command(
                args.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                f"0:{kind}:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            )
            tracks["/" + name] = await faststart.disk_call(output.read_bytes)

        async def serve(reader, writer):
            active.add(asyncio.current_task())
            try:
                request = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
                name = request.splitlines()[0].split()[1]
                data = tracks[name]
                match = re.search(r"(?im)^range: bytes=(\d+)-(\d*)", request)
                start = int(match[1]) if match else 0
                stop = min(int(match[2]), len(data) - 1) if match and match[2] else len(data) - 1
                state["requests"] += 1
                state["largest_request"] = max(state["largest_request"], stop - start + 1)
                await asyncio.sleep(args.request_delay_ms / 1000)
                status = "206 Partial Content" if match else "200 OK"
                header = (
                    f"HTTP/1.1 {status}\r\nConnection: close\r\nAccept-Ranges: bytes\r\n"
                    f"Content-Length: {stop - start + 1}\r\n"
                )
                if match:
                    header += f"Content-Range: bytes {start}-{stop}/{len(data)}\r\n"
                writer.write((header + "\r\n").encode())
                started, sent = time.monotonic(), 0
                for offset in range(start, stop + 1, 65536):
                    block = data[offset : min(offset + 65536, stop + 1)]
                    writer.write(block)
                    await writer.drain()
                    state["sent_bytes"] += len(block)
                    sent += len(block)
                    if args.large_request_mib_per_second and stop - start + 1 > 10_000_000:
                        due = sent / (args.large_request_mib_per_second * 1024 * 1024)
                        await asyncio.sleep(max(0, due - (time.monotonic() - started)))
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
                active.discard(asyncio.current_task())

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        source = Source(
            base + "/video",
            "dash",
            audio_url=base + "/audio",
            proxy="",
            size_bytes=len(tracks["/video"]),
            require_audio=True,
            duration=seconds,
            input_protocol="progressive",
            audio_protocol="progressive",
        )
        original_block = faststart.STAGING_BLOCK_SIZE
        try:
            for name, block, ranged in (
                ("baseline", 65536, False),
                ("optimized", original_block, True),
                ("optimized", original_block, True),
                ("baseline", 65536, False),
            ):
                state.clear()
                faststart.STAGING_BLOCK_SIZE = block
                progress = TransferProgress()
                trace = TransferTrace(
                    SimpleNamespace(batch={"id": "local"}, counters=Counter()),
                    SimpleNamespace(site="youtube", content_id="local-replay"),
                    quality,
                    progress,
                )
                token = current_transfer.set(trace)

                async def acknowledge(request):
                    return True

                try:
                    async with httpx.AsyncClient(trust_env=False) as http:
                        delivery = TelegramDelivery(
                            acknowledge,
                            http,
                            args.ffmpeg,
                            max_file_bytes=256 * 1024 * 1024,
                            upload_parallelism=4,
                        )
                        started = time.monotonic()
                        _, info = await delivery._upload(
                            replace(source, chunk_size=HTTP_CHUNK_SIZE if ranged else None),
                            quality,
                            "replay.mp4",
                            progress,
                        )
                        elapsed = time.monotonic() - started
                    row = {
                        "pipeline": name,
                        "elapsed_seconds": round(elapsed, 4),
                        "http_requests": state["requests"],
                        "http_sent_bytes": state["sent_bytes"],
                        "largest_http_request_bytes": state["largest_request"],
                        "prepared_bytes": progress.downloaded,
                        "source_input_bytes": trace.source_size,
                        "uploaded_bytes": progress.uploaded,
                        "media_seconds": progress.seconds,
                        "faststart": info.faststart,
                        "stages_seconds": {k: round(v, 4) for k, v in trace.stage_seconds.items()},
                    }
                    assert progress.upload_done and abs(progress.seconds - seconds) < 2
                    if ranged:
                        assert trace.source_size == sum(map(len, tracks.values()))
                    rows.append(row)
                    print(json.dumps(row), flush=True)
                finally:
                    current_transfer.reset(token)
        finally:
            faststart.STAGING_BLOCK_SIZE = original_block
            server.close()
            await server.wait_closed()
            for task in tuple(active):
                task.cancel()
            await asyncio.gather(*tuple(active), return_exceptions=True)
    averages = {
        name: sum(r["elapsed_seconds"] for r in rows if r["pipeline"] == name) / 2
        for name in ("baseline", "optimized")
    }
    result = {
        "scope": "loopback_replay_simulated_telegram",
        "request_delay_ms": args.request_delay_ms,
        "origin_throttling_simulated": bool(args.large_request_mib_per_second),
        "large_request_mib_per_second": args.large_request_mib_per_second,
        "rows": rows,
        "speedup": round(averages["baseline"] / averages["optimized"], 3),
    }
    if args.report:
        await faststart.disk_call(
            Path(args.report).write_text, json.dumps(result, indent=2), "utf-8"
        )
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="An authorized, complete local MP4 with video and audio")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--request-delay-ms", type=float, default=40)
    parser.add_argument(
        "--large-request-mib-per-second",
        type=float,
        default=0,
        help="Optional simulated rate cap for requests exceeding 10 MB; default disabled",
    )
    parser.add_argument("--report", help="Optional local JSON report")
    arguments = parser.parse_args()
    if not 0 <= arguments.request_delay_ms <= 1000:
        parser.error("Request delay must be between 0 and 1000 ms")
    if not 0 <= arguments.large_request_mib_per_second <= 100:
        parser.error("Large-request rate must be between 0 (disabled) and 100 MiB/s")
    asyncio.run(benchmark(arguments))
