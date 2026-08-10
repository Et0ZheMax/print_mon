import sys
from pathlib import Path
from types import SimpleNamespace

# Allow running tests without installing the package
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import pytest

from prn_site_ping.print_server import (  # noqa: E402
    _parse_printer_names,
    fetch_printers_from_server,
    normalize_server_name,
)


def test_parse_printer_names_unique_order() -> None:
    raw = "\nQueueA\nQueueB\nQueueA\n\nQueueC\n"
    assert _parse_printer_names(raw) == ["QueueA", "QueueB", "QueueC"]


def test_fetch_printers_from_server_returns_empty_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr("prn_site_ping.print_server.platform.system", lambda: "Linux")
    assert fetch_printers_from_server("dc02") == []


def test_normalize_server_name_accepts_unc_prefix() -> None:
    assert normalize_server_name(r"\\dc02") == "dc02"


def test_normalize_server_name_rejects_powershell_injection() -> None:
    with pytest.raises(ValueError):
        normalize_server_name("dc02'; Remove-Item *")


def test_fetch_printers_uses_noninteractive_safe_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout="QueueA\nQueueB\n")

    monkeypatch.setattr("prn_site_ping.print_server.platform.system", lambda: "Windows")
    monkeypatch.setattr("prn_site_ping.print_server.subprocess.run", fake_run)

    assert fetch_printers_from_server("dc02") == ["QueueA", "QueueB"]
    command = captured["command"]
    assert "-NonInteractive" in command
    assert "Get-Printer -ComputerName 'dc02'" in command[-1]
