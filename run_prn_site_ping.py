"""Convenience launcher for prn-site-ping with the default config."""
from __future__ import annotations

from pathlib import Path

from prn_site_ping.__main__ import main


def run() -> None:
    config_path = Path(__file__).resolve().parent / "config" / "printers.txt"
    main(["--config", str(config_path)])


if __name__ == "__main__":
    run()
