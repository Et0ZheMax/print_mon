from __future__ import annotations

import argparse

from .app_dirs import get_app_data_dir
from .config import load_printers
from .gui import AppConfig, PrinterDashboard


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prn-site-ping", description="Tkinter dashboard for printer availability")
    p.add_argument(
        "--config",
        default=None,
        help="Path to printers list file (txt). If omitted: env PRN_SITE_PING_CONFIG, then ./config/printers*.txt",
    )
    p.add_argument("--columns", type=int, default=3, help="Number of button columns")
    p.add_argument("--timeout", type=float, default=1.0, help="TCP connect timeout (seconds)")
    p.add_argument("--title", type=str, default="Управление принтерами", help="Window title")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    printers = load_printers(args.config)
    cfg = AppConfig(printers=printers, columns=args.columns, timeout=args.timeout, title=args.title)

    state_dir = get_app_data_dir("prn-site-ping")
    app = PrinterDashboard(cfg, state_dir=state_dir)
    app.run()


if __name__ == "__main__":
    main()
