import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "tools/summarize_logs.py"
SPEC = importlib.util.spec_from_file_location("summarize_logs", MODULE_PATH)
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def transfer(identity, site="youtube", method="progressive"):
    return {
        "timestamp": "2026-10-09T00:00:10Z",
        "started_at": "2026-10-09T00:00:00Z",
        "event": "transfer_finished",
        "session_id": "session",
        "transfer_id": identity,
        "outcome": "success",
        "site": site,
        "method": method,
        "file_size_bytes": 1024**2,
        "elapsed_seconds": 10,
        "stages_seconds": {"download": 8, "upload": 7},
        "bytes": {"stream_read_bytes": 1024**2, "upload_acked_bytes": 1024**2},
    }


def test_summary_uses_wall_time_deduplicates_and_keeps_nic_totals_separate(tmp_path):
    path = tmp_path / "performance.jsonl"
    records = [
        transfer("1"),
        transfer("2"),
        transfer("1"),
        {
            "timestamp": "2026-10-09T00:00:30Z",
            "event": "metrics_interval",
            "session_id": "session",
            "totals": {"network_received_bytes": 9 * 1024**2},
            "log_records_dropped": 3,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + '\n{"truncated":', encoding="utf-8"
    )
    result = summary.summarize([path])
    assert result["transfers"] == 2
    assert result["wall_seconds"] == 10 and result["elapsed_sum_seconds"] == 20
    assert result["bytes"]["stream_read_bytes"] == 2 * 1024**2
    assert result["stages_sum_seconds"] == {"download": 16, "upload": 14}
    assert result["stream_read_mib_per_wall_second"] == 0.2
    assert result["malformed_records_skipped"] == 1
    assert "network_received_bytes" not in result["bytes"]
    assert result["session_summaries"][0]["totals"]["network_received_bytes"] == 9 * 1024**2
    assert result["session_summaries"][0]["log_records_dropped"] == 3


def test_summary_limits_n_files_and_filters_site_and_time(tmp_path):
    path = tmp_path / "performance.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [transfer("1", "pinterest"), transfer("2"), transfer("3")]
        ),
        encoding="utf-8",
    )
    assert summary.summarize([path], site="youtube", limit=1)["transfers"] == 1
    assert (
        summary.summarize([path], since=summary.timestamp("2026-10-10T00:00:00Z"))["transfers"] == 0
    )


def test_filter_timestamps_require_an_explicit_timezone():
    with pytest.raises(ValueError, match="timezone"):
        summary.timestamp("2026-10-09T00:00:00")


def test_p95_for_two_transfers_selects_the_upper_observation(tmp_path):
    records = [transfer("1"), transfer("2")]
    records[0]["elapsed_seconds"] = 1
    path = tmp_path / "performance.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    assert summary.summarize([path])["elapsed_p95_seconds"] == 10
