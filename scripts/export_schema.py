"""Export schema reference SQL; never load credentials or contact a database."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.postgres.schema import reference_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the reference is outdated")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/reference/POSTGRES_SCHEMA.sql")
    args = parser.parse_args()
    sql = reference_schema()
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != sql:
            print("Schema reference is missing or outdated; run python scripts/export_schema.py")
            return 1
        print("Schema reference matches the application migration chain")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(sql, encoding="utf-8", newline="\n")
    print(f"Schema reference written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
