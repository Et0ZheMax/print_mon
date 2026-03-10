import sys
from pathlib import Path

# Allow running tests without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from prn_site_ping.print_server import _parse_printer_names, fetch_printers_from_server  # noqa: E402


def test_parse_printer_names_unique_order() -> None:
    raw = "\nQueueA\nQueueB\nQueueA\n\nQueueC\n"
    assert _parse_printer_names(raw) == ["QueueA", "QueueB", "QueueC"]


def test_fetch_printers_from_server_returns_empty_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr("prn_site_ping.print_server.platform.system", lambda: "Linux")
    assert fetch_printers_from_server("dc02") == []
