"""Summarize retained transfer events; payload totals are separate from NIC counters."""

import argparse
import glob
import json
from collections import Counter, defaultdict
from datetime import datetime
from math import ceil
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone, for example 2026-10-09T00:00:00Z")
    return parsed


def summarize(paths, *, limit=None, site=None, since=None, until=None):
    counts = Counter()
    sizes = Counter()
    methods = Counter()
    stages = Counter()
    sites = defaultdict(Counter)
    durations = []
    first = last = None
    seen = set()
    malformed = 0
    sessions = {}
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    finished = timestamp(record["timestamp"])
                except (ValueError, KeyError, TypeError):
                    malformed += 1
                    continue
                if (since is not None and finished < since) or (
                    until is not None and finished > until
                ):
                    continue
                if record.get("event") in {"metrics_interval", "runtime_stopped"}:
                    session = record.get("session_id")
                    if session not in sessions or finished > sessions[session][0]:
                        sessions[session] = (finished, record)
                if record.get("event") != "transfer_finished":
                    continue
                key = (record.get("session_id"), record.get("transfer_id"))
                if key in seen or (site is not None and record.get("site") != site):
                    continue
                if limit is not None and len(seen) >= limit:
                    continue
                seen.add(key)
                outcome = record["outcome"]
                counts[outcome] += 1
                provider = record["site"]
                sites[provider][outcome] += 1
                methods[record.get("method", "unknown")] += 1
                for name, amount in record.get("bytes", {}).items():
                    sizes[name] += amount
                if outcome == "success":
                    sizes["delivered_file_bytes"] += record.get("file_size_bytes") or 0
                for name, seconds in record.get("stages_seconds", {}).items():
                    stages[name] += seconds
                durations.append(record["elapsed_seconds"])
                started = timestamp(record.get("started_at", record["timestamp"]))
                first = min(first, started) if first is not None else started
                last = max(last, finished) if last is not None else finished
    durations.sort()
    wall = max(0, (last - first).total_seconds()) if first is not None else 0
    return {
        "transfers": len(seen),
        "outcomes": dict(counts),
        "methods": dict(methods),
        "sites": {key: dict(value) for key, value in sites.items()},
        "first_started_at": first.isoformat() if first is not None else None,
        "last_finished_at": last.isoformat() if last is not None else None,
        "wall_seconds": round(wall, 6),
        "elapsed_sum_seconds": round(sum(durations), 6),
        "elapsed_p95_seconds": durations[max(0, ceil(len(durations) * 0.95) - 1)]
        if durations
        else None,
        "bytes": dict(sizes),
        "mib": {key: round(value / 1024**2, 4) for key, value in sizes.items()},
        "stages_sum_seconds": {key: round(value, 6) for key, value in stages.items()},
        "stream_read_mib_per_wall_second": round(sizes["stream_read_bytes"] / 1024**2 / wall, 4)
        if wall
        else None,
        "upload_acked_mib_per_wall_second": round(sizes["upload_acked_bytes"] / 1024**2 / wall, 4)
        if wall
        else None,
        "malformed_records_skipped": malformed,
        "session_summaries": [
            {
                "session_id": key,
                "timestamp": record["timestamp"],
                "scope": "session_totals_at_latest_retained_snapshot",
                "totals": record.get("totals", {}),
                "log_records_dropped": record.get("log_records_dropped", 0),
                "log_write_errors": record.get("log_write_errors", 0),
            }
            for key, (_, record) in sessions.items()
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="+", help="JSONL paths or quoted globs, including rotated files"
    )
    parser.add_argument(
        "--files", type=int, help="Analyze the first N matching completed transfer records"
    )
    parser.add_argument("--site", help="Restrict transfer records to one provider")
    parser.add_argument("--since", type=timestamp)
    parser.add_argument("--until", type=timestamp)
    args = parser.parse_args()
    if args.files is not None and args.files < 1:
        parser.error("files must be positive")
    paths = {
        Path(path) for pattern in args.paths for path in glob.glob(pattern) if Path(path).is_file()
    }
    if not paths:
        parser.error("No log files matched")
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("since must be before until")
    # Rotated files are older than the current file; lexical suffix order is incorrect.
    paths = sorted(paths, key=lambda path: (path.stat().st_mtime_ns, str(path)))
    print(
        json.dumps(
            summarize(paths, limit=args.files, site=args.site, since=args.since, until=args.until),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
