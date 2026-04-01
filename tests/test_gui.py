import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.gui import PrinterDashboard, _compose_card_summary  # noqa: E402


def test_sort_printers_orders_names_case_insensitively() -> None:
    printers = ["zeta-2", "Alpha-1", "beta-3", "alpha-2"]

    assert PrinterDashboard._sort_printers(printers) == [
        "Alpha-1",
        "alpha-2",
        "beta-3",
        "zeta-2",
    ]


def test_compose_card_summary_appends_diagnostic_line() -> None:
    assert _compose_card_summary("SNMP: нет данных", "SNMP timeout") == "SNMP: нет данных\ndiag: SNMP timeout"


def test_compose_card_summary_without_diagnostic_keeps_summary() -> None:
    assert _compose_card_summary("K 50%", None) == "K 50%"
