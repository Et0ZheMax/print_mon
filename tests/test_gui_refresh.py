import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.gui import PrinterDashboard


class _FakeDashboard:
    def __init__(self) -> None:
        self.printers = ["a"]
        self.printers_path = Path("/tmp/printers.txt")
        self.status_var = type("S", (), {"set": lambda self, x: None})()
        self.refreshed: list[bool] = []

    def _sort_printers(self, names):
        return sorted(names)

    def _render_printer_cards(self):
        return None

    def refresh_all(self, force_snmp: bool):
        self.refreshed.append(force_snmp)


def test_sync_refresh_forces_snmp(monkeypatch):
    monkeypatch.setattr("prn_site_ping.gui.write_printers_file", lambda *_: None)
    fake = _FakeDashboard()
    PrinterDashboard._apply_server_printers(fake, ["b", "a"])
    assert fake.refreshed == [True]
