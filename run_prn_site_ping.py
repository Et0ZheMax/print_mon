"""Convenience launcher for prn-site-ping with the default config."""
from __future__ import annotations

from pathlib import Path
import sys


# Keep the convenience launcher runnable straight from a source checkout.
PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))

from prn_site_ping.__main__ import main  # noqa: E402


def run() -> None:
    config_path = PROJECT_ROOT / "config" / "printers.txt"
    main(["--config", str(config_path)])


if __name__ == "__main__":
    run()
