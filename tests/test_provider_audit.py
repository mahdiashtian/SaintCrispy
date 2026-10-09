"""Keep live origin diagnostics reproducible without publishing signed URLs."""

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("provider_audit", ROOT / "tools/audit_providers.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_multiple_reported_links_keep_separate_cases_for_the_same_provider():
    first = "https://soundcloud.com/quavoofficial/away"
    second = "https://soundcloud.com/octobersveryown/quebec"
    args = SimpleNamespace(
        sites=["soundcloud", "youtube"], url=[("soundcloud", first), ("soundcloud", second)]
    )
    assert audit.requested_cases(args) == [
        ("soundcloud", "soundcloud", first),
        ("soundcloud", "soundcloud:2", second),
        ("youtube", "youtube", audit.SAMPLES["youtube"]),
    ]


@pytest.mark.parametrize(
    "value", ["unknown=https://example.com", "soundcloud=file:///tmp/private", "bad"]
)
def test_invalid_audit_case_is_rejected_before_origin_requests(value):
    with pytest.raises(argparse.ArgumentTypeError):
        audit.url_argument(value)
