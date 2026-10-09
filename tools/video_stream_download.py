"""Download an authorized video page, MP4 URL, or HLS manifest with yt-dlp."""

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit


def http_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if "\r" in value or "\n" in value:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("Use a valid HTTP or HTTPS URL.") from None
    return value


def positive_int(value: str) -> int:
    try:
        number = int(value)
        if number < 1:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("Use a positive integer.") from None
    return number


def show_sources(info: dict, all_formats: bool = False) -> None:
    formats = (
        info.get("formats") or [info] if all_formats else info.get("requested_formats") or [info]
    )
    seen = set()
    print("\nExtracted media URLs:")
    for media in formats:
        for kind, key in (("media", "url"), ("manifest", "manifest_url")):
            url = media.get(key)
            if not url or url in seen:
                continue
            seen.add(url)
            print(f"[{media.get('format_id', '?')}] {kind}: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=http_url)
    parser.add_argument("--output", default="Downloads")
    parser.add_argument("--list-formats", action="store_true")
    parser.add_argument("--all-urls", action="store_true", help="Print URLs for all formats.")
    parser.add_argument("--height", type=positive_int, help="Maximum known video height.")
    parser.add_argument("--referer", type=http_url)
    parser.add_argument("--ffmpeg-location", help="FFmpeg executable or directory.")
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError
    except ImportError:
        print('Install with: python -m pip install -U "yt-dlp[default]"', file=sys.stderr)
        return 2

    output = Path(args.output).expanduser().resolve()
    video_filter = f"[height<={args.height}]" if args.height else ""
    options = {
        "format": ("bv*+ba/b" if args.list_formats else f"bv*{video_filter}+ba/b{video_filter}"),
        "outtmpl": str(output / "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "continuedl": True,
        "overwrites": False,
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": False,
        "concurrent_fragment_downloads": args.concurrency,
        "socket_timeout": 30,
        "merge_output_format": "mkv",
    }
    if args.referer:
        options["http_headers"] = {"Referer": args.referer}
    if args.ffmpeg_location:
        options["ffmpeg_location"] = args.ffmpeg_location

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(args.url, download=False)
            if not info or info.get("_type", "video") != "video":
                raise ValueError("Use a single video page or a direct media URL.")
            print(f"\nTitle: {info.get('title', info.get('id', '?'))}")
            downloader.list_formats(info)
            show_sources(info, all_formats=args.all_urls)
            if args.list_formats:
                return 0
            output.mkdir(parents=True, exist_ok=True)
            downloader.process_ie_result(info, download=True)
            print(f"\nOutput directory: {output}")
            return 0
    except (DownloadError, OSError, ValueError) as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; partial files are kept for a later retry.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
