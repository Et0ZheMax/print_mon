import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.gui import PrinterDashboard  # noqa: E402


def test_sort_printers_orders_names_case_insensitively() -> None:
    printers = ["zeta-2", "Alpha-1", "beta-3", "alpha-2"]

    assert PrinterDashboard._sort_printers(printers) == [
        "Alpha-1",
        "alpha-2",
        "beta-3",
        "zeta-2",
    ]
