"""Run the bot on the host with the configuration beside this entry point."""

import os
import runpy
import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", override=True, interpolate=False, encoding="utf-8-sig")
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    runpy.run_module("downloader_bot", run_name="__main__")


if __name__ == "__main__":
    main()
